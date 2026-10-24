---
title: "A drift guard between a list and its prose must run in both directions"
date: 2026-10-24
module: claude_pilot.tier1
component: system-prompt-hint
problem_type: test_gap
category: best-practices
severity: medium
tags: [drift-guard, hint, terminal-forms, lethality, registry, exemptions, ac3, ac4, replay, claude-pilot-278]
applies_when: "a test claims that prose (a hint, a doc, a help text) is derived from a code list, or a new pattern is added to a lethality or admission set"
---

# A drift guard must run in both directions

## What happened

cpp#278 AC3 asked that the model-facing hint stay derived from tier1's lethality
lists, "with a test that fails if a terminal form added to the list is not named
in the hint". #281 shipped `test_terminal_forms_hint_derived_from_tier1`. That test
walked the **registry** and checked that each entry was in the hint and in the
lethal set. It never walked the **lethal set**. So the case AC3 was written for, a
new lethal pattern, stayed invisible. A `perl -e` pattern added to
`_TIER3_VERB_PATTERNS_FOR_LETHALITY` left all three drift tests green, and 18 of
the 23 lethal patterns had never been decided at all.

A drift guard that iterates the curated side proves only that the curated side is
correct. The side that grows without anyone noticing, the source list, is the one
that needs iterating.

## The fix

- Iterate the source set. Every pattern in the lethal union must be **either** a
  registry entry (named in the hint) **or** an entry of `_TERMINAL_FORM_EXEMPTIONS`
  with a written reason. The failure message names each undecided pattern.
- Make the exemption a decision, not a hole. Each exemption needs a non-empty
  reason, must still be lethal (no stale entry), and must not also be in the
  registry.
- Keep the hint short with families. Several patterns share one bullet string,
  and the renderer emits each distinct bullet once.
- Classify by measurement, not by guess. 36 907 real pilot Bash calls, replayed
  through the real handler, showed what pilots actually die on:
  `git push --force-with-lease` (47 kills), `git reset --hard` (16), `<(…)` (6),
  `git branch -D` (2), `grep "DELETE FROM"` (1). Those went into the hint. The
  ones with zero hits (`dd`, `mkfs`, `chmod -R`, …) went into the exemptions.
- Never promise a carve-out in the hint. A first draft said process substitution
  was fatal "except a read-only `<(…)`". The carve-out (cpp#252) spares only
  `git show|git diff|cat|printf` interiors, so `diff <(sort a) <(sort b)` still
  kills the run. If a survivable form is described as fatal, nothing is lost; if
  a fatal form is described as survivable, the pilot dies. Name the fatal case
  only, and pin a witness from just outside the carve-out.
- Prove the test bites. Add a mutation to a throwaway copy and check that the red
  names it. Never commit the mutation.

## AC4: prove that no decision moved

The change only names regex objects and adds prose, but "only" is a claim. The
replay passes every corpus command (all string literals in `tests/` plus the
transcript commands, 41 405 in all, on two cwds) through
`create_permission_handler`, `is_tier1_auto_approve`, `is_tier3_dangerous`,
`is_tier3_dangerous_for_lethality` and `_denial_is_terminal`, then writes sorted
JSONL. The base and fix outputs were byte-identical. Positive control: the
`perl -e` mutant diverged on exactly its two rows. Without that control, an
identical output could also come from a harness that measures nothing.

## Side finding (not fixed here: it would move lethality)

The measurement turned up two over-lethal patterns: `git\s+push\s+.*-\w*f\b` matches
any `-…f` segment of a branch slug (`-of`, `-rif`, `-diff`, `-pdf`: 3 plain `git push -u` calls killed in the corpus; the hint now offers `git push -u origin HEAD`), and
`git\s+branch\s+.*-\w*D\b` matches `echo "---AHEAD…"` after `git branch
--show-current` (2 kills). Fixing them changes lethality, so it needs its own
ticket.
