---
title: "A sed -i backup suffix is confined like bare -i — recognize the suffix, confine the backup, and keep the recognizer linear"
date: 2026-09-30
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, sed, sed-i, backup-suffix, lethality, survivable-deny, worktree-containment, redos, linear-recognizer, shlex, fail-closed, negative-control, claude-pilot-229, claude-pilot-245, claude-pilot-250, claude-pilot-253, mika-2601, mika-2565]
applies_when: "extending a lethality carve that recognizes an in-place editor flag (sed -i) to the flag's suffix/variant forms, when the variant also writes a sibling file that must itself be confined"
---

# A sed -i backup suffix is confined like bare -i

## Context

cpp#229/#245 (mika#2565) made a denied `sed -i` SURVIVABLE — the deny stays, but
`_denial_is_terminal` returns `False` — when the in-place edit lands on relative
source files that resolve inside the worktree and the script is pure
substitutions (single, multi-`;`, or multi-`-e`). The pilot then routes around
the refusal (falls back to the Edit tool) instead of dying on it.

The carve, `sed_i_confined_to_worktree` in `tier1.py`, is consulted ONLY by
`permissions._denial_is_terminal`. It never touches admission
(`is_tier3_dangerous`, `is_tier1_auto_approve`, the YAML rules) — the command
stays DENIED either way. It only ever flips terminal to survivable, never
refused to allowed. This fix extends that same carve; the decision-flip wiring
already existed, so nothing new was wired into `_denial_is_terminal`.

## The trap

cpp#229's in-place flag recognizer was `_SED_INPLACE_FLAG_RE =
re.compile(r"-[A-Za-z]*i[A-Za-z]*$")` — it matched only clusters whose every
character is an ASCII letter (`-i`, `-ni`, `-ir`). GNU sed also takes an
ATTACHED backup SUFFIX: `-i.bak` writes the edit in place AND saves the original
to `<file>.bak` beside it. The `.` in `.bak` is not a letter, so the recognizer
did not fullmatch, `saw_in_place` stayed `False`, `_sed_i_target_operands`
returned `None`, and the command fell back onto the lethal
`\bsed\s+(-\w*i|-i\w*)\b` pattern. Terminal.

mika#2601 (pilot `f2edcf8f`, 2026-09-30T14:28:33.718Z) died on:

```
sed -i.bak 's/^const PRODUCTION_ATTEMPTS: u32 = 3;$/const PRODUCTION_ATTEMPTS: u32 = 1;/' crates/mika-agent/src/task_engine/mod.rs && grep -n "^const PRODUCTION_ATTEMPTS" crates/mika-agent/src/task_engine/mod.rs
```

`-i.bak` edits `mod.rs` (a relative, in-worktree target) and writes its backup
`mod.rs.bak` beside it, also in the worktree. No out-of-worktree write, no
proven danger — yet terminal, purely because the suffix form was unrecognized.
MPC predicted this exact shape on 2026-09-29 ("`sed -i.bak` in the worktree is
TERMINAL, never covered").

## The fix

Two moves, both LETHALITY-only (admission byte-identical, the tier1 gate and the
egress axis untouched):

1. **Recognize the suffix forms, and capture the suffix.**
   `_SED_INPLACE_FLAG_RE` is replaced by `_sed_inplace_suffix(tok)`, a LINEAR
   scan: the flag is the FIRST `i` after the leading dash (GNU: `-i` consumes the
   rest of its clustered argument as the optional suffix), the leading cluster
   before it must be ASCII letters (the pre-#253 charset, other no-arg short
   flags), and the SUFFIX is everything after the `i` (`""` for bare `-i`).
   `_sed_i_target_operands` now returns `(files, suffix)`. Everything about the
   script/file split (shlex, `-f`/`--file` → `None`) and the substitution-only
   validation (`_sed_i_script_all_safe_subs`, `_SED_I_SUBST_UNIT_RE`) is #245,
   unchanged.

2. **Confine the backup, not just the edit.**
   `_sed_i_edit_and_backup_confined(target, suffix, cwd)` requires BOTH the
   edited target AND — for a non-empty suffix — the backup `target + suffix`
   (GNU appends a non-`*` suffix to the filename) to pass the unchanged
   `_sed_i_target_confined` (`is_within_project`, cwd/fs-aware, symlink-resolving;
   rejects absolute, `..`, `$`/`~`). A suffix that would project the backup out
   of the worktree (`.bak/../../etc/x`) fails `is_within_project`. A `*` in the
   suffix is a GNU wildcard (each `*` is replaced by the filename, which can send
   the backup to an arbitrary path); `_sed_inplace_suffix` rejects it up front,
   so such a token is never recognized as in-place and the segment stays terminal.

The per-file containment and the `rm_confined_to_pilot_scratch`-style remainder
re-check that keeps every mixed/chained shape terminal without a per-shape carve
are unchanged.

## Why the recognizer had to stay linear

The first attempt kept a regex — `-[A-Za-z]*?i(?P<suffix>[^*]*)$` — a lazy
leading class that splits on an `i`. On an adversarial token
(`-` + `ai`×N + `*`), the trailing `*` fails the `[^*]*$` tail, and the engine
retries the tail from every candidate `i` position: O(N²). Measured 5 ms at
N=1000, 1382 ms at N=16000 — quadratic, the exact class of hazard cpp#250 (the
`_SAFE_SED_PRINT_RE` ReDoS) had just closed. A deterministic scan
(`str.find` the first `i`, `str.isalpha` the lead, reject on `*`) is O(len) and
carries no backtracking: the same input is microseconds. When recognition can be
expressed as a single forward pass, prefer it to a quantifier that can be made to
backtrack — a lethality recognizer runs on attacker-influenced command strings.

## Why this is the right shape

Refracted from
[a-single-substitution-carve-is-defeated-by-a-multi-expression-sed-script](a-single-substitution-carve-is-defeated-by-a-multi-expression-sed-script.md):
a carve keyed on one syntactic form of a flag is defeated the first time a pilot
writes another valid form of it. `-i` and `-i.bak` are the same operation with
the same danger surface; the recognizer must cover the whole flag grammar GNU
accepts. And when a variant writes an EXTRA file (the backup), the containment
invariant must extend to that file too — confining the edit but not its sibling
would be a hole.

## Negative controls (must stay terminal)

- `sed -i.bak 's/a/b/' /etc/hosts` — out-of-worktree target.
- `sed -i.bak 's/a/b/w /etc/x' F` — `s///w` write flag in the script.
- `sed -i.bak 's/a/b/g; w /etc/x' F` — `w` write COMMAND after a `;`.
- `sed -i.bak '1r /etc/passwd' F` — `r` read command.
- `sed -i.bak/../../../../../../tmp/x 's/a/b/' F` — backup projected out of the worktree.
- `sed -i/tmp/* 's/a/b/' F` — `*` wildcard suffix, fail-closed at recognition.
- `sed -i.bak 's/a/b/' F ; rm -rf /` — dangerous verb after an unquoted `;`.
- `sed -i.bak 's/a/b/' F /etc/passwd` — mixed operand list, one out of worktree.
- absolute / `$HOME` / `~` / `..` / outbound-symlink targets, unresolvable cwd.
