---
title: "A repeated separator whose head and tail both match blanks is a ReDoS — give every blank exactly one owner; the cpp#251 grep for (X+\\s*)* does not see this shape"
date: 2026-10-09
module: claude_pilot.permissions
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, bash, for-loop, redos, catastrophic-backtracking, ambiguous-grammar, separator, regex, denial-of-service, bounded-time, parity, claude-pilot-151, claude-pilot-250, claude-pilot-283]
applies_when: "a regex repeats a group of the form (?:B*SEP B*)+ (head and tail match the same chars), or a token that ends in B* is followed by a separator that starts with B*"
---

# A repeated separator whose head and tail both match blanks is a ReDoS

## Context

cpp#151 composed `_SANCTIONED_READONLY_FOR_LOOP_RE` in `permissions.py` from
named parts, to admit read-only `for` loops. `_bash_allow_is_chain_safe` runs it
whenever the policy returns `allow` for a Bash command. The statement separator
was:

```python
_FOR_STMT_SEP = r"(?:[ \t]*(?:;|\r?\n)[ \t]*)+"
_FOR_CMD = rf"{_FOR_CMD_WORD}(?:{_FOR_BLANK}+{_FOR_ARG})*{_FOR_BLANK}*"
```

## The trap

This shape has no nested `X+` inside `(...)*`, so the cpp#251 sweep, which
grepped for `(?:X+\s*)*`, missed it. The ambiguity comes from overlapping
boundaries:

- the repeated group starts and ends with `[ \t]*`, so a blank between two
  separators can be the tail of one repetition or the head of the next;
- `_FOR_CMD` ends with `{_FOR_BLANK}*` and is followed directly by the
  separator's `[ \t]*`, so the blanks before the first `;` also have two owners.

When the overall match fails (a `> out.txt` in the body, a `!`), the engine
tries every way of assigning the blanks: 2^n. A loop with indented blank lines
(`for f in a b; do\n  grep -n TODO "$f"\n` + `"  \n"`×n + `  wc -l "$f" > out.txt\ndone`)
took 921 ms at n=12 and more than 10 s at n=16 in `_bash_allow_is_chain_safe`.
The same regex with `" ;"`×n took 477 ms at n=20 and more than 10 s at n=40.

## The fix

```python
_FOR_STMT_SEP = r"(?:(?:;|\r?\n)[ \t]*)+"
```

Each repetition now starts on a `;` or a newline, which no blank can match, so
the boundary between repetitions is forced. Blanks before the first `;` belong
only to the preceding `_FOR_CMD`. Both repros at n=200 now take well under 1 ms.

The recognized language is unchanged. The constant is only used right after a
`_FOR_PIPELINE`, which ends in `_FOR_CMD`, which ends in `{_FOR_BLANK}*`, and
`{_FOR_BLANK}*[ \t]*` = `{_FOR_BLANK}*`. The compiled pattern differs from the
old one only by that substring. `tests/test_for_loop_redos.py` freezes the
old pattern and checks both against a generated corpus of 32,280 inputs
(5,139 positives, 27,141 negatives), with 0 divergences. It also runs both
repro forms at n=200 in a child process with a hard timeout, so a regression
fails the test instead of hanging the suite.

## Rule

Look for overlapping boundaries, not only nested quantifiers. For each repeated
group, ask whether the first character of an iteration can also be the last
character of the previous one. For each pair of adjacent tokens, ask whether
the end of one and the start of the next can match the same character. If
either is true and the match can fail later, the blanks have several owners
and the time can be exponential. Fix it by giving every character exactly one
owner, usually by anchoring each repetition on a mandatory character the
padding cannot match.

Measure rather than eyeball. The cpp#283 sweep parsed every pattern in
`tier1.py`, `permissions.py` and the bundled policy with `re._parser`, built a
witness prefix that reaches each candidate node, and timed growth in a
subprocess with a timeout. The old for-loop pattern was the positive control.
No other pattern was exponential. Three are quadratic on inputs with thousands
of blanks: `bash-mkdir`, `_SAFE_SED_PRINT_RE` and
`_EVAL_COMMAND_POSITION_RE`, each taking a few seconds at n=10,000. The plan
`docs/plans/2026-10-09-001-fix-283-for-loop-separator-redos-plan.md` lists
them.
