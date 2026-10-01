---
title: "An Agent dispatch that inherits the parent context must not force a smaller-context-window model — the harness rewrites it to inherit the session model"
date: 2026-10-01
problem_type: tooling_decision
track: knowledge
module: permissions
component: permission-classifier
tags:
  - claude-pilot
  - can_use_tool
  - agent-dispatch
  - subagent
  - context-window
  - prompt-too-long
  - headless
applies_when: "A headless pilot's subagent dispatch (the Agent tool) fails `Prompt is too long` even with a trivial prompt, and the CE quality gate (ce-code-review multi-agent depth, ce-simplify-code) silently falls back to an in-session pass."
---

# A context-inheriting Agent dispatch must not force a smaller-window model

## Context

A pilot session runs on a large-context model — the pilot logs
`[init] ... model claude-opus-5[1m]`, a 1M-token window. A CE skill then
dispatches a subagent via the `Agent` tool and **forces** a smaller model:
`Agent {description: ..., subagent_type: general-purpose, model: "sonnet", run_in_background: true}`
(sonnet/haiku = 200k window). The subagent loads the project/base context —
`CLAUDE.md` + skills + tool schemas + system prompt — which **already exceeds
200k** for the mika repo. So the dispatch fails `Prompt is too long`
**regardless of the prompt body**: a one-word probe fails too (cpp#257,
mika#2606, dispatch `e9cb7b6b`).

The three things that look causal but are **not** (measured off
`~/.mika/data/pilot-transcripts/`):

- **The requested model alone.** Other sessions ran `sonnet` dispatches with 0
  errors (`cfc99de6`, `f9ce4a8d`).
- **Parent context size alone.** The failures had 430–490k parent contexts;
  the two clean sessions had *larger* ones (737k, 723k).
- **`run_in_background`.** `9aba7764` failed with foreground calls.

The real variable is the **ratio**: a dispatch fails when the subagent's base
context is larger than the *forced* model's window. The clean `sonnet` sessions
had a base that still fit under 200k; the failing ones did not. The parent
session survives because it runs the 1M model, so any dispatch that **inherits**
the session model succeeds; only a forced **smaller-window** override fails.

The downstream harm: `ce-code-review`'s multi-agent depth gate (forced on a
`size_band: large` diff) and `ce-simplify-code`'s reuse pass both dispatch this
way. When the dispatch fails they fall back to an in-session pass — the
never-skip quality gate **degrades silently** (only prose in the PR body said
so).

## The guard must sit on a path the tool actually traverses

This is the **central** lesson of cpp#257, learned the hard way at the gate.

**When a subagent dispatch inherits the parent context, the forced model's
window must be >= the session model's window.** The root fix is in the CE
plugin: the skill should not hard-code `model: "sonnet"` for a
context-inheriting dispatch (it should inherit, or pick a 1M-window model).
Because the plugin is a separate repo, claude-pilot carries a harness-side
backstop — but **where** that backstop lives is the whole game.

The first cut placed the rewrite in `can_use_tool` (the tiered permission
classifier). **It was inert.** An `Agent`/`Task` dispatch from the main pilot is
authorized **upstream** of `can_use_tool` — the SDK's allowed-tools list /
settings admit it before the permission callback is ever consulted — so the
callback never sees the dispatch. Measured on four deployed pilots (73e6f3ee,
189c0147, e9cb7b6b, 9aba7764): **26 `Agent` dispatches all RAN, 0 reached
`can_use_tool`** (the `[tool:request]` log showed only Bash/Edit/Write). This is
the exact class of `harness-runtime-tools-bypass-can-use-tool.md`: a guard on a
surface the tool bypasses is dead code that *reads* as protection. A unit test
that calls the handler directly goes green and attests the *function*, not the
*path* — which is precisely how the inert placement passed its first review.

**The reached placement is a SDK `PreToolUse` hook.** The CLI fires `PreToolUse`
for every tool call, independent of the permission decision, and a hook that
returns `updatedInput` rewrites the forwarded tool input. claude-pilot registers
one on `Agent|Task` (`create_subagent_model_inherit_hook`,
`src/claude_pilot/permissions.py`, wired into `ClaudeAgentOptions.hooks` in
`agent.py` and `shell.py`): when the dispatch forces a model whose window is
**strictly smaller** than the session model's, the hook returns
`{"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": <dispatch
without `model`>}}` so it inherits the (larger-window) session model. The
inherited 1M window holds the >200k base context the forced 200k window could
not.

Crucially the hook returns **no `permissionDecision`** — so it changes the
dispatch's *input* without changing whether it is *admitted*. Admission stays on
its own axis (tier1 / policy / egress / lethality), byte-identical. (The SDK does
document that a `PreToolUse` hook returning an *allow* decision would skip
`can_use_tool`; this hook deliberately returns no decision, so it does not.)

Rules that keep it tight and non-weakening:

- **Only ever drop a strictly-smaller-window override.** Never upgrade, never
  rewrite a same-or-larger request, never touch a dispatch with no model.
- **Determine the session model from `config.model`** (the `--model` the pilot
  launched with, a `PilotConfig` threaded from `cli.py` into `run_agent` →
  the hook factory — NOT `guardrails.config`, which is a
  `ResolvedGuardrailConfig` of timings and carries no model).
- **Fail safe when the session model is unknown** (`config` is `None`, or
  `config.model` is `None`/unparseable — the production case, since dispatch-lib
  does not pass `--model`): still drop a **known-small** override (sonnet/haiku,
  200k) — the pilot's default session model is the large-window one, so a forced
  small window is the failure class by construction. Never drop an
  unclassifiable or large-window override on a guess. This fail-safe is the
  **primary live mechanism** today.
- **Window table**, inferred from the model id (a *relative* ordering for the
  "is the forced model strictly smaller than the session's" question, not a
  precise token count): the `[1m]` beta suffix means a 1M window on any base
  model (checked first); **opus is the large-context session tier with OR
  without `[1m]`** (opus's native window is 1M, and a plain `claude-opus-5`
  session dispatching a forced `sonnet` is exactly this failure shape — so opus
  must not be misclassified at the small tier, or the `sonnet` override would
  look "not smaller" and the rewrite would never fire); sonnet/haiku **without**
  the `[1m]` opt-in are the 200k small tier; anything else is `None` (unknown →
  never guessed).
- **Emit `audit.emit("review_degraded", {reason: "agent_dispatch_model_inherit",
  placement: "pre_tool_use_hook", ...})` on every rewrite** so the adjusted gate
  is mechanically readable, not silent prose (cpp#257 AC2).

This is an **Agent-dispatch model rewrite**, categorically not a Bash
admission/lethality change. It does not touch `is_tier3_dangerous`,
`is_tier1_auto_approve`, the YAML policy, or any egress axis.

## Proving reachability, not just the function

Because the inert placement passed a direct-call unit test, a rewrite on the
reached path must be proven *through the SDK's own hook dispatch*, not by calling
the hook function. The reachability-proof test drives
`claude_agent_sdk._internal.query.Query._handle_control_request` with a
`hook_callback` `SDKControlRequest` — the exact method the CLI's control channel
invokes when a `PreToolUse` hook fires on a real `Agent` dispatch. The hook is
registered through the SDK's own `_hooks_to_internal_format` + the callback-id
wiring `Query.initialize()` performs, then invoked by the SDK via
`self.hook_callbacks[callback_id](...)`. The test asserts the forwarded
`updatedInput` has `model` dropped, that **no** `permissionDecision` is emitted,
and that the `review_degraded` marker fired. A green direct call to the hook
function would *not* satisfy this — that is the flaw that failed the first gate.

## No `can_use_tool` backstop — removed per the gate

An earlier revision kept a `can_use_tool` branch as a "harmless backstop". The
MPC gate rejected it (blocking condition): a branch that returns
`PermissionResultAllow` for a rewritten `Agent` **short-circuits tier1 + the
policy, which refuse `Agent` by default** — a latent admission widening, not a
harmless no-op. It is removed. The PreToolUse hook (updatedInput only, never a
`permissionDecision`) is the sole, admission-neutral placement. If a future SDK
stops firing `PreToolUse` for `Agent`, the CE-plugin root fix is the line of
defence — which is why the plugin fix is the root, not a permission-handler
allow-branch that would widen admission.

## Why This Matters

A silent quality-gate fallback is the worst failure mode: the PR merges looking
reviewed while the review never ran. The dispatch error (`Prompt is too long`)
is loud at the SDK layer but swallowed at the skill layer. Making the harness
rewrite the override turns a silent degradation into a successful review; making
it emit `review_degraded` means that even when a human must look, the signal is
machine-readable.

## When to Apply

- A headless pilot's `Agent`/`Task` dispatch fails `Prompt is too long` with a
  trivial prompt, and the session runs a larger-window model than the one the
  dispatch forces.
- A CE quality gate (review/simplify) reports a "harness-native fallback" in a
  PR body with no mechanical marker.
- Any future dispatch path where a subagent inherits the parent context but is
  pinned to a smaller-window model.

## Examples

**The failure (forced smaller window):**

```
session model: claude-opus-5[1m]   (1M window)
Agent { ..., model: "sonnet" }     (200k window)
subagent base context ≈ 430k       →  "Prompt is too long"  (prompt body irrelevant)
```

**The rewrite (on the reached path — a `PreToolUse` hook):**

```python
# agent.py — the hook is registered on the SDK options the pilot runs with:
options = ClaudeAgentOptions(
    ...,
    hooks={"PreToolUse": [HookMatcher(
        matcher="Agent|Task",
        hooks=[create_subagent_model_inherit_hook(config=pilot_config, task_id=task_id)],
    )]},
)

# permissions.py — the hook body (returns updatedInput, NO permissionDecision):
async def hook(input_data, tool_use_id, context):
    rewritten = _maybe_inherit_session_model(input_data["tool_input"], config)  # strictly-smaller window
    if rewritten is None:
        return {}                                          # no change, no admission touch
    audit.emit("review_degraded", {"reason": "agent_dispatch_model_inherit",
                                   "placement": "pre_tool_use_hook", ...})
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "updatedInput": rewritten}}  # model dropped → inherits 1M
```

**Inert AND latently-widening (what failed the gate) — the same logic in
`can_use_tool`:** an `Agent` dispatch is admitted upstream and never reaches the
callback, so the branch never fires in production; worse, were it reached it
would return `Allow` and short-circuit tier1 + policy (which deny `Agent` by
default). Removed per the MPC gate condition.

Related:
`docs/solutions/tooling-decisions/harness-runtime-tools-bypass-can-use-tool.md`
(the same permission surface; the bound on whether this guard can fire at all).
