---
title: "A sed -i suffix form writing to /dev/null is the cpp#203 x cpp#255 intersection — carry the inert-sink axis across the suffix parser, guard the suffix as a write vector"
date: 2026-10-02
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: medium
tags: [permissions, policy, bash, sed, sed-i, dev-null, inert-sink, backup-suffix, lethality, survivable-deny, worktree-containment, shlex, redirect-token, fail-closed, negative-control, claude-pilot-203, claude-pilot-253, claude-pilot-255, claude-pilot-271, mika-2624]
applies_when: "two lethality carves each cover one axis of the same operation (an inert target, and a flag's suffix forms) but neither covers the other axis's value, leaving the intersection terminal"
---

# A sed -i suffix form writing to /dev/null is the cpp#203 x cpp#255 intersection

## Context

Two independent lethality carves already exist for `sed -i`, each consulted ONLY
by `permissions._denial_is_terminal` (admission is never touched — the command
stays DENIED either way; only terminal flips to survivable):

- **cpp#203** (`_SED_I_DEVNULL_RE`, inside `is_tier3_dangerous_for_lethality`):
  `sed -i <script>? /dev/null` is survivable because `/dev/null` is a
  kernel-owned inert sink — the in-place edit writes nothing durable. But the
  regex requires whitespace immediately after the flag, so it matches only the
  BARE `-i` form; `-i.bak` has no whitespace and does not match.
- **cpp#253/#255** (`sed_i_confined_to_worktree`, the shlex-based suffix parser):
  a `sed -i<SUFFIX>` (`-i.bak`, `-i~`) is survivable when every file target
  resolves inside the worktree AND the `<target><suffix>` backup does too. But
  `/dev/null` is an absolute path, so `is_within_project` rejects it — the
  worktree carve never covers it.

So each carve covers one axis (inert target; suffix form) only for the other
axis's narrow value (bare `-i`; worktree target). Their INTERSECTION —
`sed -i.bak … /dev/null` — fell through both and stayed terminal.

## The trap

mika#2624 (pilot `94770602`, 2026-10-01T19:20:31Z, 66 turns, $19.52) died
probing a multi-line regex it never intended to write. The verbatim killer, line
2 of a 3-line script:

```
sed -i.bak 's|^        for (rel, content) in production_sources() {\n            if HOLD_CLASSIFICATION|XX|' /dev/null 2>/dev/null; true
```

The refusal is legitimate (the script is still DENIED). But it was TERMINAL, and
the session died — purely because the SUFFIX x INERT-TARGET combination sat in
the gap between cpp#203 (bare `-i` only) and cpp#255 (worktree targets only).

Replay at source HEAD 406983c (`_denial_is_terminal`):

| command | verdict |
|---|---|
| `sed -i 's\|a\|XX\|' /dev/null` (cpp#203) | survivable |
| `sed -i.bak 's/a/b/' crates/x.rs` (cpp#255) | survivable |
| `sed -i.bak 's\|a\|XX\|' /dev/null` | **TERMINAL** ← the gap |

## The fix

One move, LETHALITY-only (admission byte-identical, the tier1 gate and the
egress axis untouched), inside the existing `sed_i_confined_to_worktree` — NOT a
parallel recognizer. The cpp#255 suffix parser (`_sed_i_target_operands`, which
already splits the suffix off `-i.bak`/`-i~` and proves the script is a pure
substitution) now carries the cpp#203 `/dev/null` axis:

1. **A SOLE `/dev/null` target is inert for any benign suffix.** When a segment's
   only real file target is `/dev/null`, it is survivable regardless of the `-i`
   suffix form. No cwd resolution — `/dev/null` is a kernel device, never
   resolved on disk. SOLE-target mirrors cpp#203's own constraint (`/dev/null`
   must be the only file operand), so a mixed list stays byte-identical to
   pre-cpp#271: `/dev/null` is not treated inert there, and the per-target
   confinement rejects it as absolute. cpp#203 (bare `-i`) and cpp#255 (suffix +
   worktree) behaviour are both unchanged — this only ADDS the intersection.

2. **The suffix must not become a write vector.** GNU sed writes the backup by
   appending the suffix to the filename (`/dev/null` + suffix), so
   `_sed_i_suffix_is_benign_backup` requires a no-slash/no-`..` suffix. `.bak`,
   `~`, empty are benign; `-i../../etc/x`, `-i/tmp/x`, `-i..` are rejected and
   stay terminal (fail-closed). The `*` wildcard is already excluded upstream by
   `_sed_inplace_suffix`.

## Why the redirect-token filter is needed, and why it is safe

The verbatim's sed segment ends `… /dev/null 2>/dev/null`. `shlex.split` does
not model shell redirects, so it mis-tokenizes `2>/dev/null` as a SECOND
positional — making the target list `['/dev/null', '2>/dev/null']`, not the sole
`['/dev/null']` the carve needs. `_SED_REDIRECT_TOKEN_RE` (`^\d*[<>]`, anchored,
linear) filters redirect tokens before the sole-target test. This is safe
because a REAL out-of-worktree redirect target is independently re-armed by
`_denial_is_terminal`'s redirect/destination veto, which runs on the FULL
command AFTER this carve: `sed -i.bak 's/a/b/' /dev/null >/etc/passwd` is carved
here but the `>/etc/passwd` redirect keeps the denial terminal downstream. A
benign `2>/dev/null` stderr-silencer passes that veto and stays survivable.

## Why this is the right shape

Refracted from
[a-single-substitution-carve-is-defeated-by-a-multi-expression-sed-script](a-single-substitution-carve-is-defeated-by-a-multi-expression-sed-script.md)
and [a-sed-i-backup-suffix-is-confined-like-bare-i](a-sed-i-backup-suffix-is-confined-like-bare-i.md):
when two carves grow along orthogonal axes of the same operation, their
intersection is the uncovered cell, and a pilot will land on it. The fix is not a
third recognizer but teaching ONE of the existing parsers to carry the other's
axis value — here the suffix parser carries cpp#203's `/dev/null` sanction,
reusing `_sed_i_target_operands` and the sole-operand constraint verbatim. Adding
a value to an existing axis is safer than forking a parallel classifier that can
drift.

## The one thing out of scope

The ticket lists `sed --in-place=/tmp/../etc/ … /dev/null` as a negative that
must stay terminal. On HEAD it is ALREADY survivable — not via any `/dev/null`
carve, but because the long `--in-place` form is not matched by `TIER3_PATTERNS`
at all, so the whole command is never tier3-dangerous and `_denial_is_terminal`
short-circuits. `_sed_inplace_suffix` does not recognize `--in-place` either, so
this carve never touches it; its behaviour is unchanged by this fix. Making the
long form terminal would require ADDING `--in-place` to the tier3 denylist — an
ADMISSION change, outside cpp#271's lethality-only scope and its byte-identical
invariant. Flagged to MPC as a separate question.

## Negative controls (must stay terminal)

- `sed -i.bak 's/a/b/' /etc/passwd` — real out-of-worktree target.
- `sed -i.bak 's/a/b/' ../../x` — `..` traversal target.
- `sed -i../../etc/x 's|a|XX|' /dev/null` — suffix carries `/` and `..`.
- `sed -i/tmp/x 's|a|XX|' /dev/null` — suffix carries `/`.
- `sed -i.. 's|a|XX|' /dev/null` — suffix is `..` (no slash) — still a write vector.
- `sed -i.bak 's/a/b/' /dev/null /etc/passwd` — second target out of worktree.
- `sed -i.bak 's/a/b/' /dev/null src/x.rs` — mixed list: `/dev/null` not the sole operand, so not inert (cpp#203 constraint, byte-identical to HEAD).
- `sed -i.bak 's/a/b/' /dev/null >/etc/passwd` — real redirect re-armed by the full-command destination veto.
- `sed -i.bak 'y/a/b/' /dev/null` — non-substitution script, fail-closed.
- `sed -i.bak 's/a/b/w /etc/x' /dev/null` — `w` write flag in the script, fail-closed.
- `sed -i.bak 's|a|XX|' /dev/null && rm -rf /etc` — chained destructive verb.
