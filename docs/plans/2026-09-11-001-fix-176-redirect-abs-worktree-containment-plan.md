---
issue: claude-pilot#176
title: "REGRESSION #155 — an absolute-worktree redirect target is vetoed fail-closed, terminal - Plan"
type: fix
scope_repo: claude-pilot
priority: p1-security
date: 2026-09-11
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# REGRESSION #155 — an absolute-worktree redirect target is vetoed fail-closed, terminal - Plan

## Goal Capsule

**Objective.** PR#173 (cpp#155) taught `_destination_veto_reason`
(`permissions.py`) to classify a shell redirect from ANY verb as write-kind
`bash-redirect`, closing the gap where `echo hi > /etc/passwd` was invisible
to the containment veto. Its containment check for that write-kind accepts a
redirect destination ONLY if it is LEXICALLY under `/tmp/` or
worktree-RELATIVE (`_is_contained_redirect_target`). An ABSOLUTE path that
nonetheless RESOLVES inside the pilot's own worktree satisfies neither
exception, so it is denied fail-closed — and, because a destination veto is
the ONE denial class cpp#128 keeps unconditionally `interrupt=True`
(terminal), the pilot's whole run halts. Builds routinely redirect to an
absolute path under their own worktree
(`/usr/bin/time -v cargo build --release --features telemetry --bin
mika-spirit > /data/workspace/mika-platform/.claude/worktrees/fix-1719-.../
out`); this killed mika#1719 (session `4667a1c2`, 2026-09-10 21:16,
`error_during_execution:after_deny`, zero commits).

Fix `_destination_veto_reason`'s `bash-redirect` containment so an absolute
target that RESOLVES within the worktree is also accepted — routed through
the SAME cpp#38 `is_within_project` symlink-aware resolution the `cp`/`mv`/
`mkdir`/`git show` write-kinds already use for their own destinations —
without touching the `/tmp/` exception's deliberately LEXICAL nature
(cpp#143/#150/#155) and without loosening anything else: a target outside the
worktree and not under `/tmp/`, `../escape`, `~/x`, `$VAR/x`, and a symlink
that resolves outside the worktree, all stay vetoed and terminal.

**Means.** Extract the disqualifier half of `_is_contained_redirect_target`
(`~`/leading-`$`/`..`/bad-charset/bare-`$`) into its own predicate,
`_is_lexically_disqualified_redirect_target` (`tier1.py`), reused by BOTH
`_is_contained_redirect_target` (unchanged behavior, now just this predicate
plus its own `/tmp/`-prefix literal) and `_destination_veto_reason`
(`permissions.py`), which now checks the disqualifier FIRST — vetoing
immediately, before any resolve, for exactly the set that must never reach
`is_within_project` — and, for everything else (worktree-relative, OR
absolute-but-not-`/tmp/`-and-not-disqualified), falls through to the existing
`is_within_project` + control-plane checks it already runs for `cp`/`mv`/
`mkdir`/`git show` destinations.

**Authority hierarchy.** `senara-solutions/claude-pilot#176` body (already
diagnosed at source, confirmed independently below) > this plan > implementer
judgment.

**Stop conditions.** None that halt outright. The one non-negotiable
constraint: cpp#143/#150/#155's tests (the `/tmp/` lexical exception, the
`mkdir` scratch exception, the redirect classification itself) must stay
green, UNCHANGED — see § Does Not Reopen #143/#150/#155.

**Execution profile.** Two source files (`permissions.py`, `tier1.py`), one
test file (`tests/test_permissions.py`, 4 new tests). Single repo,
`claude-pilot` only.

**Tail ownership.** Implementer opens the PR through green CI (gated, no
merge — MPC reviews a security-boundary change and redeploys).

## Measurement — confirmed at source, before and after

Probed with the real decision function, against a real worktree dir NOT
under `/tmp` (so the probe cannot be accidentally satisfied by the unrelated
`/tmp/` lexical exception — see the note on this in
`tests/test_permissions.py`'s cpp#176 section):

| commit | `_segment_write_kind` (redirect seg) | `_destination_veto_reason(cmd, cwd=worktree)` |
|---|---|---|
| `d9f9254` (deployed, pre-fix) | `bash-redirect` | `"redirect destination '<abs-worktree-path>' is not a literal contained path — not lexically under /tmp/ or worktree-relative (denied fail-closed)"` |
| `c5fd3f7` (pre-#155) | `None` | `None` (unclassified — different, non-terminal deny path) |
| this fix (post-#176) | `bash-redirect` | `None` |

`d9f9254` confirms the issue's own diagnosis exactly: the ticket's proof and
this plan's independent replay agree.

## Decisions

### D1 — Extract the disqualifier set, don't fork it

`_is_contained_redirect_target`'s existing lexical checks split cleanly into
two questions: (a) is this text disqualified OUTRIGHT — `~`/leading-`$`/`..`/
bad charset/bare-`$` — regardless of any `cwd`; (b) if not, is it also
literally `/tmp/`-prefixed (when absolute) or bare-relative. `_destination_
veto_reason`'s new branch needs to ask ONLY (a) before deciding whether to
resolve — asking the OLD combined function would still veto an absolute
in-worktree path outright (that IS the bug). Extracting (a) into
`_is_lexically_disqualified_redirect_target` and rebuilding `_is_contained_
redirect_target` on top of it (`disqualified? -> False : (absolute? -> /tmp/
prefix : True)`) keeps `_is_contained_redirect_target`'s own observable
behavior byte-for-byte identical — pinned by the pre-existing `TestTier1...`
assertions in `tests/test_tier1.py:1377-1389`, unchanged and still green — so
every OTHER caller of that function (`_redirect_destination_veto_reason`,
`is_tier3_dangerous_for_lethality`) is provably unaffected. This is the
"reuse the existing trusted mechanism" instruction from the ticket, applied
literally: no new regex, no new charset, the exact same disqualifier text.

### D2 — The `/tmp/` exception stays lexical; only the FALL-THROUGH changes

`_destination_veto_reason`'s `bash-redirect` branch, in order: (1) `/dev/null`
skip (cpp#130, unchanged), (2) disqualifier veto (cpp#154 D3, unchanged
semantics, now sourced from the extracted predicate), (3) `dest.startswith
("/tmp/")` skip (cpp#143/#150/#155, unchanged — still a literal string
prefix check, never `Path.resolve()`d) . What is NEW is what happens after
(3) fails: instead of nothing (the branch used to end there because (2)
already returned for every absolute non-`/tmp/` target), execution now falls
through to the pre-existing `is_within_project(dest, cwd)` +
`_is_control_plane_path(dest, cwd)` checks immediately below — the exact
same two checks `cp`/`mv`/`mkdir`/`git show` destinations already go through.
Nothing about the `/tmp/` branch's own logic changes; the fix is entirely
about NOT returning early for the one case that used to fail closed for no
good reason.

### D3 — Symlink-escape containment is inherited, not reimplemented

`is_within_project` (`tier1.py:1994`) does `Path(file_path).resolve(strict=
False)` (absolute) or `(resolved_cwd / file_path).resolve(strict=False)`
(relative) and checks `relative_to(resolved_cwd)` — symlink-aware on every
EXISTING path component, exactly the resolution `cp`/`mv`/`mkdir`/`git show`
already trust for their own destinations (cpp#38). Routing the absolute
redirect case through this SAME function — not a bespoke "does this absolute
path start with the worktree string" prefix check, which a symlink could
defeat — is what keeps a worktree symlink crafted to resolve OUTSIDE the
worktree (`esc -> /var/tmp/.../outside`, then `cargo build ... >
<worktree>/esc/x`) refused: `Path(cwd) / "esc/x"` — or, for the absolute
spelling, `Path("<worktree>/esc/x")` directly — resolves through the symlink
to the real, outside location, `relative_to(resolved_cwd)` raises
`ValueError`, and the veto fires. Proven directly: `tests/test_permissions.
py::test_destination_veto_symlink_escape_via_absolute_redirect_still_refused`.

### D4 — Why not just widen the lexical `/tmp/` check instead

Rejected. The ticket is explicit and cpp#143's own founding lesson
(`permissions.py:1042-1057`, `_is_sanctioned_tmp_scratch`'s docstring) backs
it: resolving symlinks to GRANT a lexical exemption is unsafe — a worktree
symlink crafted to resolve into an arbitrary target would wrongly qualify
under a widened literal-prefix rule. The `/tmp/` exception must stay exactly
what it is: text-only, no filesystem access. The worktree case is
structurally different — it is not a new SANCTIONED SCRATCH exception, it is
the ORDINARY containment question cpp#38 already answers correctly for every
other write-kind, and redirects deserve the same answer, not a new one.

### D5 — Disqualifier order is unchanged: fail closed BEFORE any resolve

`~`/leading-`$`/`..`/bad-charset targets never reach `is_within_project`,
exactly as before this fix (cpp#154 plan D3's reasoning, restated in the new
predicate's own docstring): `is_within_project` does no shell-expansion, so
`Path(cwd) / "~/x"` or `Path(cwd) / "$VAR/x"` would resolve to literal
same-named subdirectories INSIDE the worktree and wrongly read as contained —
the opposite of what bash itself would do. This fix changes WHICH targets
reach the disqualifier check's `False` branch (only "absolute and not `/tmp/`
and not otherwise disqualified" is new), not what happens once a target is
found disqualified.

## Does Not Reopen #143/#150/#155

- **cpp#143** (`mkdir` `/tmp` scratch, `_is_sanctioned_tmp_scratch` /
  `_TMP_SCRATCH_MKDIR_RE`) — untouched. Different code path (`kind ==
  "bash-mkdir"`), different predicate, not read or modified by this fix.
  `tests/test_permissions.py::test_mkdir_tmp_scratch_is_permitted_but_other_
  outside_targets_stay_lethal` — still green, unchanged.
- **cpp#150** (`bash-mkdir` regex backtracking) — untouched; this fix's only
  `tier1.py` edit is the `_is_contained_redirect_target` refactor, nowhere
  near the `mkdir` regex.
- **cpp#155** (redirect classification itself, `_segment_write_kind`'s
  `bash-redirect` fallback, `_extract_write_destinations`) — untouched.
  `_segment_write_kind` and `_extract_write_destinations` are not edited by
  this fix; every cpp#155 test in `tests/test_permissions.py` (the
  `test_segment_write_kind_classifies_redirects_from_any_verb`,
  `test_destination_veto_now_fires_for_redirects_to_system_or_escaped_paths`,
  `test_destination_veto_still_none_for_tmp_scratch_and_worktree_relative`,
  `test_destination_veto_symlink_escape_via_bare_redirect`,
  `test_destination_veto_heredoc_body_text_not_misread_as_a_command`,
  `test_denial_is_terminal_redirect_gap_closed_at_destination_veto_too`,
  `test_handler_vetoes_redirect_to_system_path_end_to_end` section) and in
  `tests/test_policy_devpilot.py` (the `test_cpp155_*` section) pass
  unchanged, verbatim, post-fix — see § Requirements / Test Evidence.
- **`_is_contained_redirect_target`'s own contract** — every existing
  assertion on it (`tests/test_tier1.py:1377-1389`) passes unchanged: the
  function's observable input/output mapping is byte-for-byte identical
  before and after the D1 refactor.
- **`_redirect_destination_veto_reason`** (cpp#154, the defense-in-depth
  function `_denial_is_terminal` also calls) — not edited. It still uses
  `_is_contained_redirect_target` exactly as before (unchanged behavior per
  D1), so its own verdicts are unaffected by this fix. This means an absolute
  in-worktree redirect target it sees continues to be silently skipped by
  THIS function specifically (its own long-standing docstring: "Not stripped
  upstream, so lethality was already decided by the tier3 pattern. Nothing
  for this function to add.") — harmless, because `_destination_veto_reason`
  is the strict superset that now correctly says `None` for that exact case,
  and the aggregate `_denial_is_terminal` OR's the two together.

## Mandatory Negative-Test Gate

Three cases, `tests/test_permissions.py`, new section immediately following
the cpp#155 redirect tests:

1. `test_destination_veto_allows_absolute_redirect_target_within_worktree` —
   an absolute redirect target that resolves inside a real (non-`/tmp`)
   worktree dir the test creates, `cwd` set to that worktree. **Regression
   case.** RED on `main`/`d9f9254` (`f(...)` returns a non-`None` veto
   reason); GREEN after.
2. `test_destination_veto_symlink_escape_via_absolute_redirect_still_refused`
   — a REAL symlink inside that same worktree resolving to a directory
   outside it (`tmp_path`-style, but rooted outside `/tmp`), redirect target
   spelled through the symlink, absolute. Green on BOTH `main` and this fix
   (a negative control the fix must not break, not a regression case).
3. `test_destination_veto_tmp_absolute_redirect_still_allowed` — the
   unmodified `/tmp/` lexical exception. Green on both.

Plus `test_destination_veto_mika_1719_probe_command_no_longer_vetoed` —
the exact mika#1719 command, replayed against a worktree dir shaped like
the real one (`.../mika-platform/.claude/worktrees/fix-1719-.../`, not
under `/tmp`). RED on `main`/`d9f9254`, GREEN after — the direct incident
replay.

**Why the worktree dirs are NOT `pytest`'s own `tmp_path` fixture.** On this
platform `tmp_path` itself resolves under `/tmp/pytest-.../...`. An absolute
redirect target rooted there would ALSO satisfy the pre-existing, unrelated
`/tmp/`-prefix lexical exception (cpp#143/#150/#155) — silently passing even
on unpatched `d9f9254` and defeating the whole point of a regression test. A
new helper, `_make_non_tmp_worktree` (and the inline equivalent in the
mika#1719 probe test), roots the worktree under `/var/tmp` instead — mirrors
the real mika#1719 shape, where the worktree lives under
`/data/workspace/mika-platform/...`, never `/tmp/`. `/var/tmp` was chosen
over other alternatives (the repo checkout dir, `$HOME`) because it is
guaranteed present and writable in every environment this suite runs in and
is not itself sanctioned scratch by any rule this fix touches. Directories
are created with `tempfile.mkdtemp` and removed at teardown via
`request.addfinalizer`.

### Anti-vacuity — red-before, captured against pristine `d9f9254`

Source fix (`permissions.py` + `tier1.py`) stashed via `git stash push --
src/claude_pilot/permissions.py src/claude_pilot/tier1.py`; only the new
tests present. Verbatim:

```
tests/test_permissions.py::test_destination_veto_allows_absolute_redirect_target_within_worktree FAILED
tests/test_permissions.py::test_destination_veto_symlink_escape_via_absolute_redirect_still_refused PASSED
tests/test_permissions.py::test_destination_veto_tmp_absolute_redirect_still_allowed PASSED
tests/test_permissions.py::test_destination_veto_mika_1719_probe_command_no_longer_vetoed FAILED

assert "redirect destination '/var/tmp/cpp176-wt-kj8i23vq/wt/sub/out.txt' is not a literal contained path — not lexically under /tmp/ or worktree-relative (denied fail-closed)" is None

assert "redirect destination '/var/tmp/cpp176-mika1719-_vkril2t/mika-platform/.claude/worktrees/fix-1719-telemetry-build/out' is not a literal contained path — not lexically under /tmp/ or worktree-relative (denied fail-closed)" is None

2 failed, 2 passed, 39 deselected in 0.48s
```

Case 1 and the mika#1719 probe (case 1's exact incident shape) fail exactly
as the regression predicts — veto present, captured verbatim. Cases 2 and 3
pass on unpatched `d9f9254` too, as expected: they are negative controls on
code paths this fix does not touch.

### Green, post-fix

```
tests/test_permissions.py::test_destination_veto_allows_absolute_redirect_target_within_worktree PASSED
tests/test_permissions.py::test_destination_veto_symlink_escape_via_absolute_redirect_still_refused PASSED
tests/test_permissions.py::test_destination_veto_tmp_absolute_redirect_still_allowed PASSED
tests/test_permissions.py::test_destination_veto_mika_1719_probe_command_no_longer_vetoed PASSED

4 passed, 39 deselected in 0.42s
```

### Full suite, lint, types

```
1105 passed in 14.23s
```

`uv run ruff check .` — `All checks passed!`
`uv run mypy src` — `Success: no issues found in 23 source files`
`./scripts/verify-pipeline.sh` — passes (docs + source buckets both present).

## Non-Goals / Residuals Named, Not Closed

- `_redirect_destination_veto_reason` is not merged into `_destination_veto_
  reason`, mirroring cpp#155's own D-note on the same question — kept as
  defense-in-depth on a containment-boundary change; `_denial_is_terminal`
  still calls both.
- The argument-based write class (`tee FILE`, `touch FILE`, `dd of=FILE` with
  no shell redirect) named out of scope by cpp#155 D6 remains out of scope
  here too — untouched, unaffected.
- No change to `_is_control_plane_path` (cpp#42) — an absolute path that now
  passes containment via `is_within_project` still goes through the
  control-plane denylist check exactly as `cp`/`mv`/`mkdir`/`git show`
  destinations already do.

## References

- `senara-solutions/claude-pilot#176` — the ticket (relayed from mika#1719).
- `senara-solutions/claude-pilot#155` / PR#173 — the regression's origin;
  `docs/plans/2026-09-10-001-fix-155-redirection-destination-veto-plan.md`.
- `docs/plans/2026-09-04-002-fix-154-redirection-fichier-letalite-plan.md`
  § D3 — the `~`/`$`/`..` fail-closed-before-resolve reasoning this fix
  preserves verbatim.
- `src/claude_pilot/permissions.py` — `_destination_veto_reason` (edited).
- `src/claude_pilot/tier1.py` — `_is_lexically_disqualified_redirect_target`
  (new), `_is_contained_redirect_target` (refactored, behavior unchanged),
  `is_within_project` (read, reused unchanged).
- `tests/test_permissions.py` — new cpp#176 section (4 tests);
  `tests/test_tier1.py`, `tests/test_policy_devpilot.py` — existing suites
  replayed for non-regression, unchanged.
