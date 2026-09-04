---
title: "A decision you never recorded cannot be measured later — log the branch, not just the outcome"
date: 2026-09-04
last_updated: 2026-09-04
module: claude_pilot.permissions
component: permission-classifier
problem_type: design_decision
category: tooling-decisions
severity: high
tags: [permissions, observability, lethality, interrupt, agent-loop, guardrails, session-fatality, measurement, population-definition, claude-pilot-128, claude-pilot-151, sdk-resume]
applies_when: "a gate takes a branch that changes downstream behaviour, and you will later want to measure how often each branch fired"
---

# A decision you never recorded cannot be measured later

## Context

cpp#128 split a permission refusal into two decisions — **refuse** (always) and
**end the run** (`interrupt=True`, only for a destination veto or a
tier3-dangerous Bash command). It worked: a refusal's measured lethality fell
from 11-out-of-11 to 8-out-of-25.

Then cpp#151 arrived to close the residue, and hit a wall that had nothing to do
with the residue itself. Standing in front of those eight dead sessions, nobody
could say which of them claude-pilot had **asked** to kill and which had died
**despite** `interrupt=False`. The lethality branch was taken thousands of
times and recorded **zero** times:

- `ui.log_policy_deny(tool_name, detail, rule_id)` did not take the flag.
- `permissions._record_decision` emitted `decision` ∈ {allow, deny} and
  `rule_id` to the audit wire — not the flag.
- `grep interrupt` in `permission_events.py` returned nothing.

The eight were not one class. They were at least two superposed classes, and no
amount of re-reading the logs could separate them, because the separating fact
had never been written down.

## The trap

The acceptance criterion said: *the proportion of sessions dying in
`error_during_execution` among those that took a refusal must fall to zero.*

That sentence is measurable only if "took a refusal" is a population you can
name. Without the flag, the honest reading is "took **any** refusal" — which
includes the refusals that are **supposed** to be fatal. A fix that changed
nothing could then reach the target by quietly re-reading the population as
"took a survivable refusal", and a fix that worked could look like a failure
because deliberate kills were still counted as deaths.

**A threshold applied to an undefined population is not a measurement.** It is
a number that can be argued either way after the fact — which is exactly what
it was introduced to prevent.

## The rule

**When a gate takes a branch that changes downstream behaviour, record which
branch it took — at the same site, in the same call, from the same computation
that made it.**

Concretely, in the cpp#151 fix:

```python
# ONE computation, three consumers, at each of the four deny sites.
chain_terminal = _denial_is_terminal(tool_name, tool_input, cwd)
log_policy_deny(tool_name, detail, pd.rule_id, terminal=chain_terminal)   # operator
guardrails.note_policy_deny(f"{tool_name}: {detail}", terminal=chain_terminal)  # session
return _record_decision(
    PermissionResultDeny(message=veto_reason, interrupt=chain_terminal),  # SDK
    ..., terminal=chain_terminal,                                        # audit wire
)
```

Three properties do the work:

1. **One computation.** Calling `_denial_is_terminal` once per consumer would
   let the log line and the returned `interrupt` drift on a future edit — and
   a log that disagrees with the behaviour is worse than no log.
2. **A required keyword-only parameter.** `terminal` has no default on
   `log_policy_deny`. A new deny site cannot silently omit it and rejoin the
   two populations; the call simply does not compile-by-inspection.
3. **The literal where the value is literal.** The destination-veto site passes
   `terminal=True` as a literal, not a `_denial_is_terminal` call, because its
   `interrupt=True` is unconditional by design. The log says what the code says,
   for the same reason.

## The second-order payoff

Once recorded, the flag was not just measurable — it became **actionable**. The
same marker that defines the AC's population also gates the recovery: a bounded
resume is offered *only* to sessions whose refusal was survivable, so a
containment breach can never buy itself another turn. The observability step and
the safety property turned out to be the same step.

That is the general shape. The record you add to make a number honest is usually
also the discriminator the fix needs.

## Cost, named

The flag rides the cm#99 audit wire as a seventh field that cm does **not yet
persist** (`PermissionEventRequest` carries no `#[serde(deny_unknown_fields)]`,
so it is ignored rather than rejected). That is deliberate: the load-bearing
carrier is the stderr line the measurement greps, and the wire field is ready
the day the column exists. Sending a field ahead of its consumer is cheap; the
alternative — measuring first, instrumenting later — is what produced this
problem.

## Applies when

- A classifier, router, or gate branches on something the caller cannot
  reconstruct from the outcome alone.
- An acceptance criterion is phrased as a rate over a population, and the
  population is defined by a decision your code makes internally.
- You are about to write "we'll measure it after deploy" for a signal that does
  not exist in any log yet. It will not exist after deploy either.

## Related

- `a-refusal-and-its-lethality-are-two-decisions-not-one.md` — the cpp#128
  split this entry is the missing half of.
- `one-trace-is-not-a-population-verify-the-premise-on-every-member.md` — the
  other way a population claim goes wrong: generalising from a single trace.
