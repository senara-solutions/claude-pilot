---
title: "A detached background dispatch cannot outlive a headless session — CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 in the CLI env removes run_in_background from the tool schema (primary switch); the reached PreToolUse hook forces run_in_background=False as belt-and-suspenders"
date: 2026-10-02
problem_type: tooling_decision
track: knowledge
module: permissions
component: pre-tool-use-hook
tags:
  - claude-pilot
  - agent-dispatch
  - subagent
  - run_in_background
  - CLAUDE_CODE_DISABLE_BACKGROUND_TASKS
  - headless
  - env-var
  - pre-tool-use-hook
  - ce-code-review
  - pipeline-incomplete
applies_when: "A headless pilot dispatches its reviewers (or any subagents) with `run_in_background: true` and then yields its turn; the session closes on the SDK ResultMessage and the background agents die with it — review lost, no PR, PIPELINE_INCOMPLETE."
---

# A detached background dispatch cannot outlive a headless session

## Context

A headless pilot runs `/ce:code-review`, which dispatches its reviewers via the
`Agent` tool. In the founding incident (`624656b1`, 2026-10-01, mika#1960
phase 2) the pilot dispatched **8 reviewers, all with `run_in_background: true`**,
then wrote "the eight reviewers are running in parallel […] I'm waiting for their
feedback" and **yielded its turn**. Nothing woke it: the SDK emitted a
`ResultMessage`, claude-pilot closed the session (`[done] Success | 113 turns |
$90.40`), and the background reviewers **died with it** — no review collected, no
compound written, no PR opened. Engine verdict: `PIPELINE_INCOMPLETE`.

In an **interactive** session, a turn that ends with background agents still in
flight is **re-woken** by their completion notification. In **headless**, nothing
wakes the pilot: ending a turn while waiting on background agents closes the
session. "Dispatch-then-yield" is lethal in headless only.

The class is latent, not rare. Across the last 40 pilot transcripts, 9 dispatch
subagents and **5 of those 9 dispatch in the background**. The four that survived
did so only because they kept working — and so kept the session alive — while the
agents ran. The one that *waited* by yielding died. The `ce-code-review` skill
forbids this in plain words ("Detaching local review into a polled background job
is forbidden"), yet the prohibition did not hold: a **prompt-level** prohibition
does not bind the harness. The correction belongs at the **substrate**.

## Why the first cut (cpp#269, strip) was inoperant

cpp#269 shipped the right *placement* but the wrong *rewrite*: it **removed** the
`run_in_background` key (`_maybe_strip_run_in_background` popped it). In the
embedded CLI, **"Agents run in the background BY DEFAULT"** and the foreground
gate is `run_in_background !== false`. Removing the key requests the default,
which is **still background** — so the lethality survived unchanged. A second
pilot (`bb9163e1`, mika#2627, 140 turns, $59.72) died the **same** death AFTER
#269 deployed: its stderr shows three `agent_dispatch_run_in_background_stripped`
events (the hook fired), yet each `Agent` call returned in **11–26 ms** and the
session ended on "waiting on them". Dispatches with no key were background too.
**`strip ≠ foreground`**: the only value the CLI reads as foreground is the key
present and literally `False`.

## The real switch — `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` in the CLI env

Forcing `run_in_background=False` in the dispatch's `updatedInput` is **still not
guaranteed**: a resume spawn of mika#2630 saw its agents go async *despite* an
explicit `run_in_background: false` — the harness can re-interpret an input
rewrite. The **real** switch of the embedded CLI is an environment variable, read
directly by the CLI's own background-tasks gate:

```js
function Bl(){ return d3().backgroundTasksDisabled
                      || a.CLAUDE_CODE_DISABLE_BACKGROUND_TASKS }
// the Agent (and Bash) tool schema is built conditionally on it:
n = Bl()||k8() ? e.omit({run_in_background:!0}) : e
```

With `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` set, `run_in_background` **disappears
from the Agent and Bash tool schemas entirely**: the model cannot request it, and
the CLI injects system-prompt text telling the model only synchronous subagents
exist. This is the CLI's **own gate**, not an input rewrite it can reinterpret —
so it is the **primary** switch, and the hook below is kept only as
belt-and-suspenders. The symbols `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` and
`backgroundTasksDisabled` are both present in the pinned bundled CLI binary
(`claude_agent_sdk/_bundled/claude`).

- **Value**: `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`. The gate is a raw truthy
  read of the string (any non-empty value triggers it); `"1"` is Claude Code's
  canonical truthy form for a `DISABLE_*` flag. An empty string would **not**
  disable it.
- **Mechanism**: passed via the SDK's own **`ClaudeAgentOptions.env`** option (in
  `agent.py`, constant `_CLI_FORCE_FOREGROUND_ENV`), **not** `os.environ`. The
  SDK's subprocess transport builds the CLI's effective env as
  `{**os.environ_without_CLAUDECODE, "CLAUDE_CODE_ENTRYPOINT": ..., **options.env,
  "CLAUDE_AGENT_SDK_VERSION": ...}` (`_internal/transport/subprocess_cli.py`), so
  an `options.env` entry lands in the launched CLI subprocess's environment and
  wins over any inherited value. Passed as a `dict(...)` copy so the shared
  module constant is never mutated.
- **Scope**: this touches **only** the CLI/agent subprocess the SDK launches. It
  is unrelated to `transport.py`'s *relay* subprocess, whose `scrub_env` is a
  different code path and is left untouched (and the var name matches no scrub
  pattern anyway).

### Side effect (assessed, accepted)

The same variable also removes `run_in_background` from the **Bash** tool schema.
A pilot can therefore no longer detach a long-running Bash command (e.g. a build)
into the background: it now runs **foreground** and must finish within the turn,
subject to the Bash tool's own timeout. This is **consistent with the fix's
intent** — a headless pilot that detaches a Bash build and yields suffers the
*same* death as the Agent case (the detached job is orphaned when the session
closes on `ResultMessage`), so foreground Bash is the correct headless behaviour,
not a regression. The one implication to flag: a genuinely long build that
previously relied on background Bash must now fit the foreground turn/timeout
budget. Whether any real pilot depends essentially on background Bash (Monitor,
etc.) is an MPC transcript-replay check at the gate.

## The belt — FORCE `run_in_background=False` on the already-reached hook

claude-pilot already registers a `PreToolUse` hook on `Agent|Task`
(`create_subagent_model_inherit_hook`, cpp#257/263) that rewrites the forwarded
dispatch input **without** any `permissionDecision` — admission-neutral. The CLI
fires `PreToolUse` for every tool call, and this is the one SDK path an `Agent`
dispatch from the main pilot actually traverses (the dispatch is authorized
*upstream* of `can_use_tool`, so a guard there is dead code — see
`harness-runtime-tools-bypass-can-use-tool.md`). That reachability is already
established for this hook; cpp#267 reuses it.

The same hook now **sets `run_in_background = False` explicitly**
(`_force_foreground_dispatch`) whenever the dispatch's value is anything other
than literal `False` — **truthy OR absent**, because an absent key is the CLI's
background default. If the value is already literally `False`, the hook no-ops.
The key is always left **present and `False`**, never removed. In headless every
dispatch then runs foreground: it completes within the turn, so there is no
detached task for session close to orphan. The two rewrites compose into **one**
`updatedInput`, and each emits its **own** `review_degraded` audit event
(`agent_dispatch_model_inherit` and `agent_dispatch_forced_foreground`); both can
co-occur on a single dispatch and both are then auditable.

Crucially, a single assistant message carrying several `Agent` calls **still runs
them in parallel**: the rewrite removes only the detached/polled *background*
mode, not within-message concurrency. So the review stays
parallel-within-a-message; only the "dispatch-then-yield" death is closed. The
cost of piste (a) is that review becomes sequential *across* messages — the trade
Prime ratified (2026-10-01) as the minimal, safe-direction change.

Why not make `_merge_stream` keep the session open until background tasks finish
(piste b)? It is more faithful to interactive mode, but strictly riskier: the
wait must be bounded and "what counts as still-in-flight" defined. Forcing the
dispatch foreground is the smaller, provably-terminating change.

## This is a harness-behavior correction, not an admission change

Neither layer touches admission. The env var sets a CLI runtime flag; the hook
adds **no `permissionDecision`** for either rewrite — it changes the dispatch's
*input*, never whether the dispatch is *admitted*. Bash admission is
byte-identical; `tier1.py` is unmodified; `is_tier1_auto_approve`,
`is_tier3_dangerous`, `TIER3_PATTERNS`, egress, and `_denial_is_terminal` are
untouched. So the MPC gate suffices; no admission signature is required. The
harness-behavior point: **a detached background dispatch cannot outlive a headless
session, so the CLI env var removes `run_in_background` from the tool schema
(primary) and the reached hook forces `run_in_background=False` (belt) — neither
strips the key to request the CLI's background default, which was cpp#269's
inoperant mistake.**

## Proving it: rewrite ⊥ effect-verification

Prime required the *rewrite* and the *invariant* to be two separate assertions,
never the same one:

- **Env-presence test (primary switch)**
  (`test_cpp267_run_agent_sets_disable_background_tasks_env_in_cli_subprocess`,
  `test_cpp267_force_foreground_env_constant_is_canonical_truthy`): asserts on the
  **real mechanism** — the `env` dict the pilot hands to `ClaudeAgentOptions`
  (captured through a spy `ClaudeAgentOptions`, not a CLI mock) carries
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, a non-empty (truthy) value, and is a
  **copy** of the module constant (not the shared object). Because the SDK merges
  `options.env` into the launched CLI's effective environment, a key present here
  is present in the CLI subprocess.
- **Rewrite-test** (`test_cpp267_run_in_background_is_forced_false_when_truthy`,
  `test_cpp267_absent_run_in_background_is_forced_false`,
  `test_cpp267_already_false_is_noop`): the rewrite logic
  (`_force_foreground_dispatch`) sets `run_in_background` to literal `False`
  (present, not removed) for a truthy value AND for an absent key, no-ops on an
  already-`False` dispatch, and does not mutate the input.
- **AC3 RED/GREEN invariant-test**
  (`test_cpp267_invariant_red_on_strip_green_on_force_false`): Prime's invariant
  *no background task survives session end in headless*, encoded by a model of the
  CLI gate (`run_in_background !== false`). It is **RED on cpp#269's strip** — the
  stripped dispatch has an absent key, which the CLI reads as the background
  default, so the invariant is violated — and **GREEN after fix267b** forces the
  key present and `False`.
- **Invariant-test (SDK path)**
  (`test_cpp267_invariant_no_background_dispatch_forwarded_via_sdk_control_request`):
  the invariant asserted on **CLI semantics** — after the reached hook, the
  forwarded `updatedInput` carries `run_in_background` **present and `=== False`**
  (the one value the CLI gate accepts as foreground; the behavioral contract is
  that the Agent result returns only at subagent END). Reachability is proven
  through the SDK's own hook path — `Query._handle_control_request` with a
  `hook_callback` `SDKControlRequest`, exactly as the cpp#263 reachability test —
  **not** a direct call to the hook function (a green direct call attests the
  function, not the path the CLI drives; that is the flaw that failed the cpp#257
  first gate).
- **Combined-test**
  (`test_cpp267_model_inherit_and_background_strip_combine_on_one_dispatch`): the
  exact `624656b1` / `bb9163e1` shape (`model: sonnet` + `run_in_background: true`
  on a 1M session), driven through the SDK path — the single forwarded
  `updatedInput` has **both** the `model` override dropped and `run_in_background`
  forced present-and-`False`, **both** audit events fire, and no
  `permissionDecision` is added (cpp#263 model-inherit stays intact).

## Why This Matters

A lost review is the worst failure mode: the pipeline reports `PIPELINE_INCOMPLETE`
only after the reviewers are already dead and the work of the turn is gone. A
prompt-level "do not background" prohibition reads as protection but does not
bind the harness. Moving the correction to the reached hook turns a lethal
"dispatch-then-yield" into a blocking dispatch that completes in the turn, and the
dedicated audit event makes the correction mechanically readable rather than
silent.

## When to Apply

- A headless pilot dispatches subagents with `run_in_background: true` and then
  yields its turn (the session closes before the agents report).
- A `ce-code-review` / `ce-simplify-code` run ends with `PIPELINE_INCOMPLETE` and
  the transcript shows background `Agent` dispatches with no foreground follow-up.
- Any headless dispatch path where a detached background task could outlive the
  turn that spawned it.

## Examples

**The failure (headless, dispatch-then-yield):**

```
turn 113: Agent { ..., run_in_background: true }  × 8    (all background)
pilot yields → SDK ResultMessage → session closes → 8 reviewers die
engine: PIPELINE_INCOMPLETE
```

**The rewrite (on the reached PreToolUse hook — input only, no permissionDecision):**

```python
# permissions.py — the same Agent|Task hook, now composing two rewrites:
model_rewrite = _maybe_inherit_session_model(tool_input, config)   # cpp#263
if model_rewrite is not None:
    rewritten = model_rewrite
    audit.emit("review_degraded", {"reason": "agent_dispatch_model_inherit", ...})

bg_source = rewritten if rewritten is not None else tool_input
bg_rewrite = _force_foreground_dispatch(bg_source)                # cpp#267/fix267b
if bg_rewrite is not None:   # fired when truthy OR absent; no-op only if already False
    rewritten = bg_rewrite   # rewritten["run_in_background"] is False (present)
    audit.emit("review_degraded",
               {"reason": "agent_dispatch_forced_foreground", ...})

return {} if rewritten is None else {
    "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": rewritten}
}
```

Related:
`docs/solutions/tooling-decisions/a-context-inheriting-dispatch-must-not-force-a-smaller-window-model.md`
(the same hook, the model-inherit rewrite cpp#267 extends),
`docs/solutions/tooling-decisions/harness-runtime-tools-bypass-can-use-tool.md`
(why the PreToolUse hook is the reached path, not `can_use_tool`).
