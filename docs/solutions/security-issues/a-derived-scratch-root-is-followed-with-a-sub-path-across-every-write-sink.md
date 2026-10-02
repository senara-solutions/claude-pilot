---
title: "A derived-scratch root is followed with a sub-path across EVERY write sink, not just the redirect"
date: 2026-10-02
last_updated: 2026-10-02
module: claude_pilot.tier1
component: permission-classifier
problem_type: design_decision
category: security-issues
severity: medium
tags: [permissions, policy, lethality, denial, mkdir, cp, mv, mv-source-is-a-write, chmod, touch, redirect, scratch, pilot-scratch, mktemp, derived-scratch, sub-path, last-wins, reads-only, fail-closed, claude-pilot-209, claude-pilot-211, claude-pilot-218, claude-pilot-265, claude-pilot-270, claude-pilot-272, claude-pilot-273, claude-pilot-279, claude-pilot-280, mika-2631]
applies_when: "a pilot writes to a variable-carried derived-scratch target WITH a sub-path tail through a sink other than the redirect (mkdir -p, cp/mv destination)"
---

# A derived-scratch root is followed with a sub-path across EVERY write sink, not just the redirect

## Context

cpp#272 taught the lethality resolver the repo-prescribed `.pilot-scratch/` root
(joining `/tmp` and `$(mktemp -d)`). But it closed the hole at only two of the
write sinks, and INCONSISTENTLY: the redirect sink followed a derived-scratch
variable WITH a sub-path (`> "$D/a.txt"`) because it used the suffix-aware
`_MKTEMP_SCRATCH_TARGET_RE`; the `mkdir` sink followed the variable only BARE
(`mkdir -p "$D"`) because it used the bare-only `_CE_SCRATCH_VARREF_RE`; and
`cp`/`mv` had no derived-scratch carve at all (only the cpp#209 mktemp-var carve).
cpp#272's own plan named this residue: "the mkdir transitive sink stays
bare-`$VAR`; a `$R/sub` suffix is out of scope."

Pilot 4617da8f (mika#2631) hit exactly that residue — recopying a directory tree
under `.pilot-scratch/`, the natural shape of the prescribed idiom:

```
FAL=.pilot-scratch/falsify
mkdir -p "$FAL/skills/bundled/_shared/tests"   # ← terminal
cp skills/bundled/_shared/dispatch-lib.sh "$FAL/skills/bundled/_shared/"
```

Same derived root `$FAL`, survivable through the redirect, terminal through
`mkdir` and `cp`. Following the repo's own scratch instruction killed the pilot.

## Guidance

### 1. One shared sink resolver — the redirect twin WAS already it

Do not fork a per-sink predicate. The redirect twin
(`_is_transitive_ce_scratch_redirect_target`) already had the correct shape:
match `_MKTEMP_SCRATCH_TARGET_RE` (`$VAR`/`${VAR}` + optional surrounding quote +
a `[\w./-]*` tail), reject a `..` tail, resolve the var LAST-WINS reads-only
(`_last_real_assignment_value` → `_live_scratch_source`), test the root with
`_value_roots_at_scratch`. Promote that body to a shared
`_is_transitive_ce_scratch_sink_target(command, dest, cwd)` and have the redirect,
`mkdir` and `cp`/`mv` sinks all follow it. Stripping the `/<sub-path>` tail off the
operand before resolving the var is the WHOLE fix — the root recognizers, the
reassignment rule, and the containment all come along unchanged, so every negative
the resolver already enforced (reassignment-out in any form, a `..` tail, a
non-scratch root, a link creator in the `.pilot-scratch` branch) is inherited for
free across all sinks at once.

### 2. Keep the bare-ref branch for the subshell `)` artifact

`mkdir`'s existing bare predicate matched `$VAR` and the subshell-split artifact
`$VAR)` (a `(subshell; mkdir -p "$D")` split leaves a trailing `)`). The shared
`_MKTEMP_SCRATCH_TARGET_RE` charset does not admit `)`. So UNION rather than
replace: the mkdir predicate returns True if EITHER the bare `_CE_SCRATCH_VARREF_RE`
ref OR the shared sub-path shape roots at scratch. This keeps all 25 pre-existing
mkdir-predicate unit tests green (including the `$RUN_DIR)` artifact) while adding
the sub-path shape.

### 3. For `cp`, the DESTINATION is the containment axis — but NOT for `mv`

`_extract_cp_mv_destination` already resolves the write target to the
`-t`/`--target-directory` value or the LAST positional operand. Carve on THAT:
`cp x.sh "$FAL/"` (dest `$FAL/`, trailing slash, matches the suffix regex) and
`cp a "$FAL/sub/"` copy INTO scratch and are survivable, while `cp x "$D/a" /etc/`
has FINAL dest `/etc/` — which is not scratch, fails the resolver, and falls to the
cpp#38 containment veto → stays TERMINAL. For `cp` the SOURCE is a read, not the
containment axis: `cp /etc/passwd "$D/"` reads an out-of-tree file and writes it
INTO scratch — survivable (the only byte written lands under `.pilot-scratch`;
reading /etc/passwd is a read, broadly allowed, not a write-breach). This is the
explicit cpp#279 AC3 decision; no real write escapes scratch. The cpp#211 `$`/`~`
cp/mv veto still fires for every OTHER `$`-rooted destination because the carve is
checked just before it.

#### 3a. The `mv` source is a WRITE — require every source contained too (cpp#280)

The "destination is the containment axis" rule is CORRECT for `cp` but WRONG for
`mv`, and the MPC gate (head `86718366`) caught exactly this: `mv` DELETES its
source, so the source is itself a WRITE. `mv /etc/passwd "$D/"` does not merely
read `/etc/passwd` — it MOVES it (removes it from `/etc`), a system-file mutation
the destination-only carve wrongly made survivable. The fix
(`_mv_has_escaping_source`, verb-scoped via the segment's leading word): for `mv`
ONLY, the derived-/mktemp-/literal-scratch destination carve fires only when EVERY
source operand is ALSO contained — rooted at a recognized scratch (the SAME
`_is_transitive_ce_scratch_sink_target` / `_is_mktemp_scratch_redirect_target` /
`_is_sanctioned_tmp_scratch` the destination uses) OR a clean worktree-relative
operand (not absolute, not `$`/`~`-rooted, no `..` component). `cp` is byte-identical
to cpp#279 (the guard is a no-op for any non-`mv` segment).

Two subtleties make the source test LEXICAL rather than an `is_within_project`
resolve: (i) in a torn-down worktree (`Path(cwd).resolve(strict=True)` raising —
the cpp#209 incident window) `is_within_project` fails closed to `False` for EVERY
path, which would both mask an out-of-tree source and wrongly terminalize a
legitimate relative one, so the ratified cpp#209 positive `mv crates/x.rs "$D/"`
must stay survivable in BOTH cwd worlds; (ii) a clean relative source always names
something inside the worktree, and `mv` relocates a symlink ENTRY rather than
following it, so its deletion is always in-envelope — the same "contained by
accident" posture the cpp#209 `$VAR`-destination carve relies on. Both carve sites
(the cpp#209 mktemp-var carve and the cpp#279 derived-scratch carve) gain the guard,
so `D=$(mktemp -d); mv /etc/passwd "$D/"` is terminal through either path.

### 4. Make `$(mktemp -d)` consistent across sinks too

`_value_roots_at_scratch` does not itself recognize a `$(mktemp -d)` value — that
root is recognized by the SEPARATE `_is_mktemp_scratch_redirect_target` (via
`_mktemp_scratch_variable_names`), which the cp and redirect sinks already OR in.
The mkdir sink did not, so `D=$(mktemp -d); mkdir -p "$D/x"` was terminal while the
same through cp/redirect was survivable — the same "inconsistent across sinks"
class. Add the mktemp-var OR to the mkdir sink so every sink treats every derived
root identically. This is alignment, not a new perimeter: the mktemp root was
already carved at the other sinks.

### 5. `chmod`/`touch` are NOT write-kinds — leave them, do not wire them

`_segment_write_kind` classifies only `cp`/`mv`, `mkdir`, `git show >`, and
redirects. `chmod` and `touch` are unclassified, so `_destination_veto_reason`
never sees them and `_denial_is_terminal` leaves them SURVIVABLE for EVERY
destination already — in-scratch, in-worktree, or out. The cpp#279 corpus requires
them survivable for derived-scratch sub-paths, which is already true. Wiring them
as new write-kinds to "carve scratch" would be a net REGRESSION: it would first
make every out-of-scratch `chmod`/`touch` terminal (and change the refusal axis),
then carve scratch back — a perimeter expansion onto a sink the carve never
covered. That is an admission question, not a lethality fix, and is bounded out of
this ticket. (`chmod -R`/`chown -R` ARE terminal, but via the cpp#205 dangerous-verb
set, not a destination veto — the verb axis is out of scope here.)

### 6. Flip lethality, never admission

Everything new is reached ONLY under `for_lethality`: the shared predicate, the
mkdir union, the new cp clause, and the mkdir mktemp OR all live inside
`permissions._destination_veto_reason`'s `for_lethality`-gated clauses. So
`is_tier1_auto_approve`, `is_tier3_dangerous`, every YAML rule, egress, and
`_destination_veto_reason(for_lethality=False)` are byte-identical to the served
code. A survivable deny is still a deny. Pin this with a dedicated
admission-identity test and an empirical broad-sample diff against `1e8e28b` (0
diffs, 336 commands crossing every root × sink × form).

## References

- Code: `src/claude_pilot/tier1.py` (`_is_transitive_ce_scratch_sink_target` —
  the shared suffix-aware sink resolver, promoted from the redirect twin's body;
  `_is_transitive_ce_scratch_redirect_target` now delegates to it;
  `_is_transitive_ce_scratch_mkdir_target` unions the retained bare
  `_CE_SCRATCH_VARREF_RE` branch with the shared predicate),
  `src/claude_pilot/permissions.py` (the `bash-mkdir` `for_lethality` clause ORs
  the mktemp-var carve; a NEW `bash-cp-mv` `for_lethality` clause follows the
  shared sink resolver on the destination, before the cpp#211 `$`/`~` veto;
  cpp#280 adds `_mv_has_escaping_source` + `_extract_cp_mv_sources`, which gate
  BOTH cp/mv for_lethality carves so a `mv` with an out-of-envelope source — a
  WRITE, since `mv` deletes the source — stays terminal while `cp` is unchanged).
- Siblings: cpp#272 (`.pilot-scratch/` root — this closes its named residue),
  cpp#273 (link-creator fail-closed guard — kept, inherited through the shared
  resolver), cpp#209 (cp mktemp-var carve), cpp#211/#218 (`$`-rooted cp/mkdir
  vetoes this carve defers for a derived-scratch dest), cpp#265/#266 (transitive
  LAST-WINS), cpp#270/#268 (inverted reads-only reassignment rule). Doctrine
  cpp#38/#213 (worktree-relative containment), cpp#143 (never resolve to GRANT),
  cpp#205 (default survivable).
- Plan: `docs/plans/2026-10-02-006-fix-279-scratch-subpath-all-sinks-lethality-plan.md`.
- Ticket: cpp#279 / mika#2631 (pilot 4617da8f, died tour 36, rescue PR#2637).
  Completion of the ratified `.pilot-scratch`/mktemp//tmp derived-scratch carve.
