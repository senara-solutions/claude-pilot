---
issue: claude-pilot#209
title: cp/mv DESTINATION-ARGUMENT write-kind mktemp-scratch veto stays TERMINAL — rendre survivable - Plan
type: fix
scope_repo: claude-pilot
priority: p1-lethality
date: 2026-09-26
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# cp/mv DESTINATION-ARGUMENT write-kind mktemp-scratch veto stays TERMINAL — rendre survivable - Plan

## Goal Capsule

**Objectif.** cpp#209: pilot `1b6c4d70` died on mika#2054 at 14:44:12, on a
compound of the shape `D=$(mktemp -d) ; cp crates/…/*.rs "$D/" ; printf …
>> "$D/mod.rs" ; bash scripts/verify.sh "$D"`. cpp#201 already carved the
mktemp-scratch idiom (a `$VAR` reference rooted at a variable THIS SAME
COMMAND assigns from `mktemp`'s own output) out of LETHALITY for write-kind
`bash-redirect` (the `>>`/`>` append). The residual cpp#209 names: the SAME
idiom, applied to a `cp`/`mv` **destination argument** (write-kind
`bash-cp-mv`), never received the carve. This plan closes that residual —
LETHALITY ONLY, reusing cpp#201's existing predicates verbatim
(`_mktemp_scratch_variable_names` / `_is_mktemp_scratch_redirect_target`,
`tier1.py`) — without touching `is_tier3_dangerous`, `is_safe_bash_command`,
`is_tier1_auto_approve`, any YAML rule, or the gate.

## Cause established at source — refining the ticket's own framing

The ticket's dispatch cited a probe on HEAD `8877dd6` claiming
`_denial_is_terminal` measured `True` for the `cp … "$D/"` shape. Reproducing
this **on this branch's actual HEAD (`ad7b859`, and independently confirmed
on `8877dd6` itself)** surfaced a more precise mechanism than the ticket's
own framing, which this section documents in full — a judgment call flagged
per the task's own instructions ("verify at source", "prove BOTH
directions").

**Measurement 1 — with a real, EXISTING worktree** (`tmp_path / "wt"`, a real
directory on disk, mirroring every pre-existing cpp#195/#201/#203/#205 test's
own fixture pattern):

```
_denial_is_terminal("Bash", {"command": 'D=$(mktemp -d) ; cp crates/x/a.rs "$D/"'}, wt)
  -> False        (already, on UNFIXED HEAD — no bug observable here)
```

Root cause of this non-observation: unlike a redirect target,
`bash-cp-mv`'s containment question has **no cwd-independent, always-terminal
trigger** — `is_tier3_dangerous_for_lethality`'s bare-`>` TIER3 pattern (the
thing that makes an UNSTRIPPED `$`-rooted redirect target terminal by default,
before cpp#201's carve) has no `cp`/`mv` analog; `cp`/`mv` is not in
`TIER3_PATTERNS` at all. The ONLY terminal trigger for `bash-cp-mv` is
`_destination_veto_reason`'s call to `is_within_project`, a `cwd`-*dependent*
resolve. When `cwd` resolves, `is_within_project("$D/", cwd)` returns `True`
— Python does no shell expansion, so the LITERAL text `"$D"` reads as an
ordinary same-named subdirectory, genuinely contained under `cwd` — and the
write is never vetoed at all (for either the refusal or the lethality
question). This is a **pre-existing, out-of-scope gap** in
`_destination_veto_reason`'s `bash-cp-mv` branch (it has no
`_is_lexically_disqualified_redirect_target`-style pre-check the way the
`bash-redirect`/`bash-git-show-redirect` branch does) — flagged here, NOT
fixed here: fixing it would change the REFUSAL question for `bash-cp-mv`
(currently `None` for a `$`-rooted destination in this cwd world), which the
ticket's own bearing ("Le REFUS reste... aucune admission nouvelle") forbids.

**Measurement 2 — with a worktree that stopped resolving** (created, then
removed — `Path(cwd).resolve(strict=True)` raises `OSError`, the exact
fail-closed branch `is_within_project`'s own docstring names; a worktree torn
down mid-session, e.g. by a parallel cleanup, is exactly this shape):

```
_denial_is_terminal("Bash", {"command": 'D=$(mktemp -d) ; cp crates/x/a.rs "$D/"'}, gone_wt)
  -> True         ← the bug, reproducibly, on UNFIXED HEAD
_denial_is_terminal("Bash", {"command": full incident compound}, gone_wt)
  -> True         ← same
_denial_is_terminal("Bash", {"command": 'cp crates/x/a.rs "$D/"'} (no assignment), gone_wt)
  -> True         ← stays True even after the fix (anti-vacuity control, see below)
```

When `cwd` does not resolve, `is_within_project` fails closed to `False`
**unconditionally, for every destination alike** (not specifically because of
the `$`), and `_destination_veto_reason(..., for_lethality=True)` returns the
veto reason for the `bash-cp-mv` segment → `_denial_is_terminal` returns
`True`. This is the reproducible, non-vacuous trigger this plan's tests use —
confirmed by the mandatory red-before/green-after run below. It reproduces
the ticket's own claimed measurement exactly, and is consistent with the
mika#2054 incident window (a worktree that stopped resolving mid-run) without
depending on the accident that Measurement 1's cwd-resolves world hides.

**Conclusion.** The residual cpp#209 names is real, but its LIVE manifestation
is narrower than "any cp/mv-to-`$D`" — it fires specifically when `cwd`
fails to resolve. The fix below closes it unconditionally (independent of
whether `cwd` happens to resolve), which is strictly safer than leaving it
contingent on that accident, and is a pure, minimal extension of cpp#201's
own carve, not a new predicate.

## The fix — reuse cpp#201's predicate, gated on `for_lethality`, for `bash-cp-mv`

**One insertion, `src/claude_pilot/permissions.py`, in
`_destination_veto_reason`'s per-target loop, immediately before the
existing `is_within_project` check** (so it applies regardless of whether
that check would say "contained" or not):

```python
if (
    for_lethality
    and kind == "bash-cp-mv"
    and _is_mktemp_scratch_redirect_target(command, dest)
):
    continue
if not is_within_project(dest, cwd):
    ...
```

- `_is_mktemp_scratch_redirect_target` is cpp#201's own predicate,
  **imported already, called verbatim** — not reimplemented. It requires a
  `$VAR`/`"$VAR"`/`${VAR}` (+ safe, `..`-free relative tail) shape AND that
  `VAR` be assigned, in the SAME command, from `$(mktemp …)`/`` `mktemp …` ``
  — exactly cpp#201's own boundary (`$HOME`, `${HOME}`, `$OLDPWD`,
  `$(whoami)`, a bare `$`, `~`, and a traversal riding a legitimate
  scratch-var prefix all fail it, unchanged).
- Gated on `for_lethality` **first** in the `and`-chain: the whole branch is
  a syntactic no-op when `for_lethality=False` — the ONLY value every
  pre-existing caller of `_destination_veto_reason` passes (the default).
  This makes the REFUSAL question for `bash-cp-mv` **provably**
  byte-identical before/after this diff — not just tested, but structurally
  incapable of firing for `for_lethality=False`.
- Scoped to `kind == "bash-cp-mv"` specifically — `bash-mkdir` and
  `bash-redirect`/`bash-git-show-redirect` are untouched (the latter two
  already have cpp#201's carve, in their own branch, unaffected by this
  insertion).
- Placed BEFORE `is_within_project` (not inside the
  `bash-redirect`/`bash-git-show-redirect` branch, and not merged into that
  tuple) so the lethality verdict for this one named idiom does not depend
  on whether `is_within_project` happens to resolve `cwd` — see "Cause
  established at source" above for why folding `bash-cp-mv` into that
  branch wholesale would be WRONG: that branch's later `dest.startswith(
  "/tmp/"): continue` and the absence of any pre-check both change the
  REFUSAL question for `bash-cp-mv` (an admission-adjacent change this
  ticket's bearing forbids), whereas this scoped insertion changes nothing
  for `for_lethality=False`.

**What is explicitly NOT touched:** `is_tier3_dangerous`,
`is_safe_bash_command`, `is_tier1_auto_approve`, any YAML rule, the gate,
`_redirect_destination_veto_reason`, `_is_lexically_disqualified_redirect_
target`, `_is_contained_redirect_target`, the `bash-redirect`/`bash-git-show-
redirect` branch, and `is_within_project` itself. No env bypass added.
`tier1.py` is not modified at all — the entire diff is confined to one
insertion in `permissions.py`.

## Both-directions proof

**Positive (become NON-terminal, still denied) — red-before/green-after,
verbatim** (`git stash -- src/claude_pilot/permissions.py`, tests kept):

```
$ uv run pytest tests/test_permissions.py -k cpp209 -v
...
FAILED test_cpp209_denial_is_terminal_mika_2054_replay_survivable
FAILED test_cpp209_denial_is_terminal_cp_mv_alone_survivable
FAILED test_cpp209_handler_end_to_end_still_denied_but_survivable
3 failed, 15 passed, 125 deselected in 0.63s
```

Green (fix restored):

```
$ uv run pytest tests/test_permissions.py -k cpp209 -v
18 passed, 125 deselected in 0.63s
```

- `test_cpp209_denial_is_terminal_mika_2054_replay_survivable` — the exact
  incident compound, on a torn-down worktree → `_denial_is_terminal(...) is
  False`.
- `test_cpp209_denial_is_terminal_cp_mv_alone_survivable` — AC1's explicit
  ask: `cp x "$D/"` and `mv x "$D/"` alone (same-command mktemp assignment,
  no trailing redirect) → both `False`.
- `test_cpp209_handler_end_to_end_still_denied_but_survivable` — through the
  REAL `create_permission_handler`: `PermissionResultDeny` (never `Allow`),
  `interrupt is False`. The unassigned-`$D` control on the SAME shape stays
  `PermissionResultDeny` with `interrupt is True`.

**Negative (STAY terminal, both cwd worlds) — pass unchanged pre- and
post-fix (15 of the 18 tests above, present in the red run too):**

- `test_cpp209_unassigned_dollar_var_stays_terminal` — the SAME `"$D/"`
  shape, WITHOUT the `D=$(mktemp -d)` assignment anywhere in the command,
  stays terminal (anti-vacuity: the carve is keyed on provable mktemp
  origin, not the mere presence of `$`).
- `test_cpp209_negative_stays_terminal_torn_down_cwd` (8 parametrized cases)
  and `test_cpp209_negative_stays_terminal_existing_cwd` (4 cases, the
  world where cp/mv containment already worked) — `/etc/y`,
  `/var/outside/x` (resolvable, out-of-worktree), `rm -rf x`, `sed -i`,
  `git push --force`, `mv x "$HOME/z"` (cpp#154 D3, via this ticket's own
  verb pair), a redirect... i.e. cp/mv to a DIFFERENT, non-mktemp-assigned
  variable (`$OTHER`), and a traversal riding a legitimate scratch-var
  prefix (`"$D/../../etc/passwd"`, two `..` against one `$D` pseudo-segment
  — genuinely escapes even in `is_within_project`'s own arithmetic, in
  BOTH cwd worlds) — all stay terminal, everywhere.
- `test_cpp209_admission_unaffected_for_lethality_false` — direct proof
  that `_destination_veto_reason(cmd, cwd, for_lethality=False)` is
  unchanged in BOTH cwd worlds (the torn-down-worktree world still returns
  the containment-veto string; the existing-worktree world still returns
  `None` — i.e. Measurement 1's pre-existing, out-of-scope gap is
  preserved exactly, not touched in either direction).

## Non-reopening

`test_cpp209_non_reopening_154d3_176_195_196_201_203_205` replays, on an
EXISTING worktree, unmodified representative cases from cpp#154 D3, #176,
#195/#196, #201, #203, and #205 — all unchanged. cpp#207 is not replayed as
a runtime case: it is an admission-only change (`is_tier1_auto_approve` /
`tier1.py`), structurally disjoint from `_denial_is_terminal` /
`_destination_veto_reason` (`permissions.py`), which this diff is entirely
confined to — confirmed by grep (`tier1.py` has zero lines changed by this
diff) and by the full suite (every pre-existing test file, including
`test_tier1.py`'s cpp#207 tests, is unmodified and green).

## Admission-identity proof

`_is_mktemp_scratch_redirect_target` is called at the new site with the SAME
signature cpp#201 already uses elsewhere in this function — no new
predicate. The new branch's `for_lethality and ...` structure means it is
**syntactically unreachable** when `for_lethality=False`: every existing
call site of `_destination_veto_reason` (the two in `create_permission_
handler`, the REFUSAL-facing default) passes `False` (or the parameter's
default), so this diff cannot change what any of them return — confirmed
both by code inspection (a single top-level `and for_lethality`) and by
`test_cpp209_admission_unaffected_for_lethality_false`. `tier1.py` — the
module `is_tier1_auto_approve`/`is_safe_bash_command`/`is_tier3_dangerous`
live in — has ZERO lines touched by this diff; `permissions.py`'s
`_denial_is_terminal` is the only consumer of the new branch's effect
(reachable only via its own `for_lethality=True` call), mirroring cpp#201's
own disjoint-call-graph proof.

## Full suite / lint / gate (verbatim)

```
$ uv run pytest -q
1378 passed in 15.33s   (1360 baseline + 18 new cpp#209 tests)

$ uv run ruff check .
All checks passed!

$ uv run mypy src
Success: no issues found in 23 source files

$ ./scripts/verify-pipeline.sh
(run against this plan doc + the paired source diff — bucket check passes:
docs/plans/** present alongside src/claude_pilot/permissions.py and
tests/test_permissions.py)
```

## Judgment calls flagged

1. **The ticket's own reproduction claim was refined, not simply trusted.**
   Measurement 1 above shows the exact `cp … "$D/"` shape is ALREADY
   non-terminal on unfixed HEAD when `cwd` resolves — the ticket's "terminal
   True" measurement only reproduces when `cwd` fails to resolve
   (Measurement 2). This plan documents both measurements explicitly and
   scopes the fix + tests to the mechanism that is actually reproducible,
   per "verify at source" / "prove both directions" — the fix is unaffected
   by which measurement is closer to the exact original incident trace,
   since it closes the class unconditionally either way.
2. **Rejected alternative: folding `bash-cp-mv` into the existing
   `bash-redirect`/`bash-git-show-redirect` tuple wholesale.** This would
   have been a shorter diff (one line: add `"bash-cp-mv"` to the tuple at
   `_destination_veto_reason`'s branch condition) and would ALSO pick up
   that branch's `/tmp/`-literal-prefix carve (closing a second, related
   gap: a literal, already-resolved `/tmp/…` cp/mv destination — e.g. if a
   pilot typed the LITERAL path `mktemp -d` returned in an earlier, separate
   tool call, rather than the shell variable — independently measured to be
   TERMINAL today, both via this same `is_within_project` path and via the
   unconditional-`interrupt=True` allow-then-veto path in
   `create_permission_handler`). **Rejected**: that branch's disqualification
   pre-check and its `/tmp/` carve both run for `for_lethality=False` too
   (every existing caller), so folding `bash-cp-mv` in would change the
   REFUSAL question for `bash-cp-mv` — e.g. a bare `mv x "$HOME/z"` would
   newly become vetoed-and-UNCONDITIONALLY-terminal via `create_permission_
   handler`'s allow-then-veto path if the YAML `bash-cp-mv` rule ever admits
   it (it can, via a distinct, out-of-scope quoting gap in that YAML rule's
   own lookaheads — see below). That is an admission-adjacent behavior
   change this ticket's bearing explicitly forbids ("aucune admission
   nouvelle"). The literal-`/tmp/`-path class and the YAML quoting gap are
   real, separate findings, flagged here for a FUTURE, narrowly-scoped
   ticket — not fixed in this diff.
3. **A separate, out-of-scope finding: the `bash-cp-mv` YAML rule's
   `(?!.*\s\$)`/`(?!.*\s~)`/`(?!.*\s/)` lookaheads check the character
   immediately after a space — a QUOTED operand (`cp x "$D/"`) has a quote
   character there instead, so the lookahead does not see the `$`/`~`/`/`
   and the rule matches anyway** (measured: `policy.evaluate('Bash', {
   'command': 'cp crates/x/a.rs "$D/"'})` → `decision=allow,
   rule_id=bash-cp-mv` on unfixed HEAD). Combined with Measurement 1's gap
   (`_destination_veto_reason` never disqualifies a `$`/`~`-rooted `bash-
   cp-mv` destination), a QUOTED `$`/`~`-rooted cp/mv destination is
   currently **admitted and executed**, not merely denied — a genuine,
   pre-existing containment gap, and explicitly OUT OF SCOPE here (YAML and
   admission are untouched by this ticket's bearing). Flagged for Prime, or
   a dedicated ticket; not touched by this diff or its tests.

## Acceptance criteria

- **AC1 — exact replay survivable, still refused.** The exact mika#2054
  compound (`D=$(mktemp -d) ; cp crates/…/*.rs "$D/" ; printf … >>
  "$D/mod.rs" ; bash scripts/verify.sh "$D"`), and `cp x "$D/"` / `mv x
  "$D/"` alone, yield `_denial_is_terminal(...) is False` on a worktree that
  stopped resolving (the reproducible trigger — see "Cause established at
  source"); via `create_permission_handler`, `PermissionResultDeny` (never
  `Allow`) with `interrupt is False`. → `test_cpp209_denial_is_terminal_
  mika_2054_replay_survivable`, `test_cpp209_denial_is_terminal_cp_mv_alone_
  survivable`, `test_cpp209_handler_end_to_end_still_denied_but_survivable`.
- **AC2 — resolvable out-of-worktree and tier3-dangerous stay terminal, in
  BOTH cwd worlds; `$HOME` and unassigned-`$D` stay terminal.** `/etc/y`,
  `/var/outside/x`, `rm -rf`, `sed -i`, `git push --force`, `mv x
  "$HOME/z"` (cpp#154 D3, via this ticket's own verb pair), a redirect to a
  DIFFERENT un-assigned variable, and a traversal riding a legitimate
  scratch-var prefix all stay `_denial_is_terminal(...) is True`, on both a
  torn-down and an existing worktree. → `test_cpp209_unassigned_dollar_var_
  stays_terminal`, `test_cpp209_negative_stays_terminal_torn_down_cwd` (8
  cases), `test_cpp209_negative_stays_terminal_existing_cwd` (4 cases).
- **AC3 — non-reopening, and zero admission drift.** cpp#154 D3/#176/#195/
  #196/#201/#203/#205 replay unmodified and green; cpp#207 is structurally
  disjoint (admission-only, `tier1.py`, zero lines touched). The REFUSAL
  question (`for_lethality=False`) is byte-identical in both cwd worlds. →
  `test_cpp209_non_reopening_154d3_176_195_196_201_203_205`,
  `test_cpp209_admission_unaffected_for_lethality_false`, full suite (1378
  passed).

## References

- `src/claude_pilot/permissions.py:1172` — `_destination_veto_reason`, new
  `bash-cp-mv` branch.
- `src/claude_pilot/permissions.py:812` — `_denial_is_terminal` (unchanged;
  consumer of the new branch via its existing `for_lethality=True` call).
- `src/claude_pilot/tier1.py:559` / `:596` — `_mktemp_scratch_variable_
  names` / `_is_mktemp_scratch_redirect_target` (cpp#201, reused verbatim,
  unmodified).
- `docs/plans/2026-09-24-002-fix-201-survivable-heredoc-mktemp-write-plan.md`
  — cpp#201, the redirect-write-kind carve this ticket extends.
- `docs/plans/2026-09-26-001-fix-203-sed-i-devnull-survivable-plan.md`,
  `docs/plans/2026-09-26-002-fix-205-default-survivable-lethality-plan.md`,
  `docs/plans/2026-09-26-003-feat-207-allow-tracked-repo-scripts-plan.md` —
  non-reopened by this diff.
- claude-pilot#209, mika#2054, mika#1686.
