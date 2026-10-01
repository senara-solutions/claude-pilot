---
title: "A detached background dispatch cannot outlive a headless session — the reached PreToolUse hook strips run_in_background so every dispatch is blocking"
date: 2026-10-01
problem_type: tooling_decision
track: knowledge
module: permissions
component: pre-tool-use-hook
tags:
  - claude-pilot
  - agent-dispatch
  - subagent
  - run_in_background
  - headless
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

## The fix — strip `run_in_background` on the already-reached hook

claude-pilot already registers a `PreToolUse` hook on `Agent|Task`
(`create_subagent_model_inherit_hook`, cpp#257/263) that rewrites the forwarded
dispatch input **without** any `permissionDecision` — admission-neutral. The CLI
fires `PreToolUse` for every tool call, and this is the one SDK path an `Agent`
dispatch from the main pilot actually traverses (the dispatch is authorized
*upstream* of `can_use_tool`, so a guard there is dead code — see
`harness-runtime-tools-bypass-can-use-tool.md`). That reachability is already
established for this hook; cpp#267 reuses it.

The same hook now **also** removes `run_in_background` when it is present and
truthy (`_maybe_strip_run_in_background`). In headless every dispatch becomes
blocking: it completes within the turn, so there is no detached task for session
close to orphan. The two rewrites compose into **one** `updatedInput`, and each
emits its **own** `review_degraded` audit event (`agent_dispatch_model_inherit`
and `agent_dispatch_run_in_background_stripped`); both can co-occur on a single
dispatch and both are then auditable.

Crucially, a single assistant message carrying several `Agent` calls **still runs
them in parallel**: the strip removes only the detached/polled *background* mode,
not within-message concurrency. So the review stays parallel-within-a-message;
only the "dispatch-then-yield" death is closed. The cost of piste (a) is that
review becomes sequential *across* messages — the trade Prime ratified
(2026-10-01) as the minimal, safe-direction change.

Why not make `_merge_stream` keep the session open until background tasks finish
(piste b)? It is more faithful to interactive mode, but strictly riskier: the
wait must be bounded and "what counts as still-in-flight" defined. Forcing the
dispatch blocking is the smaller, provably-terminating change.

## This is a harness-behavior correction, not an admission change

The hook adds **no `permissionDecision`** — for either rewrite. It changes the
dispatch's *input*, never whether the dispatch is *admitted*. Bash admission is
byte-identical; `tier1.py` is unmodified; `is_tier1_auto_approve`,
`is_tier3_dangerous`, `TIER3_PATTERNS`, egress, and `_denial_is_terminal` are
untouched. So the MPC gate suffices; no admission signature is required. The
harness-behavior point: **a detached background dispatch cannot outlive a headless
session, so the reached hook forces it blocking.**

## Proving it: rewrite ⊥ effect-verification

Prime required the *rewrite* and the *invariant* to be two separate assertions,
never the same one:

- **Rewrite-test** (`test_cpp267_run_in_background_is_stripped_when_truthy`,
  `..._absent_or_falsy_passes_through_unchanged`): the rewrite logic
  (`_maybe_strip_run_in_background`) drops a truthy `run_in_background` and leaves
  absent/`False`/falsy untouched, without mutating the input.
- **Invariant-test**
  (`test_cpp267_invariant_no_background_dispatch_forwarded_via_sdk_control_request`):
  the invariant that governs the lethality — *in headless, no detached background
  task outlives the session* — asserted at the level that governs it: **after the
  reached hook, no `Agent` dispatch the pilot forwards carries
  `run_in_background: true`**. Reachability is proven through the SDK's own hook
  path — `Query._handle_control_request` with a `hook_callback`
  `SDKControlRequest`, exactly as the cpp#263 reachability test — **not** a direct
  call to the hook function (a green direct call attests the function, not the
  path the CLI drives; that is the flaw that failed the cpp#257 first gate).
- **Combined-test**
  (`test_cpp267_model_inherit_and_background_strip_combine_on_one_dispatch`): the
  exact `624656b1` shape (`model: sonnet` + `run_in_background: true` on a 1M
  session), driven through the SDK path — the single forwarded `updatedInput` has
  **both** the `model` override dropped and `run_in_background` stripped, **both**
  audit events fire, and no `permissionDecision` is added (cpp#263 model-inherit
  stays intact).

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
bg_rewrite = _maybe_strip_run_in_background(bg_source)             # cpp#267
if bg_rewrite is not None:
    rewritten = bg_rewrite
    audit.emit("review_degraded",
               {"reason": "agent_dispatch_run_in_background_stripped", ...})

return {} if rewritten is None else {
    "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": rewritten}
}
```

Related:
`docs/solutions/tooling-decisions/a-context-inheriting-dispatch-must-not-force-a-smaller-window-model.md`
(the same hook, the model-inherit rewrite cpp#267 extends),
`docs/solutions/tooling-decisions/harness-runtime-tools-bypass-can-use-tool.md`
(why the PreToolUse hook is the reached path, not `can_use_tool`).
