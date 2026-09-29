---
title: "A single-substitution carve is defeated by a multi-expression sed script — the script is one shlex argument, validate it whole, do not hand-parse it to find the file"
date: 2026-09-30
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, sed, sed-i, lethality, survivable-deny, worktree-containment, multi-expression, shlex, fail-closed, negative-control, claude-pilot-229, claude-pilot-236, claude-pilot-243, mika-2482, mika-2565]
applies_when: "carving the LETHALITY of a denied verb whose argument is an interpreter script (sed, awk) that can hold several commands in one quoted operand or across several -e flags"
---

# A single-substitution carve is defeated by a multi-expression sed script

## Context

cpp#229 (mika#2565, case B) made a denied `sed -i` SURVIVABLE — the deny stays,
but `_denial_is_terminal` returns `False` — when the in-place edit lands on a
relative source file that resolves inside the worktree. The pilot then routes
around the refusal (falls back to the Edit tool) instead of dying on it.

The carve, `sed_i_confined_to_worktree` in `tier1.py`, is consulted ONLY by
`permissions._denial_is_terminal`. It never touches admission
(`is_tier3_dangerous`, `is_tier1_auto_approve`, the YAML rules) — the command
stays DENIED either way. It only ever flips terminal to survivable, never
refused to allowed.

## The trap

cpp#229's target extractor, `_sed_i_target_operands`, validated the FIRST
positional operand against a regex, `_SED_I_SAFE_SUBST_SCRIPT_RE`, that matched
exactly ONE substitution and was anchored `^…$`. It also bailed to `None`
(fail-closed, stays terminal) the moment it saw `-e`/`--expression`.

A real pilot script is almost never a single substitution. mika#2482 (pilot
`23311c35`, 21:24:39Z, the 7th lethal death of 2026-09-29) died on:

```
sed -i 's/merged_pr(1900, branch, 60)/…/g; s/merged_pr(1900, "feat\/1888\/research", 60)/…/g; s/…' <worktree file>
```

Three `s///g` substitutions separated by `;`, inside ONE single-quoted argument,
with escaped separators (`\/`) and embedded double-quotes. That whole argument
is one token, but it is not a single substitution, so the `^…$`-anchored regex
did not match, the extractor returned `None`, and the command fell back onto the
lethal `\bsed\s+(-\w*i|-i\w*)\b` pattern. Terminal. The same family, replayed a
factor at a time (cpp#243):

| form | terminal, before |
|---|---|
| `sed -i 's/a/b/g' F` | False (cpp#229 carve) |
| `sed -i 's/a/b/g; s/c/d/g' F` | **True** |
| `sed -i 's/a/b/g;s/c/d/g' F` | **True** |
| `sed -i -e 's/a/b/g' -e 's/c/d/g' F` | **True** |

`_split_compound_command` was NOT at fault: it correctly keeps the quoted `;`
inside one segment. The defect was entirely inside `_sed_i_target_operands` —
its single-substitution assumption and its blanket `-e` bail.

## The fix

Two moves, both LETHALITY-only (admission byte-identical, the tier1 gate
untouched):

1. **Separate the script from the files with shlex, not with a script parse.**
   The sed script is ONE shlex argument regardless of its internal `;`, `\/` or
   `"…"`; several `-e`/`--expression` flags each contribute one script argument.
   `_sed_i_target_operands` now collects the `-e` scripts explicitly, and in the
   bare form takes the first positional as the script — the remaining
   positionals are the FILE operands. It never hand-parses the script body to
   find where the script ends and the files begin (the exact move that `\/` and
   `"…"` defeat).

2. **Validate the whole script as substitutions only, with a forward scanner.**
   `_sed_i_script_all_safe_subs` consumes `_SED_I_SUBST_UNIT_RE` matches from the
   front; after each unit the remainder must be empty or a `;` followed by
   another unit. A `;` inside a PATTERN or REPLACEMENT is never reached by the
   scanner, because the unit regex has already consumed the substitution up to
   its closing separator and flags. Every collected script (positional and each
   `-e`) must pass. The flag charset stays `[gpiImM0-9]` — no `w`/`W` write, no
   `e` exec — so a trailing `w FILE` write ends the unit and the leftover `w …`
   matches no further unit, staying terminal.

The containment check per file (`_sed_i_target_confined` -> `is_within_project`,
cwd/fs-aware, symlink-resolving; reject absolute, `..`, `$`/`~`) is unchanged,
as is the `rm_confined_to_pilot_scratch`-style remainder re-check that keeps
every mixed/chained shape terminal without a per-shape carve.

## Why this is the right shape

The general rule, refracted from
[a-quoted-redirect-char-in-a-commit-trailer-is-not-a-redirection](a-quoted-redirect-char-in-a-commit-trailer-is-not-a-redirection.md):
when a denied verb's argument is an interpreter script, the split between "the
script" and "the files" belongs to the shell's own tokeniser (shlex), never to a
regex that peers inside the script. What must peer inside the script is the
SAFETY validator, and it must validate the whole thing, not just its first
command. A carve keyed on "one substitution" is defeated the first time a real
pilot writes two.

## Negative controls (must stay terminal)

- `sed -i 's/a/b/w /etc/evil' F` — `w` write flag targeting outside the worktree.
- `sed -i 's/a/b/g; w /etc/x' F` — `w` write COMMAND after a `;`.
- `sed -i -e 's/a/b/g' -e 's/c/d/w /tmp/x' F` — `w` in a later `-e`.
- `sed -i 's/a/b/g; s/c/d/g' /etc/hosts` — multi-sub but absolute target.
- `sed -i -f script.sed F` — external script file, uninspectable, fail-closed.
- `sed -i '…' F ; rm -rf x` — dangerous verb after an unquoted `;`.
- `sed -i '…' F /etc/passwd` — mixed operand list, one out of worktree.
- absolute / `$HOME` / `~` / `..` / outbound-symlink targets, unresolvable cwd.
