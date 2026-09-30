---
title: "A nested quantifier in the sed-print whitelist is a ReDoS — anchor each operand on leading whitespace; a restricted character class before an out-of-class char is the blow-up, an unrestricted \\S is not"
date: 2026-09-30
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, sed, sed-print, redos, catastrophic-backtracking, nested-quantifier, regex, denial-of-service, bounded-time, parity, fail-closed, claude-pilot-189, claude-pilot-190, claude-pilot-250, mika-2601]
applies_when: "an operand/argument list in a whitelist regex is written (?:CLASS+\\s*)* — a nested quantifier whose inner class is RESTRICTED, so an out-of-class trailing char (a redirect >) forces the engine to retry every split of a long token"
---

# A nested quantifier in the sed-print whitelist is a ReDoS

## Context

cpp#189/#190 gave the classifier a narrow read-only carve,
`_is_safe_sed_print_only` in `tier1.py`, that recognizes
`sed -n '<addr>[,<addr>]p' [FILE...]` — a print-only sed — so research chains
(`echo … && sed -n … && grep …`, `sed -n … | cat`) stop routing to the relay.
The file-operand tail was written:

```python
_SAFE_SED_PRINT_RE = re.compile(
    rf"^\s*sed\s+-n\s+'{_SED_ADDR}(?:,{_SED_ADDR})?p'\s*(?:[A-Za-z0-9_./-]+\s*)*$"
)
```

The predicate is consulted on the lethality path
(`_blank_print_only_sed_segments` <- `is_tier3_dangerous_for_lethality` <-
`_denial_is_terminal`) and on the admission chain check. Both run the regex on
whatever Bash command the pilot requested.

## The trap

`(?:[A-Za-z0-9_./-]+\s*)*` is a nested quantifier: `X+` inside `(...)*`, with
the separator `\s*` able to match the empty string. When a path is followed by a
character OUTSIDE the class `[A-Za-z0-9_./-]` — the `>` of a redirection, a `|`,
a `$` — the tail can no longer match, and the engine retries every way of
cutting the path into `[A-Za-z0-9_./-]+` sub-tokens, each with an empty `\s*`
between them. The time is exponential in the path length.

A pilot (mika#2601, `01c5c3ef`, 2026-09-30) requested:

```
sed -n '1139,1216p' crates/mika-agent/src/task_engine/mod.rs > .pilot-scratch/probe-block.txt
```

The ~40-char path plus the `>` redirection is exactly the trigger. The regex
never returned: the process sat in state R, **41 minutes of CPU** on the main
thread, and the SDK loop hung forever. Measured at the source (path of N chars
+ ` > g`):

| N | time on HEAD |
|---|---|
| 16 | 3.2 ms |
| 20 | 53.8 ms |
| 22 | 199 ms |
| 24 | 798 ms |
| 26 | 2.97 s |
| 28 | 11.7 s |
| ~40 (pilot) | > 20 s (hang) |

~4x per +2 chars — textbook catastrophic backtracking.

## The fix

One line. Anchor each operand repetition on a leading `\s+`:

```python
_SAFE_SED_PRINT_RE = re.compile(
    rf"^\s*sed\s+-n\s+'{_SED_ADDR}(?:,{_SED_ADDR})?p'\s*(?:\s+[A-Za-z0-9_./-]+)*\s*$"
)
```

Now every operand must begin with `\s+`. A run of non-whitespace can be
consumed exactly one way, so the ambiguity that fed the exponential retry is
gone. Matching is linear: the pilot's command resolves in ~2.4 µs, a 200-char
path + `>` in ~7.7 µs.

This is hardening, not a decision change. The regex is the ONLY source line
touched; no caller is modified, so `is_tier3_dangerous`,
`is_tier3_dangerous_for_lethality`, `_denial_is_terminal` and the tier1 gate
`is_tier1_auto_approve` are byte-identical by construction. Decision parity was
proven on the MPC 11-case set (HEAD-regex vs fixed-regex, same bool on every
case) and on a 21,263-input fuzz corpus with zero divergence. A bounded-time
regression test (`tests/test_tier1.py`, 50 ms budget) pins the linearity across
all four axes.

## Why this is the right shape

The blow-up needs two things: a nested quantifier `(?:X+sep)*` where `sep` can
be empty, AND a way for matching to FAIL after the group has consumed a long
run, so the engine backtracks through every split. The failure came from the
RESTRICTED class: `[A-Za-z0-9_./-]` does not include `>`, so a trailing `>`
cannot be absorbed and cannot end the string — the engine must retry.

The sibling `_SAFE_SED_SUB_RES` patterns (`s///` substitution forms) share the
same `\s*(?:\S+\s*)*$` shape but are NOT vulnerable, because `\S` is
UNRESTRICTED: every trailing char (`>`, `|`, `$`) is absorbed into `\S+`, the
operand group always succeeds, and there is no class-boundary failure to
backtrack on. Stress-verified: a 100,000-char operand with a `>`/`|` tail
matches in under 0.5 ms, linear.

So the rule is not "never write `(?:X+\s*)*`" — it is: a nested quantifier over
a RESTRICTED character class, followed by anything, is a ReDoS the moment a real
input carries an out-of-class char; anchor each repetition on a mandatory
separator so the token can only be cut one way. The audit of cpp#250 found
`_SAFE_SED_PRINT_RE` was the only pattern of this class in `tier1.py` /
`permissions.py`.

## Parity (must stay byte-identical)

Positives still recognized: `sed -n '1p' f`, `sed -n '1,5p' path/to/file`,
`sed -n '$p' a b c`, `sed -n '/re/p' file`, `sed -n '/a/,/b/p' x`,
`sed -n '10,20p'` (no file), trailing spaces, multiple operands.

Negatives still rejected: `sed -n '1,2p' f > g` (redirection),
`sed -n '1p' f; sed -n '2p' g` (second sed), `sed -n '1p' $(id)` (shell
metachar), `sed -i '1p' f` (wrong flag), `sed -n '1,20p;w evil' file` (write
command), `sed -n '1p' f | cat` (pipe).
