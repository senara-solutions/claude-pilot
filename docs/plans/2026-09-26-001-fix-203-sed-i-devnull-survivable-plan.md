---
issue: claude-pilot#203
title: "sed -i vers cible inoffensive (/dev/null) reste TERMINAL — rendre survivable"
type: fix
scope_repo: claude-pilot
priority: p1-lethality
date: 2026-09-26
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# sed -i vers cible inoffensive (/dev/null) reste TERMINAL — rendre survivable - Plan

## Goal Capsule

**Objectif.** mika#1686 comment 5844642872 named 4 deny-deaths in the
n=4 dossier; on HEAD `9ab3c13` only ONE residue is still terminal —
instance #1 (pilot session `b669ac5c`, mika#2532 impl):

```
sed -i 's/.../ X/' /dev/null; grep -n "created_by_session" crates/m.rs
```

Established at source (probe: `policy.evaluate`, `_denial_is_terminal`):
`policy=allow(rule_id=bash-grep)` (first-match on the innocent trailing
`grep`, which would run) but `_denial_is_terminal=True`. The `sed -i`
segment alone — targeting `/dev/null`, a no-op device — kills the session
(`error_during_execution:after_deny`, turn 62). This plan makes exactly
that one write-verb-against-a-proven-harmless-target class survivable,
without loosening the refusal in either direction and without touching
admission (`is_tier1_auto_approve`/`is_safe_bash_command`/
`is_tier3_dangerous`) or any YAML rule. Scope B only (per the dispatch's
own cut): the general "any syntactic over-refusal survivable" proposal
(cpp#128 doctrine) is explicitly out of scope, carried to Prime/Vincent
separately.

## Cause established at source (probed in this worktree, HEAD `9ab3c13`)

| function | verdict on the incident command | why |
|---|---|---|
| `policy.evaluate` | `allow`, `rule_id=bash-grep` | first-match on the trailing `grep` segment |
| `is_tier3_dangerous(command)` (REFUSAL) | `True` | `TIER3_PATTERNS`' `sed -i` entry matches on the flag alone, any target |
| `is_tier3_dangerous_for_lethality(command)` (pre-fix) | **`True`** | **the bug** — nothing in the redirect-stripping pipeline (`_mask_quoted_redirect_chars`, `_STDOUT_DEVNULL_RE`, `_strip_contained_redirects`) ever sees `sed -i`'s target: `/dev/null` here is a positional FILE ARGUMENT to `sed -i`, not a shell redirect — no `<`/`>` character appears anywhere in the command |
| `_denial_is_terminal(...)` (pre-fix) | **`True`** | short-circuits on the first check above |

This is a **different grammar position** from cpp#130's `>/dev/null`
carve, which strips a *redirect operator's* target. `sed -i FILE` edits
`FILE` in place with no redirect syntax at all, so cpp#130's existing
`_STDOUT_DEVNULL_RE`/`_strip_contained_redirects` machinery is structurally
blind to it — confirmed: `_segment_write_kind("sed -i 's/a/b/' /dev/null")`
also returns `None` (sed is not `cp`/`mv`/`mkdir`/`git show`, and
`_segment_redirect_targets` finds no `>` at all), so
`_destination_veto_reason`/`_redirect_destination_veto_reason` never even
see this write either — both downstream `_denial_is_terminal` checks are
no-ops for this shape, and the ENTIRE fix has to land in
`is_tier3_dangerous_for_lethality` itself.

## The fix — a new, narrow lexical carve in `tier1.py`

**`_SED_I_DEVNULL_RE`** (`src/claude_pilot/tier1.py`, next to
`_STDOUT_DEVNULL_RE`): matches `sed -i`/`-i<flags>` (the exact
`TIER3_PATTERNS` flag alternation `-\w*i|-i\w*`), an OPTIONAL single
script/expression argument (quoted or one bare token), then a literal
`/dev/null` target that must be the LAST token before a compound-command
separator (`;`, `&`, `|`, `)`) or end-of-string — reusing cpp#130's exact
`/dev/null` RECOGNITION (the same literal text and the same
trailing-boundary lookahead `(?![/\w.])` that already keeps
`/dev/null.txt`/`/dev/nullified`/`/dev/null/../etc/passwd` fatal for
redirects), **not inventing a new one**, per scope B.

`is_tier3_dangerous_for_lethality` now strips a match of this regex (blank
to a single space, same style as `_STDOUT_DEVNULL_RE.sub(" ", …)`) before
running `is_tier3_dangerous` on the result:

```diff
     return is_tier3_dangerous(
         _strip_contained_redirects(
-            _STDOUT_DEVNULL_RE.sub(" ", _mask_quoted_redirect_chars(command))
+            _STDOUT_DEVNULL_RE.sub(
+                " ", _SED_I_DEVNULL_RE.sub(" ", _mask_quoted_redirect_chars(command))
+            )
         )
     )
```

Order does not matter relative to the other three narrowings (mask →
/dev/null-redirect → contained-target): `_SED_I_DEVNULL_RE` shares no
character class with any of them (no `<`/`>` in its match), so it is
applied between the mask and the /dev/null-redirect strip purely for
readability, next to the constant it pairs with — documented in the
function's docstring.

**Why the carve is scoped this narrowly (fail-closed on everything else):**

- Only the bare flag shape `TIER3_PATTERNS` already matches — the carve
  can never fire on a command the flag pattern would not have matched
  anyway (no widening of what "sed -i" means).
- At most ONE script/expression argument between the flag and the target.
  A second flag or a second pre-target argument is a different shape and
  is left untouched (stays lethal).
- `/dev/null` must be the ONLY file operand — a second, real target
  (`sed -i 's/a/b/' /dev/null realfile.rs`) fails the trailing lookahead
  and stays lethal, because sed -i writes to EVERY file argument it is
  given, not just the first.
- Whitespace is required immediately after the flag, so a backup-suffix
  flag glued to `-i` (`-i.bak`) does not match — a documented,
  undemonstrated shape left on the fail-closed side (not the mika#1686
  incident).

**What is explicitly NOT touched:** `is_tier3_dangerous` (the REFUSAL
classifier — the command stays denied, exactly as before), `TIER3_PATTERNS`
itself, `is_safe_bash_command`, `is_tier1_auto_approve`, `SAFE_SHELL_
COMMANDS`, any YAML allow-rule, `_destination_veto_reason`,
`_redirect_destination_veto_reason`, `_segment_write_kind`,
`_is_contained_redirect_target`, `_is_lexically_disqualified_redirect_
target`, `_is_mktemp_scratch_redirect_target` (cpp#201). No env bypass
added, no admission change of any kind.

## Both-directions proof

**Positive (become NON-terminal, still denied) — red-before/green-after,
verbatim:**

Red (source stashed to `3d8a23b`, only the new survivability assertions
fail; every negative assertion — written against the SAME pre-fix HEAD —
already passes, proving they test a real property, not a vacuous one):

```
$ git stash push -- src/claude_pilot/tier1.py
$ uv run pytest tests/test_tier1.py::TestTier3SedIDevnullLethality tests/test_permissions.py -k cpp203 -v
...
FAILED tests/test_tier1.py::TestTier3SedIDevnullLethality::test_exact_incident_replay_not_lethal
FAILED tests/test_tier1.py::TestTier3SedIDevnullLethality::test_sed_i_devnull_alone_not_lethal
FAILED tests/test_permissions.py::test_cpp203_denial_is_terminal_mika_2532_replay_survivable
FAILED tests/test_permissions.py::test_cpp203_denial_is_terminal_sed_i_devnull_alone_survivable
FAILED tests/test_permissions.py::test_cpp203_handler_end_to_end_still_denied_but_survivable
5 failed, 8 passed in 0.1s (tier1) / 10 passed, 3 failed (permissions)
$ git stash pop
```

Green (fix restored):

```
$ uv run pytest tests/test_tier1.py::TestTier3SedIDevnullLethality -v
8 passed
$ uv run pytest tests/test_permissions.py -k "cpp203 or cpp201 or cpp154 or cpp196" -v
33 passed
```

- `test_exact_incident_replay_still_refused` — `is_tier3_dangerous(...)`
  on the exact incident command stays `True` (unchanged REFUSAL).
- `test_exact_incident_replay_not_lethal` /
  `test_cpp203_denial_is_terminal_mika_2532_replay_survivable` — the EXACT
  incident command (`sed -i 's/.../ X/' /dev/null; grep -n
  "created_by_session" crates/m.rs`) → `is_tier3_dangerous_for_lethality`
  and `_denial_is_terminal` both `False`.
- `test_cpp203_handler_end_to_end_still_denied_but_survivable` — same
  command through the REAL `create_permission_handler`:
  `PermissionResultDeny` (never `Allow`), `interrupt is False`. Measured
  live:

  ```
  positive full incident: PermissionResultDeny False
    "policy allow (bash-grep) vetoed — command chains a tier3-dangerous
     or command-substitution tail onto the allowed prefix"
  positive alone:         PermissionResultDeny False
  negative real target:   PermissionResultDeny True
  ```

- `test_sed_i_devnull_alone_not_lethal` /
  `test_cpp203_denial_is_terminal_sed_i_devnull_alone_survivable` —
  isolates the `sed -i` segment from the trailing `grep`: `sed -i
  's/a/b/' /dev/null` alone is also survivable.

**Negative (STAY terminal, both worlds) — pass unchanged pre- and
post-fix:**

`test_real_target_stays_lethal` / `test_second_real_target_stays_lethal` /
`test_devnull_lookalike_escape_stays_lethal` /
`test_danger_alongside_sed_i_devnull_stays_lethal` /
`test_backup_suffix_glued_to_flag_stays_lethal` (`test_tier1.py`) and
`test_cpp203_negative_stays_terminal` (10 parametrized cases,
`test_permissions.py`): a real out-of-worktree target (`/etc/passwd`), a
real in-worktree target (`realfile.rs`), the ratified `$HOME`-as-`~`-
respelling invariant (cpp#154 D3), a second real target alongside
`/dev/null`, /dev/null lookalikes (`/dev/nullified`, `/dev/null.txt`,
`/dev/null/../etc/passwd`, cpp#130's own trailing-boundary edges reused
verbatim), `rm -rf`, `git push --force`, `bash -c`, and a dangerous verb
chained alongside the carved `sed -i … /dev/null` (`&&`/`;`) all stay
terminal. `chmod 000 /etc/x` was considered as a required negative per the
dispatch, but measured `False` (non-terminal) on THIS repo's pre-fix HEAD
too — `chmod` is not a `TIER3_PATTERNS`/`_segment_write_kind` entry at
all — so it is not a valid negative here and was deliberately NOT included
(flagged as a judgment call below).

## Non-reopening

`test_cpp203_non_reopening_cpp154_d3_cpp196_cpp201` replays: the cpp#154
D3 `$HOME`-as-`~`-respelling control (`echo hi > $HOME/.ssh/
authorized_keys` stays terminal), the cpp#196 `/tmp` git-show-redirect
carve (`git show … > /tmp/x 2>/dev/null || true` stays non-terminal), and
the cpp#201 mktemp-scratch heredoc carve (`T=$(mktemp -d) ; cat >"$T/log"
<<'EOF' … EOF` stays non-terminal) — all measured unchanged, because
`_SED_I_DEVNULL_RE` is disjoint from every redirect-based check those three
tickets touch (no `<`/`>` character is ever involved in matching `sed -i
… /dev/null`). `test_cpp201_negative_stays_terminal`'s existing
`"sed -i 's/a/b/' /etc/f"` case (a REAL target, not `/dev/null`) is
UNMODIFIED and still asserts terminal — cpp#128 (deny-vs-terminal split)
is the same mechanism this fix extends, unaltered elsewhere.

## Full suite / lint / gate (verbatim)

```
$ uv run pytest
1245 passed in 14.53s

$ uv run ruff check .
All checks passed!

$ uv run mypy src
Success: no issues found in 23 source files

$ ./scripts/verify-pipeline.sh
(passes once this plan doc is committed alongside the source diff)
```

No existing test was modified — only new tests were added
(`TestTier3SedIDevnullLethality` in `tests/test_tier1.py`; a new cpp#203
section appended to `tests/test_permissions.py`).

## Judgment calls, flagged

1. **`chmod` dropped from the negative battery.** The dispatch's suggested
   negative list included `chmod 000 /etc/x` as a "stays terminal" control.
   Measured: `chmod` is not itself a `TIER3_PATTERNS` entry, and
   `_segment_write_kind` only classifies `cp`/`mv`/`mkdir`/`git show >`/
   redirects — `chmod` matches none of these, so it was ALREADY
   non-terminal on pre-fix HEAD (`3d8a23b`). Including it as a "stays
   terminal" assertion would be asserting something false about this
   codebase, unrelated to this ticket. Substituted `bash -c 'id'` (a real,
   independently-verified tier3-dangerous+terminal case) in its place.
2. **Backup-suffix `sed -i.bak /dev/null` left un-widened.** The carve
   requires whitespace immediately after the `-i` flag, so `sed -i.bak
   's/a/b/' /dev/null` does not match and stays lethal. This is a narrower
   cut than the bare `TIER3_PATTERNS` flag shape and is not the mika#1686
   incident; documented in-code and pinned by
   `test_backup_suffix_glued_to_flag_stays_lethal` as a deliberate,
   fail-closed scope boundary rather than an oversight.
3. **No pilot was run.** All verification is via direct unit/handler-level
   probes (`policy.evaluate`, `_denial_is_terminal`,
   `create_permission_handler`) and the committed test suite, per the
   dispatch's explicit instruction.

## Acceptance criteria

- **AC1 — exact replay survivable, still refused.** The exact mika#1686/
  mika#2532 incident command (`sed -i 's/.../ X/' /dev/null; grep -n
  "created_by_session" crates/m.rs`) and the isolated `sed -i 's/a/b/'
  /dev/null` yield `is_tier3_dangerous(...) is True` (still refused) AND
  `is_tier3_dangerous_for_lethality(...) is False` /
  `_denial_is_terminal(...) is False` (no longer session-fatal); via
  `create_permission_handler`, `PermissionResultDeny` (never `Allow`) with
  `interrupt is False`. Red-before this fix (measured `True`/terminal on
  pre-fix HEAD `3d8a23b`), green after. →
  `test_exact_incident_replay_still_refused`,
  `test_exact_incident_replay_not_lethal`,
  `test_sed_i_devnull_alone_not_lethal`,
  `test_cpp203_denial_is_terminal_mika_2532_replay_survivable`,
  `test_cpp203_denial_is_terminal_sed_i_devnull_alone_survivable`,
  `test_cpp203_handler_end_to_end_still_denied_but_survivable`.
- **AC2 — real and out-of-worktree targets, and every unrelated
  tier3-dangerous shape, stay terminal in both directions; no admission
  widening.** `sed -i` against `/etc/passwd`, an in-worktree real file, a
  `$HOME` respelling, a second real target alongside `/dev/null`, and
  every `/dev/null` lookalike (`/dev/nullified`, `/dev/null.txt`,
  `/dev/null/../etc/passwd`) stay terminal; `rm -rf`, `git push --force`,
  `bash -c`, and a dangerous verb chained alongside the carved segment
  stay terminal; these pass UNCHANGED pre- and post-fix. →
  `test_real_target_stays_lethal`, `test_second_real_target_stays_lethal`,
  `test_devnull_lookalike_escape_stays_lethal`,
  `test_danger_alongside_sed_i_devnull_stays_lethal`,
  `test_backup_suffix_glued_to_flag_stays_lethal`,
  `test_cpp203_negative_stays_terminal` (10 cases).
- **AC3 — non-reopening cpp#154 D3 / cpp#196 / cpp#201 / cpp#176 /
  cpp#128, zero admission drift.** The `$HOME`-as-`~`-respelling
  invariant, the `/tmp` git-show-redirect carve, and the mktemp-scratch
  heredoc carve are all unaffected (measured, not merely asserted
  unchanged-by-inspection); the pre-existing `sed -i 's/a/b/' /etc/f`
  negative in `test_cpp201_negative_stays_terminal` is unmodified and
  still green; full suite (1245 passed), ruff, and mypy are clean; no
  existing test was edited. →
  `test_cpp203_non_reopening_cpp154_d3_cpp196_cpp201`, full suite run
  above, `git diff --stat` (no deletions/edits in pre-existing test
  bodies).

## References

- `src/claude_pilot/tier1.py:172` — `TIER3_PATTERNS`' `sed -i` entry (flag
  shape reused verbatim).
- `src/claude_pilot/tier1.py:240` — `_STDOUT_DEVNULL_RE` (cpp#130's
  /dev/null recognition, reused verbatim, not reinvented).
- `src/claude_pilot/tier1.py:243` (new) — `_SED_I_DEVNULL_RE` and its
  block comment.
- `src/claude_pilot/tier1.py:~677` — `is_tier3_dangerous_for_lethality`.
- `src/claude_pilot/permissions.py:768` — `_denial_is_terminal` (unchanged
  by this ticket; both downstream checks are no-ops for this shape,
  confirmed).
- `docs/plans/2026-09-24-002-fix-201-survivable-heredoc-mktemp-write-plan.md`
  — cpp#201, the same lethality-path class, mktemp-scratch heredoc/redirect.
- `docs/plans/2026-09-22-002-fix-195-tmp-redirect-refusal-survivable-plan.md`
  — cpp#195/#196, /tmp git-show-redirect.
- `docs/plans/2026-08-30-002-fix-128-nonlethal-policy-denial-plan.md` —
  cpp#128, refusal vs. lethality split (the doctrine this ticket extends,
  not revises).
- claude-pilot#203, claude-pilot#201, claude-pilot#196, claude-pilot#154,
  claude-pilot#130, mika#1686 (comment 5844642872).
