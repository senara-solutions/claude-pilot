---
title: "A sticky session marker is not a per-event verdict — and the test that only exercises one class cannot see the difference"
date: 2026-09-04
last_updated: 2026-09-04
module: claude_pilot.agent
component: session-guardrails
problem_type: security_finding
category: best-practices
severity: high
tags: [permissions, guardrails, sticky-flags, containment, non-regression-tests, negative-control, claude-pilot-144, claude-pilot-151]
applies_when: "gating a privileged or recovery action on a session-scoped flag that some earlier event set"
---

# A sticky session marker is not a per-event verdict

## What happened

cpp#144 introduced a useful pattern: permissions.py records a fact about a
refusal on the session guardrails, and agent.py reads it at exit to reclassify
an outcome. It is sticky by design — *"a session that asked once and was refused
once still has no operator to answer it, no matter how many turns follow."*

cpp#151 reused the pattern for a different job, and the reuse was wrong in a way
that took a security review to see. The new marker,
`nonterminal_policy_deny`, was set whenever a **survivable** refusal happened.
The resume gate then read it as *"this death followed a survivable refusal"*.

It does not say that. It says *"a survivable refusal happened at some point in
this session."* The two coincide only in a session with exactly one refusal.

**The exploit is three lines long.** Turn 1: `echo probe; ls` — refused,
survivable, marker armed **permanently**. Turn N: a write into `.git/hooks/` —
destination veto, `interrupt=True`, the kill claude-pilot deliberately asks
for. The turn dies; the gate consults the marker; the marker still says
"survivable"; the session that just tried to leave its sandbox is handed
another turn and told *"continue the task from where it stopped."*

The code comment asserted the opposite — *"a destination veto never sets the
marker, so a containment breach can never buy itself another turn"* — and it
was true of the veto **in isolation** and false of a **session**. That gap
between a true statement about one event and a false statement about a run is
the whole bug.

## The rule

**A flag that answers "did X ever happen?" must not be used to answer "was the
thing that just happened an X?"** When you gate an action on a session-scoped
flag, ask which question the flag actually answers, and expect the two to
diverge as soon as a session contains more than one event.

Two fixes, and use both when the stakes are containment:

1. **Give the disqualifying class its own sticky flag, and let it win.**
   `terminal_policy_deny` latches on any deliberately lethal refusal and vetoes
   the recovery for the rest of the run. Sticky on purpose, in the safe
   direction: a session that once tried to leave its worktree does not become
   trustworthy again three turns later.
2. **Find a signal that is not your own bookkeeping.** The SDK reports
   `ResultMessage.terminal_reason == "aborted_tools"` when a turn was cancelled
   by an interrupt control request — which is precisely what our own
   `interrupt=True` sends. Checking it is a *positive, upstream-sourced*
   identification of "we killed this", independent of every flag we maintain.
   For the hole to reopen, both would have to fail together.

## The test lesson, which is the sharper half

The non-regression test **existed** and **passed**. It armed a session whose
only refusal was terminal, and asserted no resume happened.

That test cannot fail in either world. It never constructs the mixed sequence —
survivable refusal *then* lethal refusal — which is the only shape where the
sticky flag and the per-event verdict disagree. It verified the premise the
implementation already believed.

**A non-regression test for a gate must exercise a session that satisfies the
gate's condition and still must be refused.** Concretely, the arms that matter:

```python
guardrails.note_policy_deny("Bash: echo probe; ls", terminal=False)          # eligible
guardrails.note_policy_deny("Bash: cp x .git/hooks/post-checkout", terminal=True)  # …and not
# EDE arrives -> exactly one client, no resume
```

Both new guards were then checked by neutralising each one in turn and
confirming the suite goes red. A guard nobody has watched fail is a guard
nobody has tested.

## Applies when

- Gating any recovery, retry, escalation, or privilege on `session.some_flag`
  set by an earlier event.
- Reusing a sticky-flag pattern that worked for classification (where "ever
  happened" is genuinely the question) to make an authorization decision (where
  it almost never is).
- Writing a non-regression test whose fixture makes the dangerous case
  unreachable. If the test would pass with the guard deleted, it is a comment.

## Related

- `a-decision-you-never-recorded-cannot-be-measured-later.md` — the same
  change's other lesson: the flag that makes a number honest is often the
  discriminator the fix needs.
- `one-trace-is-not-a-population-verify-the-premise-on-every-member.md` —
  the same failure of quantifiers, one level up.
