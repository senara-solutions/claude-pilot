---
title: "A refusal and its lethality are two decisions, not one — never fuse them in a permission gate"
date: 2026-08-30
last_updated: 2026-08-30
module: claude_pilot.permissions
component: permission-classifier
problem_type: design_decision
category: tooling-decisions
severity: critical
tags: [permissions, policy, can-use-tool, interrupt, agent-loop, guardrails, session-fatality, claude-pilot-20, claude-pilot-128, mika-1410, correlation-evidence, doc-drift]
applies_when: "designing or reviewing any gate that both refuses an action and decides whether the run continues"
---

# A refusal and its lethality are two decisions, not one

## Context

claude-pilot's `can_use_tool` callback returns `PermissionResultDeny` when the
deterministic policy refuses a tool call. cpp#20 joint 2 set `interrupt=True` on
**every** denial path, so a refusal did two things at once: it blocked the
command, and it aborted the SDK agent loop. The rationale is recorded in the
source — a non-terminal denial "surfaces the denial as a tool_result error the
LLM can fabricate around", so the pilot was made to exit rather than let a model
talk its way around a "no".

Fusing the two decisions cost the entire loop. Measured on the 60 most recent
pilot sessions (`/var/log/claude-pilot/*.stderr`, 2026-08-30): 11 sessions
carried at least one `[policy:deny]`, 11 ended in `error_during_execution`, and
they were the **same 11**. The tool-call distribution across the 60 was
`46 at 0 calls · 1 at 1 · 4 at 2 · 3 at 3 · 3 at 4 · 2 at 5 · 1 at 22`, and the
eleven that died were exactly `22, 5, 5, 4, 4, 3, 3, 2, 2, 2, 1` — **no zero**.
Every session that did any work was killed by a refusal. The most productive
session of the day, 22 tool calls, died like the rest.

The refused shapes were not dangerous commands. They were `echo "label"; command`
and `for d in …; do …; done` — the ordinary way anyone groups two reads into one
call. Each instance tripped a *different* rule id, which is why the previous
fixes (widening `bash-jq`, then `cargo` in cpp#127) each closed an instance and
left the class standing.

## Guidance

### 1. Separate the decision from its lethality, and name the split in code

A gate answers two independent questions:

- **Decision** — is the action permitted? (allow / refuse)
- **Lethality** — if refused, does the run also end?

Fuse them and every refusal becomes a session kill, so the gate's *strictness*
and the system's *survivability* trade against each other on a single axis. Split
them and each can be set on its own merits. cpp#128 introduced one predicate,
`permissions._denial_is_terminal`, as the single place that answers the second
question. The first question's code is untouched — no rule widened, no verb list
edited, no new `decision: allow`.

### 2. Reserve lethality for what the model cannot usefully adapt to

The test is not "how bad is this command" but "is there anything for the model to
do differently". Two classes survive as terminal in claude-pilot:

- **`_destination_veto_reason`** (worktree containment, control-plane denylist —
  cpp#38/cpp#42). The pilot tried to write outside its own sandbox. That is a
  containment breach in intent, not a refused idiom.
- **tier3-dangerous Bash** (`is_tier3_dangerous`). Note this is a whole-string
  `re.search`, so a dangerous tail chained onto an allowed prefix
  (`mkdir x && rm -rf /tmp/y`) is still caught and still terminal.
- **`escalate` / deny-with-notify.** Left untouched. It exists to put a human in
  the loop, so continuing past it defeats its only purpose — and `_fire_notify`
  has no dedup, so a non-terminal escalate turns a retry loop into a
  notification flood on the very channel that compensates for non-lethal
  denials elsewhere.

Everything else returns as a `tool_result` error the model can read and route
around.

### 2b. A boundary that is terminal on only one route is an oracle, not a boundary

The first cut kept the destination veto terminal *at its own call site* — which
is reached only when the command matched a write-capable allow rule.
`mkdir -p /outside/x` matched `bash-mkdir`, hit the veto, halted. The same escape
with a label in front, `echo "go"; mkdir -p /outside/x`, failed chain-safety,
never reached that call site, and came back non-terminal. Nothing was written
either way, but the boundary had degraded from a one-shot tripwire into
something a prompt-injected pilot could probe once per turn for its whole budget.

The fix is to consult the same predicate on every denial route, not just the one
the veto happens to sit on. Whenever you narrow a blanket behavior, enumerate the
routes the old blanket was covering; the ones you did not think about are exactly
where the blanket was doing real work.

### 3. When you cite guardrails as the safety backstop, go read them

The counter-reason for `interrupt=True` was that a non-terminal denial lets the
model loop or fabricate. The natural answer is "the session guardrails bound
it" — `maxTurns=200`, `stallThreshold=5`, `emptyResponseThreshold=5`,
`idleTimeoutMs=300_000` (`types.py:42-46`). Reading the code, only **one** of the
four actually bounds a refusal loop:

| Guardrail | Bounds a refused-but-busy session? | Why |
|---|---|---|
| `maxTurns=200` | **yes** | SDK-native turn cap; terminates with `error_max_turns`, a real `ResultMessage` |
| `stallThreshold=5` | no | `guardrails.py` resets `_consecutive_stall_turns = 0` on `has_tool_use`; a refused call is still a tool use |
| `emptyResponseThreshold=5` | no | resets the same way; a refusal loop produces content |
| `idleTimeoutMs=300_000` | no | rearmed by turn boundaries and content-bearing stream events; a busy loop keeps it alive |

The bound holds, and it terminates honestly with a terminal `ResultJson` — but it
is carried by `maxTurns` alone. Three of the four guardrails detect a **silent**
or **degenerate** session, not a busy-but-fruitless one. Citing a guardrail set
without checking which member covers your case is how a safety argument becomes
decorative.

### 4. A comment that contradicts the runtime is a bug report nobody filed

Two places in this repo had already written down the gap, in prose, for months:

- `policies/permissions.yaml:140-146` — "*broader shapes route to relay*". They
  did not route anywhere; they killed the session.
- `tier1.py` — "*this hint reduces the RATE of denied reaches; it does NOT close
  the session-fatality class … that class closes only when cpp#20 joint 2's
  contract is revised to distinguish adaptation from fabrication (mika#1410)*".

Both were accurate diagnoses sitting in comments while the loop lost every
productive session. When a design note describes an intent the runtime does not
implement, treat it as an open ticket, not as documentation. Changing behavior
means changing every note that described the old behavior in the same commit —
otherwise the next reader inherits the same contradiction.

### 5. Prove the fix with a two-armed negative control

A lethality change is invisible in the decision: both worlds return
`PermissionResultDeny`. A test asserting only "it is refused" passes with and
without the fix and proves nothing. `test_session_09fee003_shape_is_refused_but_no_longer_lethal`
runs both arms over the same command: with the fix it must be refused **and**
non-terminal (revert the call sites, this arm fails); with `_denial_is_terminal`
forced to `True` it must be terminal again (hard-code `interrupt=False` at the
call sites, this arm fails). Measured: reverting the three call sites to
`interrupt=True` fails 6 tests.

## Related

- `docs/solutions/security-issues/command-string-policy-allow-rules-are-compound-unsafe.md` — the allow-list layer that decides the *first* question.
- claude-pilot#20 (joint 2, the fused contract), #127 (the last per-instance widening), #128 (this split), mika#1410 (the ask, filed months earlier).
