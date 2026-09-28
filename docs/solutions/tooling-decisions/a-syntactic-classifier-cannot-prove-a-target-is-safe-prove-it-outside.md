---
title: "A syntactic classifier cannot prove a target is safe — put the proof outside the classifier, and scope an exception by a named place before a general property"
date: 2026-09-28
last_updated: 2026-09-28
module: claude_pilot.permissions
component: permission-classifier
problem_type: design_decision
category: tooling-decisions
severity: high
status: "doctrine ratified by mika-prime 2026-09-28; implementation pending Vincent ratification — tickets cpp#211 (tightening) and cpp#213 (designated-path exception) open, cpp#212 (git-provenance exception) held in reserve, cpp#214 (wait-ceiling) measure-first. No policy code written yet."
tags: [permissions, policy, lethality, rule-a, destructive-verbs, bright-line, defense-in-depth, sandbox, git-provenance, claude-pilot-205, claude-pilot-211, claude-pilot-212, claude-pilot-213, claude-pilot-214, mika-1686, mika-2054, mika-2544, mika-2548]
applies_when: "designing an exception to a syntactic permission classifier that decides an action's safety from the command string alone"
---

# A syntactic classifier cannot prove a target is safe — prove it outside

## Context

claude-pilot's permission classifier decides, from the command *string*, whether
a bash call is admitted, and — separately (see the sibling learning, *a refusal
and its lethality are two decisions*) — whether a refusal is TERMINAL (kills the
session) or SURVIVABLE (the pilot gets the deny and routes around it).

On 2026-09-26, cpp#205 ratified **rule (a)**: destructive verbs (`rm -rf`,
`chmod -R`, `dd`, `mkfs`, fork-bombs) stay TERMINAL *regardless of target*. The
stated reason was not caution for its own sake — it was a proof boundary: **a
syntactic classifier cannot prove what a target actually is.** `rm -rf $X` where
`$X` came from anywhere, `rm -rf ./build` where `./build` might be a symlink out
of the tree — the string does not carry the evidence, so the classifier must not
pretend to have it. Rule (a) is a deliberate bright line.

## What happened

Rule (a) then killed pilots doing something obviously harmless:

- 2026-09-26 16:36–42Z, pilot `83db3a82` (impl mika#2054): `rm -rf verif-2054-redcheck` — its own empty red-check scratch dir, inside the worktree — TERMINAL → session death.
- 2026-09-26 19:11Z, pilot `cbdd3f5b` (groom mika#2544): `rm -rf .t2544 && git status` — its own groom scratch — TERMINAL → death.

Both pilots were tidying a directory **they had just created themselves**, under
the worktree. Rule (a) turned a refusal into lost work. Two proposals to carve a
survivable exception appeared:

- **cpp#212 — git provenance:** any directory that is *untracked* and strictly
  under the worktree → `rm -rf` becomes survivable. Untracked (`git ls-files`
  fails) proves the content was created this session by the pilot, so nothing
  human or versioned is destroyed. A real proof — but it lives *at the target*,
  and its surface is *every* untracked path.
- **cpp#213 — designated place:** `rm`/`rmdir` whose resolved targets are all
  under `<worktree>/.pilot-scratch/` (a prefix the dispatch pre-creates, excludes
  from git, and empties each run) → survivable; everywhere else terminal,
  unchanged. Paired with the dispatch-side prompt rule mika#2548 that forbids
  `rm` on that prefix.

## The pattern

**1. Don't teach a syntactic classifier to trust a target. Make the target safe
by construction, outside the classifier, and let the classifier only recognize
that safe zone.** Rule (a) forbade the *classifier* from judging a target; it
never forbade a target from being made safe *elsewhere*. Every sound member of
this arc keeps its safety proof outside the syntactic string:

| Decision | Where the proof lives (not in the classifier) |
|----------|-----------------------------------------------|
| cpp#211 (tighten `cp`/`mv` `"$HOME"` escape) | the bwrap sandbox bounds the blast radius; the fix restores the classifier so each layer holds *alone* |
| cpp#213 (designated `.pilot-scratch/`) | the dispatch *guarantees* a safe zone in advance; the classifier only recognizes the named prefix |
| cpp#214 (wait ceiling) | a measured generation-flow signal, not a guessed timeout number |

**2. Between two valid exceptions, prefer the one that names a PLACE over the
one that reads a general PROPERTY.** Both #212 and #213 are lethality-only (the
`rm -rf` stays *refused*; only its terminality drops — no destruction is ever
admitted) and both reject `..`, outbound symlinks, and out-of-worktree targets.
`git provenance` is the more *seductive* — "untracked proves it's disposable"
lands true on contact. That is exactly the tell to slow down on: the test is not
"is it true" but "does the second condition *constrain* the first or merely
*repeat its form*." Untracked genuinely constrains (git is orthogonal to the
classifier), but its surface is *all* untracked paths under the worktree — a
gradient you must re-evaluate git state to place. `rm -rf survives under
.pilot-scratch/` is a *door*: it reads and audits at a glance. A bright line is
defended by its legibility. Prefer the door.

**3. Promote the general rule from the narrow one on MEASURED evidence, not on
principle.** #213 ships now; #212 is held in reserve. Its promotion condition is
data, not elegance: *if a pilot still dies on an `rm -rf` of scratch OUTSIDE
`.pilot-scratch/` after #213 deploys*, the narrow surface proved too small and
the git-provenance generalization is earned. Not before. Adopting both at once
widens the line for an unmeasured gain. The narrow choice leaves a *measurable,
reversible* cost (one out-of-prefix death = the promotion signal) — prefer a
measurable cost that informs the next decision over a broadening that preempts
it.

**4. A layer that admits what its neighbor refuses is not depth — it is an
aligned hole.** cpp#211: a `cp`/`mv` destination that is *quoted* and rooted at
`$`/`~` (`cp secret "$HOME/exfil"`) was ADMITTED where the bare form is refused,
because a YAML lookahead required whitespace before the metacharacter and missed
the quoted operand. The sandbox bounds it, but defense-in-depth only counts when
each layer holds alone. Tightening is the one direction this doctrine never
debates.

## Why it matters

The unifying thread across cpp#205 → #211/#212/#213/#214 is one sentence: **never
ask the syntactic classifier to prove what it cannot; make the zone safe
elsewhere and have the classifier merely recognize it.** That is what keeps
rule (a) coherent *with* an exception, rather than eroded by one. It also gives a
reusable selection rule for any future carve: name a place, keep the proof
external and orthogonal, and let measurement — not the appeal of a clever
invariant — decide when to generalize.

## References

- Sibling doctrine: `docs/solutions/tooling-decisions/a-refusal-and-its-lethality-are-two-decisions-not-one.md` (cpp#128 — lethality is a separate decision).
- Related security learning: `docs/solutions/security-issues/a-quote-scope-exemption-inherits-the-whole-grammar-escapes-included.md`.
- Tickets: cpp#205 (rule (a)), cpp#211 (tighten quoted-rooted `cp`/`mv`), cpp#212 (git-provenance exception, reserve), cpp#213 (designated `.pilot-scratch/`), cpp#214 (wait-ceiling, measure-first). Dispatch-side: mika#2548 / PR mika#2550.
- Prime bearing 2026-09-28: #211 ratified; #213 now, #212 in reserve under measured promotion; #214 measure-before-number.
