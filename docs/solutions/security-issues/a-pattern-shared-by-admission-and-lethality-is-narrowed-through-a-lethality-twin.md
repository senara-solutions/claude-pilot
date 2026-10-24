---
title: "A pattern shared by admission and lethality is narrowed through a lethality twin — tightening it in place moves admission"
date: 2026-10-24
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
symptoms:
  - "`git push -u origin <branch>` killed the pilot session when the branch name held a `-…f` segment (`-of`, `-rif`)"
  - "a probe `git branch --show-current; echo \"---AHEAD---\"` killed the session"
  - "the first in-place fix made 6 corpus commands go Deny -> Allow, including `git push -u origin <branch>`"
  - "the first lethality twin made real force forms survivable (`\\-f`, `$'-f'`, a backtick-wrapped push, a quoted `;` before `-f`) and backtracked exponentially on `>&>&…`"
root_cause: missing_validation
resolution_type: code_fix
tags: [permissions, bash, lethality, session-fatality, admission, tier3, git-push, git-branch, option-token, twin-pattern, corpus-replay, redos, catastrophic-backtracking, quoting, claude-pilot-278, claude-pilot-300]
applies_when: "changing a regex that TIER3_PATTERNS and the lethality verb set both reference, or writing a regex that walks a shell segment"
---

# A pattern shared by admission and lethality is narrowed through a lethality twin

## Problem

Two lethal patterns in `src/claude_pilot/tier1.py`, `git\s+push\s+.*-\w*f\b`
(`tier1.py:212`) and `git\s+branch\s+.*-\w*D\b`, searched the whole command for
a `-…f` / `-…D` substring after the verb. A branch name or the text of a
neighbouring command was enough. The spawn of claude-pilot#278 measured 5 pilot
sessions killed this way on 36 907 real commands (`~/.mika/data/pilot-transcripts/`),
none of which pushed by force or deleted a branch (claude-pilot#300).

## Symptoms

- `git push -u origin test/2677/flaky-mika2532-the-stderr-of-a-crash-on 2>&1 | tail -8` was refused and the session ended (`-of`).
- `git push -u origin ci/2566/publish-ui-la-v-rif-post-publish-5-15-s …` likewise (`-rif`).
- `git status …; git branch --show-current; echo "---AHEAD/BEHIND---"; …` likewise (`-AHEAD` matched `-\w*D\b`).

## What Didn't Work

**Tightening the shared pattern in place.** The pattern objects sit in
`TIER3_PATTERNS` (`tier1.py:223`), which `is_tier3_dangerous` (`tier1.py:755`)
reads for the REFUSAL, and `_TIER3_VERB_PATTERNS_FOR_LETHALITY` was
`TIER3_PATTERNS[:-1]`, the same objects. Anchoring them on option tokens fixed
lethality, but the corpus replay showed 6 commands going from Deny to **Allow**:
with the tier3 match gone, `git push -u origin <branch>` fell through to an
allow rule. The ticket was about lethality; auto-approving a push to a named
branch is a separate decision nobody made. Unit tests on the 5 verbatims were
green the whole time. Only the replay of admission showed it.

**The first twin.** A lethality-only regex that stopped the segment at the
first `;`/`|`/`&` and accepted a flag only after a blank or a quote. An
adversarial review, run against `_denial_is_terminal` with the old patterns
patched back in, found real force forms the blanket pattern had caught by
accident and the twin did not:

| command | old | first twin |
|---|---|---|
| `` `git push origin x -f` `` | terminal | survivable (backtick not a token end) |
| `git push --push-option="a;b" origin main -f` | terminal | survivable (quoted `;` ended the segment) |
| `git push origin \-f`, `git push origin $'-f'` | terminal | survivable (bash reads `-f`) |

It also gave `&` two owners: `(?<=[<>])&` and `&(?=>)` both match the `&` of
`>&>`, so a failing search on `git push >&>&…> x` doubled per pair (0.3 s at
n=20, through `_denial_is_terminal`, which runs on every refused Bash call while
the idle timer is paused).

## Solution

Leave admission on the original objects and give lethality its own:

```python
_LETHALITY_OPTION_TWINS: dict[re.Pattern[str], re.Pattern[str]] = {
    _GIT_PUSH_F_PATTERN: _GIT_PUSH_F_OPTION_PATTERN,
    _GIT_BRANCH_D_PATTERN: _GIT_BRANCH_D_OPTION_PATTERN,
}
_TIER3_VERB_PATTERNS_FOR_LETHALITY: tuple[re.Pattern[str], ...] = tuple(
    _LETHALITY_OPTION_TWINS.get(p, p) for p in TIER3_PATTERNS[:-1]
)
```

(`tier1.py:354-360`). The terminal-form registry points at the twins, because
its drift guard checks identity with the lethality set.

The twin reads the flag as an option word of the git command's own segment
(`tier1.py:341-353`):

- the segment walk consumes a quoted span or a `\X` pair whole, so a quoted
  `;` does not end it; an unterminated quote is one character, as the old
  pattern read it;
- every alternative of the walk starts on a different character
  (`(?<=[<>])&` vs `(?<![<>])&(?=>)`), so a failing search stays linear;
- the flag starts a word, optionally behind `$`, one quote and one backslash
  (`"-f"`, `$'-f'`, `\-f`), and ends before an optional quote then a blank, a
  separator, `)`, a backtick or the end;
- what counts as the flag is unchanged (`-\w*f`, `-\w*D`).

## Why This Works

A pattern object is a decision. When one object serves two decisions, editing
it edits both, and the tests written for the decision you meant to change say
nothing about the other. The twin keeps the refusal byte-identical by
construction: `TIER3_PATTERNS` has no diff, so `is_tier3_dangerous` cannot move.

The corpus replay (every `tests/` literal + every pilot Bash command + probes,
two cwds, through `create_permission_handler`, `is_tier1_auto_approve`,
`is_tier3_dangerous`, `is_tier3_dangerous_for_lethality`, `_denial_is_terminal`)
over 41 445 commands (82 890 rows) then showed 25 commands changed, all of the
two forms; `result`, `tier1` and `t3`
moved on no row, and the sha256 of every other row was identical before and
after.

## Prevention

- Before tightening a `TIER3_PATTERNS` entry for lethality, check whether the
  same object is in `_TIER3_VERB_PATTERNS_FOR_LETHALITY`. If it is, add a twin
  to `_LETHALITY_OPTION_TWINS` instead.
- Replay admission as well as lethality, and require `result` to be unchanged.
  A green unit test on the target forms is not evidence about admission.
- Narrowing a blanket pattern drops what it covered by accident; enumerate
  the old matches that are real hazards and pin them as terminal
  (`test_cpp300_force_and_delete_flags_stay_terminal`: quoted, escaped,
  `$'…'`, backtick, `$( )`, after `2>&1`). See
  [narrowing-a-blanket-guard-means-re-arming-what-it-covered-by-accident](narrowing-a-blanket-guard-means-re-arming-what-it-covered-by-accident.md).
- A regex that walks a shell segment gives every character one owner, and a
  timing test pins it (`test_cpp300_option_twins_fail_in_linear_time`). See
  [a-repeated-separator-whose-head-and-tail-both-match-blanks-is-a-redos](a-repeated-separator-whose-head-and-tail-both-match-blanks-is-a-redos.md).
- Prove the tests bite: restore the old pattern in the twin map (AC1 goes red),
  weaken `-\w*f` to `-\w+f` (AC2 goes red), restore the overlapping `&`
  alternatives (the timing test never finishes).

Left as they were on main, recorded as strict `xfail`: `git push origin +main`
is refused by the policy's default deny but not terminal (no tier3 pattern
matches a `+` refspec), and `git push -fu origin x` is admitted, because
`-\w*f\b` needs `f` to end the cluster. Changing either is a separate lethality
or admission decision.
