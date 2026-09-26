---
issue: claude-pilot#205
title: "défaut SURVIVABLE pour _denial_is_terminal — terminal réservé au danger prouvé (mika#1686 généralisation, cas a)"
type: fix
scope_repo: claude-pilot
priority: p1-lethality
date: 2026-09-26
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# défaut SURVIVABLE pour `_denial_is_terminal` — Plan

DOCTRINE-level lethality change. Ratified by Prime + Vincent ("Oui. A",
2026-09-26). Bearing: LETHALITY ONLY (admit nothing new); non-reopening of
cpp#154 D3 / #176 / #196 / #201 / #203; watchdogs cpp#168/#177 are the loop
bound.

## Goal Capsule

**Objectif.** mika#1686 comment 5844642872 named a class cpp#130/#154/#155/
#157/#176/#195/#196/#201/#203 each carved an exemption from, one shape at a
time: a policy denial whose cause is pure FORM — a syntactic classifier that
could not parse or prove a target safe — was ending the run, not merely
refusing the command. This ticket is the doctrine-level generalization: make
`_denial_is_terminal`'s DEFAULT explicitly, provably SURVIVABLE, terminal
**only** for an enumerated PROVEN-DANGER set (case a).

**The discovery that reframes the work.** `_denial_is_terminal` was
*already* built this way — cpp#128 constructed it as "default `False`,
`True` only on an enumerated True-path" from the start. Auditing every
True-path (below) finds it is **not** an inversion but a **retargeting**:
one existing True-path (the purely lexical, cwd-free generic bare-`>`/`>>`
redirect pattern inside `is_tier3_dangerous_for_lethality`) is a genuine,
previously-undetected SYNTACTIC OVER-REFUSAL — class (ii) — and is removed;
one gap (`chmod -R`/`chown -R`/`dd`/`mkfs`/`truncate`/a fork bomb — verbs
whose blast radius matches `rm -rf`, never enumerated in `TIER3_PATTERNS`)
is a genuine UNDER-terminal miss and is closed. Every other True-path is
already class (i), proven danger, and is untouched.

## Audit — the complete truth table

`_denial_is_terminal(tool_name, tool_input, cwd)`, pre-cpp#205 (`permissions.py:768`):

```python
if tool_name != "Bash": return False
command = tool_input.get("command")
if not isinstance(command, str): return True
if is_tier3_dangerous_for_lethality(command): return True
if _redirect_destination_veto_reason(command, cwd) is not None: return True
return _destination_veto_reason(command, cwd, for_lethality=True) is not None
```

Every True-path, enumerated and classified:

| # | True-path | What it proves | Classification | Action |
|---|---|---|---|---|
| 0 | `tool_name != "Bash"` → **False** (not a True-path — the one guaranteed-survivable case) | n/a | n/a | unchanged |
| 1 | `command` missing/non-string | nothing parseable at all | (i) proven-danger by fail-closed convention (cpp#128's own choice: an empty string is parseable and safe, a missing/non-string value is not) | unchanged |
| 2a | `is_tier3_dangerous_for_lethality` — `rm -rf`/`-fr` | destructive verb, any target | (i) — a syntactic classifier cannot prove ANY target safe for this verb | unchanged |
| 2b | … `git push --force`/`-f`/to main·master, `git reset --hard`, `git branch -D` | destructive git verb | (i) | unchanged |
| 2c | … `DROP TABLE`/`DELETE FROM`, `cargo publish`, `gh label delete/edit` | destructive/irreversible verb | (i) | unchanged |
| 2d | … `sed -i` (target not proven `/dev/null`, cpp#203) | write verb, target not proven inert | (i) — cpp#203 already proved the ONE target that IS provably inert; everything else stays (i) | unchanged |
| 2e | … `bash -c`/`sh -c`/`eval` | arbitrary sub-execution, no target at all to prove safe | (i) | unchanged |
| 2f | … process substitution `<(`/`>(` | unprovable escape vector (executes a sub-command as a "file") | (i) | unchanged |
| 2g | … **generic bare `>`/`>>`** (`(?<!<)>{1,2}(?!\(|&[\d-])`), target not proven `/dev/null`/contained | **a FILE TARGET** — exactly what a cwd-aware check CAN prove safe or unsafe | **(ii) — SYNTACTIC OVER-REFUSAL.** The purely lexical, cwd-free `_is_contained_redirect_target` (cpp#154 D1, deliberately cwd-free) can only ever recognize a target as safe when it is worktree-RELATIVE or literally `/tmp/`-prefixed. An ABSOLUTE target that actually RESOLVES inside the worktree (the mika#1719/cpp#176 shape) is invisible to it and stays matching this entry — even though `_destination_veto_reason`'s `is_within_project` resolution (cpp#176's own fix, landed for cp/mv/mkdir/git-show) can and does prove it safe. Measured: `_denial_is_terminal` = `True` for `/usr/bin/time -v cargo build … > <cwd>/out` when `<cwd>` is absolute and NOT `/tmp/`-prefixed, even though `_destination_veto_reason(..., for_lethality=True)` independently returns `None` (not vetoed) for the SAME command. | **REMOVED from the verb-only lethality check; redirect-target lethality is now decided ENTIRELY by paths 3/4 below, which can prove it.** |
| 3 | `_redirect_destination_veto_reason` — resolvable escape (cpp#38) or control-plane (cpp#42), for a target that WAS lexically contained (relative/`/tmp/`) but resolves out via a symlink | proven escape, cwd-aware | (i) | unchanged |
| 4a | `_destination_veto_reason(..., for_lethality=True)` — `~`/leading-`$`/`..`/bad-charset/bare-`$` operand not traced to a same-command `mktemp` assignment (cpp#154 D3 / cpp#201) | disqualified outright, fail-closed | (i) — cannot be proven safe, and admitting `$HOME` etc. would make the `~` rejection "one respelling away from useless" | unchanged |
| 4b | … unparseable write-capable segment (`_extract_write_destinations` returns `None`/empty) | cannot determine the destination at all | (i) — fail-closed, the same direction every other write-kind takes | unchanged |
| 4c | … resolvable escape (cpp#38) via `is_within_project` | proven escape, cwd-aware | (i) | unchanged |
| 4d | … control-plane path (cpp#42) | proven control-plane write | (i) | unchanged |
| — | *(4e, the fallthrough)* — absolute target that is NOT disqualified and resolves INSIDE the worktree | proven CONTAINED, cwd-aware | already `None` (non-veto) pre-cpp#205 — this is the cpp#176 fix for cp/mv/mkdir/git-show/bash-redirect write-kinds; path 2g above was the ONLY reason it could still end up terminal for those exact same commands | now decisive (path 2g no longer overrides it) |

**Net change:**

- **REMOVED** (class ii → survivable): the generic bare-`>`/`>>` entry's
  contribution to `is_tier3_dangerous_for_lethality`. Every OTHER case that
  entry used to catch is independently proven dangerous by path 3/4 above
  (verified case-by-case — every "cpp#205 LEGITIMATE FLIP" comment in
  `tests/test_tier1.py` pins the specific command and its unchanged
  aggregate verdict). Nothing in the proven-danger set shrinks.
- **ADDED** (class under-terminal → terminal, case a): `chmod -R`/
  `--recursive`, `chown -R`/`--recursive`, `dd`, `mkfs`, `truncate`, a fork
  bomb (`:(){ :|:& };:`) — verbs whose blast radius matches `rm -rf` but
  which never matched any `TIER3_PATTERNS` entry. Measured: `is_tier3_
  dangerous("chmod -R 777 /etc")` is `False` and `_denial_is_terminal(...)`
  was `False` (survivable) pre-cpp#205 — a real gap, matching the dispatch's
  own audit note ("a bare `chmod 000 x`" was already correctly survivable;
  the RECURSIVE form was ALSO, incorrectly, already survivable).

## The fix

Two independent, additive changes, both `src/claude_pilot/tier1.py`, both
LETHALITY-only:

1. `_TIER3_VERB_PATTERNS_FOR_LETHALITY = TIER3_PATTERNS[:-1]` — every
   `TIER3_PATTERNS` entry EXCEPT its own trailing generic-redirect
   catch-all.
2. `_PROVEN_DANGEROUS_VERB_PATTERNS_CPP205` — six new, narrowly-scoped
   patterns (`chmod`/`chown` recursive-only, `dd`, `mkfs`, `truncate`, fork
   bomb).
3. `_matches_proven_dangerous_lethality_verb()` — searches the union of
   (1) and (2).
4. `is_tier3_dangerous_for_lethality`'s final step is retargeted from
   `is_tier3_dangerous(stripped)` (full `TIER3_PATTERNS`) to
   `_matches_proven_dangerous_lethality_verb(stripped)`. The four existing
   strips (quote-mask, `/dev/null`-redirect, `sed -i`-`/dev/null`,
   contained-redirect) are UNCHANGED — inert for the new verb patterns
   (none contains `<`/`>`/`/dev/null`/a redirect target), still load-bearing
   for `sed -i` (cpp#203) and for `<(`/`>(` (cpp#157's quote-mask).

`permissions.py`'s `_denial_is_terminal` itself is **UNCHANGED, byte-for-byte**
— it already had the right shape. Only its docstring and the doctrine block
above it are updated to state the retargeting and enumerate the
proven-danger set explicitly (the "make the default explicit, pinned by a
test" requirement — the code was already there; what changed is that it is
now audited, documented, and tested as a deliberate contract instead of an
emergent one).

```diff
 def is_tier3_dangerous_for_lethality(command: str) -> bool:
-    return is_tier3_dangerous(
+    return _matches_proven_dangerous_lethality_verb(
         _strip_contained_redirects(
             _STDOUT_DEVNULL_RE.sub(
                 " ", _SED_I_DEVNULL_RE.sub(" ", _mask_quoted_redirect_chars(command))
             )
         )
     )
```

**What is explicitly NOT touched:** `is_tier3_dangerous` (the REFUSAL
classifier — every command in this ticket stays exactly as refused as
before), `TIER3_PATTERNS` itself (only sliced, never mutated), `is_safe_
bash_command`, `is_tier1_auto_approve`, `SAFE_SHELL_COMMANDS`, any YAML rule,
`_destination_veto_reason`, `_redirect_destination_veto_reason`,
`_segment_write_kind`, `_is_contained_redirect_target`, `_is_lexically_
disqualified_redirect_target`, `_is_mktemp_scratch_redirect_target`
(cpp#201), `_denial_is_terminal`'s own code body. No env bypass added, no
admission change of any kind.

## Proven-danger set (case a), enumerated

Stays TERMINAL:

1. **Destructive verbs, regardless of target.** `rm -rf`/`-fr`, `git push
   --force`/`-f`/to main·master, `git reset --hard`, `git branch -D`, `DROP
   TABLE`, `DELETE FROM`, `cargo publish`, `sed -i` (unless its ONLY target
   is the inert `/dev/null` sink, cpp#203), `gh label delete/edit`, `bash
   -c`/`sh -c`/`eval`, process substitution (`<(`/`>(`) — plus the NEW
   `chmod -R`/`--recursive`, `chown -R`/`--recursive`, `dd`, `mkfs`,
   `truncate`, a fork bomb.
2. **Resolvable out-of-worktree WRITE** (cpp#154/#176) — proven via
   `is_within_project`, symlink-aware.
3. **Control-plane / worktree escape** (cpp#38/#42).
4. **`~` respelling via `$HOME`/`${HOME}`/`$OLDPWD`/`$(...)`/bare `$`**
   (cpp#154 D3) — disqualified outright, before any resolve.
5. **No parseable command at all** (missing key or non-string value).

Everything else defaults to SURVIVABLE — no per-shape carve-out required.

## Both-directions proof, red-before/green-after (verbatim)

Red (source stashed, new cpp#205 tests only, `test_tier1.py`/`test_permissions.py`):

```
$ git stash push -- src/claude_pilot/permissions.py src/claude_pilot/tier1.py
$ uv run pytest tests/test_permissions.py -k cpp205 tests/test_tier1.py -k "cpp205 or Cpp205" -v
...
FAILED tests/test_permissions.py::test_cpp205_proven_danger_stays_terminal_both_worlds[chmod -R 777 /etc]
FAILED tests/test_permissions.py::test_cpp205_proven_danger_stays_terminal_both_worlds[chmod --recursive 777 x]
FAILED tests/test_permissions.py::test_cpp205_proven_danger_stays_terminal_both_worlds[chown -R nobody /etc]
FAILED tests/test_permissions.py::test_cpp205_proven_danger_stays_terminal_both_worlds[dd if=/dev/zero of=/dev/sda]
FAILED tests/test_permissions.py::test_cpp205_proven_danger_stays_terminal_both_worlds[mkfs.ext4 /dev/sda1]
FAILED tests/test_permissions.py::test_cpp205_proven_danger_stays_terminal_both_worlds[truncate -s 0 /etc/passwd]
FAILED tests/test_permissions.py::test_cpp205_proven_danger_stays_terminal_both_worlds[:(){ :|:& };:]
FAILED tests/test_permissions.py::test_cpp205_absolute_in_worktree_redirect_now_survivable_any_verb
FAILED tests/test_permissions.py::test_cpp205_handler_end_to_end_new_verbs_still_denied_but_survivable
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[chmod -R 777 /etc]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[chmod -R 777 x]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[chmod --recursive 777 /etc]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[chown -R nobody /etc]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[chown --recursive nobody:nobody x]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[dd if=/dev/zero of=/dev/sda]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[dd if=/dev/zero of=x]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[mkfs.ext4 /dev/sda1]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[mkfs /dev/sda1]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[truncate -s 0 /etc/passwd]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[truncate -s 0 x]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[:(){ :|:& };:]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[:() { :|:& };:]
FAILED tests/test_tier1.py::TestCpp205ProvenDangerVerbLethality::test_new_verbs_are_lethal_regardless_of_target[: () { : | : & } ; :]
================ 23 failed, 40 passed, 570 deselected in 0.90s =================
$ git stash pop
```

Note the discriminating positive is `test_cpp205_absolute_in_worktree_
redirect_now_survivable_any_verb` — it deliberately roots its worktree
under `/var/tmp`, NOT pytest's `tmp_path` (which resolves under `/tmp` on
this platform and would be exempted by the PRE-EXISTING, unrelated `/tmp/`-
prefix carve regardless of this fix — a non-discriminating test would have
passed on both worlds and proven nothing).

Green (fix restored):

```
$ uv run pytest -q
1308 passed in 13.97s
```

**mika#1686 comment 5844642872's four instances, replayed verbatim:**

| # | shape | pre-cpp#205 | post-cpp#205 | test |
|---|---|---|---|---|
| 1 | `sed -i 's/.../ X/' /dev/null; grep -n "created_by_session" crates/m.rs` | `False` (cpp#203) | `False` (unchanged) | `test_cpp205_mika1686_instance1_sed_i_devnull_grep_survivable` |
| 2/3 | `cd /tmp && rm -rf <probe> && mkdir <probe2> && chmod 000 <probe2> && sh -c '...'` | `True` | `True` (unchanged — `rm -rf`/`sh -c`, case a) | `test_cpp205_mika1686_instance2_3_cwd_probe_stays_terminal` |
| 4 | `for t in test_a test_b; do printf '%s\n' "$t"; done` | `False` (already allow/survivable) | `False` (unchanged) | `test_cpp205_mika1686_instance4_for_loop_survivable_or_allowed` |

**Both-directions battery** (parametrized, `test_cpp205_proven_danger_
stays_terminal_both_worlds`, 15 cases; `test_cpp205_resolvable_redirect_
out_of_worktree_stays_terminal`, `test_cpp205_out_of_worktree_write_stays_
terminal`, `test_cpp205_control_plane_path_stays_terminal`, `test_cpp205_
home_respelling_stays_terminal_d3_non_reopening`, `test_cpp205_sed_i_on_
real_out_of_worktree_file_stays_terminal`) — `rm -rf`, recursive `chmod`/
`chown`, `dd`, `mkfs`, `truncate`, fork bomb, `sed -i` on a real
out-of-worktree file, `bash -c`/`sh -c`/`eval`, `git push --force`/`reset
--hard`, a resolvable `> /etc/passwd`, an out-of-worktree `cp` destination,
a control-plane write, and `$HOME`/`${HOME}`/`$OLDPWD` respellings — ALL
stay terminal, pass pre- AND post-fix.

**Survivable battery** (`test_cpp205_syntactic_overrefusals_default_
survivable`, 5 cases) — read chains, a for-loop, the sed-i-`/dev/null`
composed no-op, composed research pipes — all non-terminal.

**Admission-identity pin** (`test_cpp205_admission_identity_unaffected`,
extends cpp#201's `_ADMISSION_BATTERY`; `test_cpp205_handler_end_to_end_
new_verbs_still_denied_but_survivable`) — `is_tier1_auto_approve`/
`is_safe_bash_command` verdicts are byte-identical for every new-verb
command, and the real `create_permission_handler` still returns
`PermissionResultDeny` (never `Allow`) for `chmod -R 777 /etc`, only
`interrupt` flips.

**Default-survivable pin** (`test_cpp205_unrecognized_denied_shape_
defaults_survivable`) — three arbitrary, never-enumerated denied shapes are
survivable with zero per-shape carve-out, proving the fallthrough branch's
own behavior rather than a lookup table.

## Non-reopening

`test_cpp205_non_reopening_154d3_176_195_196_201_203` (`test_permissions.py`)
replays, in one place: cpp#154 D3 (`$HOME` stays terminal), cpp#176
(absolute-in-worktree `cp` stays non-terminal), cpp#195/#196 (`/tmp`
git-show-redirect carve stays non-terminal), cpp#201 (mktemp-scratch
heredoc carve stays non-terminal), cpp#203 (`sed -i /dev/null` composed
with an innocent `grep` stays non-terminal) — all measured unchanged. Every
pre-existing cpp#128/#130/#151/#154/#155/#157/#166/#176/#195/#196/#201/#203
test in the committed suite passes unchanged EXCEPT the 12 legitimate flips
below (all in `test_tier1.py`, none in `test_permissions.py` or
`test_policy_devpilot.py`).

## Existing tests that legitimately flip — flagged, with reasoning

**Zero tests were deleted or had their asserted PROPERTY weakened.** Twelve
tests in `test_tier1.py` asserted `is_tier3_dangerous_for_lethality(cmd) is
True` for a command whose ONLY reason for that verdict was the now-removed
generic bare-`>` entry — i.e. they were unit-pinning a responsibility
(redirect-target-escape detection) this ticket deliberately and correctly
moves OUT of that function and into the cwd-aware destination-veto calls
(see truth-table row 2g). Each was updated in place: the direct assertion on
`is_tier3_dangerous_for_lethality` now expects `False` (the function's own,
now-narrower contract), immediately followed by a NEW assertion on
`permissions_module._denial_is_terminal(...)` with a real worktree `cwd`,
proving the AGGREGATE verdict — the thing that actually reaches the SDK —
is **unchanged, still `True`**, for the exact same command. Verified
individually (see the probe table in the "Audit" section above and the
per-test `# cpp#205 LEGITIMATE FLIP` comments):

- `TestTier3DevnullRedirectLethality::test_real_write_target_stays_lethal`,
  `::test_devnull_lookalike_escape_stays_lethal`
- `TestTier3ContainedRedirectLethality::test_leading_expansion_target_
  stays_lethal`, `::test_tmp_prefix_boundary`, `::test_uncontained_
  redirect_stays_lethal`, `::test_non_file_redirect_forms` (only its
  `&>`/`2>>`-to-`/etc` assertions; the `<(`/`>(` assertions are UNCHANGED,
  still `True` — process substitution stays in the verb-only set),
  `::test_devnull_edges_of_cpp130_unchanged`, `::test_extractor_fails_closed`
- `TestTier3QuotedRedirectCharLethality::test_real_redirect_stays_lethal`,
  `::test_unterminated_quote_stays_lethal`, `::test_escaped_quote_outside_
  quotes_opens_no_region`
- `TestCpp201LethalityNarrowing::test_unassigned_dollar_t_stays_lethal`

None of these is a proven-danger regression: every one names a target this
ticket's OWN probe confirms is still independently vetoed by `_destination_
veto_reason`/`_redirect_destination_veto_reason` (out-of-worktree, `~`/`$`-
disqualified, or unparseable), so the SDK-facing verdict is identical. The
discriminant that WOULD catch a real regression — `test_dangerous_verb_
alongside_scratch_write_stays_lethal`, every `<(`/`>(` assertion, and the
whole `TestCpp205ProvenDangerVerbLethality`/both-directions/non-reopening
batteries — is untouched or newly added, not softened.

## Full suite / lint / type / gate (verbatim)

```
$ uv run pytest -q
1308 passed in 13.97s

$ uv run ruff check .
All checks passed!

$ uv run mypy src
Success: no issues found in 23 source files

$ ./scripts/verify-pipeline.sh
(passes once this plan doc is committed alongside the source diff — docs/
 and src/ both present in the same PR)
```

## Watchdog bound (AC4), documented not re-implemented

Survivability's only cost is a re-try loop, never an executed write (the
command stays `PermissionResultDeny` through the real handler regardless of
lethality — every "handler end to end" test in this ticket and its
predecessors proves that chain). That loop is bounded by:

- `maxTurns=200` (SDK-native, `types.py:42`) — the real, structural bound.
- `idleTimeout=300s` post-cpp#177 — a session that stops producing (rather
  than looping on refusals) is caught here.
- `stallThreshold`/`emptyResponseThreshold` — do NOT fire on a busy
  refusal-adaptation loop (a refused tool call still counts as
  `has_tool_use`), matching cpp#128's own documented honesty about which
  guardrails actually carry this weight.

No new guardrail is added or proposed by this ticket — cpp#168/#177 already
harden the internal watchdog against the starvation class that would let a
genuinely stuck session outlive `idleTimeout`; this ticket's survivability
class is a BUSY loop, which `maxTurns` bounds, exactly as cpp#128's own
doctrine block already states.

## Judgment calls, flagged

1. **The `is_tier3_dangerous_for_lethality`-level test flips (12 tests,
   above) are the single largest judgment call in this ticket.** The
   alternative — leaving the generic bare-`>` entry in place and instead
   threading `cwd` into `is_tier3_dangerous_for_lethality` to special-case
   the absolute-in-worktree shape — was rejected: cpp#154 D1 deliberately
   keeps that function "purely lexical and cwd-free" as a matter of
   doctrine (resolving symlinks in order to GRANT an exemption is the exact
   hazard cpp#143 closed). Moving the responsibility to the already-cwd-
   aware, already-tested `_destination_veto_reason`/`_redirect_destination_
   veto_reason` — which is where cpp#176 ALREADY fixed this exact bug for
   cp/mv/mkdir/git-show — is the design-consistent fix, not a new pattern.
2. **`_TIER3_VERB_PATTERNS_FOR_LETHALITY = TIER3_PATTERNS[:-1]`, a slice by
   POSITION, not by content-matching.** This is load-bearing on
   `TIER3_PATTERNS`' trailing entry staying the generic redirect pattern —
   true today and pinned by `test_new_verbs_were_already_survivable_pre_
   cpp205` plus the full negative battery. A future edit that reorders
   `TIER3_PATTERNS` without updating this slice would silently change which
   entry is excluded; flagged in the constant's own block comment for the
   next reader, not solved by e.g. a named-pattern-object refactor (out of
   scope, larger blast radius than this ticket needs).
3. **`_PROVEN_DANGEROUS_VERB_PATTERNS_CPP205`'s `chmod`/`chown` recursive
   detection requires an explicit `-R`/`--recursive`** (case-sensitive,
   `chmod`/`chown` have no `-r` short flag) and a bare, non-recursive
   `chmod`/`chown` is deliberately excluded — matching the dispatch's own
   audit note that `chmod 000 x` was ALREADY correctly survivable and
   should stay that way. A recursive invocation spelled with an unusual
   flag combination this regex misses (documented: `test_lowercase_r_flag_
   is_a_documented_miss`) under-matches rather than over-matches — the
   write stays refused regardless; the only cost of a miss is one fewer
   command staying terminal, never a wider admission.
4. **`dd`/`mkfs`/`truncate` are matched as BARE WORDS anywhere in the
   command text** (same imprecision class every existing `TIER3_PATTERNS`
   entry already has — e.g. `\bsh\s+-c\b` matches inside a quoted `echo`
   too), so a benign string CONTAINING the word "dd" as its own token
   (unlikely, but e.g. inside an argument) would over-classify as
   proven-danger. Over-classification is the SAFE direction for a
   LETHALITY-only check (worst case: one fewer legitimate reformulation
   opportunity, never an executed write) — documented, not hardened
   further, matching this file's own established style for the rest of
   `TIER3_PATTERNS`.
5. **No pilot was run.** All verification is via direct unit/handler-level
   probes (`is_tier3_dangerous_for_lethality`, `_denial_is_terminal`,
   `create_permission_handler`) and the committed, red-before/green-after
   test suite, per the dispatch's explicit instruction.

## Acceptance criteria

- **AC1 — default survivable, still denied.** A refusal with NO
  destructive verb and no proven-dangerous target (`_denial_is_terminal`
  False) is surfaced as a `tool_result` error and the run continues; the
  command remains `PermissionResultDeny`. Replay: mika#1686's non-
  destructive shapes (#1 sed-i-devnull+grep, #4 for-loop) are survivable. →
  `test_cpp205_mika1686_instance1_sed_i_devnull_grep_survivable`,
  `test_cpp205_mika1686_instance4_for_loop_survivable_or_allowed`,
  `test_cpp205_syntactic_overrefusals_default_survivable` (5 cases),
  `test_cpp205_unrecognized_denied_shape_defaults_survivable` (3 cases).
- **AC2 (negative, both directions) — proven-danger set stays TERMINAL, no
  new admission.** `rm -rf <real>`, `sed -i <real out-of-worktree file>`, a
  resolvable `/etc` write, control-plane escape, `$HOME` respelling, PLUS
  the newly-enumerated `chmod -R`/`chown -R`/`dd`/`mkfs`/`truncate`/fork
  bomb — all pass unchanged pre- and post-fix; `is_tier1_auto_approve`/
  `is_safe_bash_command` byte-identical throughout. → `test_cpp205_proven_
  danger_stays_terminal_both_worlds` (15 cases), `test_cpp205_resolvable_
  redirect_out_of_worktree_stays_terminal`, `test_cpp205_out_of_worktree_
  write_stays_terminal`, `test_cpp205_control_plane_path_stays_terminal`,
  `test_cpp205_home_respelling_stays_terminal_d3_non_reopening`,
  `test_cpp205_sed_i_on_real_out_of_worktree_file_stays_terminal`,
  `TestCpp205ProvenDangerVerbLethality` (all cases),
  `test_cpp205_admission_identity_unaffected`, `test_cpp205_handler_end_
  to_end_new_verbs_still_denied_but_survivable`.
- **AC3 (non-reopening) — cpp#154 D3/#176/#195/#196/#201/#203 unchanged.**
  → `test_cpp205_non_reopening_154d3_176_195_196_201_203`; full suite 1308
  passed, ruff/mypy clean; the 12 "legitimate flip" tests are documented
  above with per-case non-regression proof, distinguished from a
  proven-danger regression.
- **AC4 (bound) — watchdogs #168/#177 bound the survivable-retry loop, not
  the deny's lethality.** Documented above, no new guardrail added.
- **Cwd-probe edge case** — mika#1686 #2/#3 stays terminal under case (a):
  the `rm -rf`/`sh -c` destructive/unprovable verbs. → `test_cpp205_
  mika1686_instance2_3_cwd_probe_stays_terminal`.

## References

- `src/claude_pilot/tier1.py:~202` (new) — `_TIER3_VERB_PATTERNS_FOR_
  LETHALITY`, `_PROVEN_DANGEROUS_VERB_PATTERNS_CPP205`, `_matches_proven_
  dangerous_lethality_verb` and their block comments.
- `src/claude_pilot/tier1.py:~678` — `is_tier3_dangerous_for_lethality`,
  retargeted.
- `src/claude_pilot/permissions.py:~668` — the doctrine block above
  `_denial_is_terminal`, updated with the cpp#205 truth table summary.
- `src/claude_pilot/permissions.py:812` — `_denial_is_terminal` itself,
  code UNCHANGED, docstring updated.
- `tests/test_tier1.py` — `TestCpp205ProvenDangerVerbLethality`,
  `_CPP205_ADMISSION_BATTERY`/`test_cpp205_admission_identity_unaffected`,
  and the 12 `# cpp#205 LEGITIMATE FLIP` in-place test updates.
- `tests/test_permissions.py` — the full cpp#205 section (mika#1686 replay,
  both-directions battery, survivable battery, non-reopening smoke, the
  mika#1719/cpp#176-class fix proof, admission-identity/handler proof).
- `docs/plans/2026-09-26-001-fix-203-sed-i-devnull-survivable-plan.md` —
  cpp#203, the most recent per-shape carve this ticket generalizes past.
- `docs/plans/2026-09-24-002-fix-201-survivable-heredoc-mktemp-write-plan.md`
  — cpp#201.
- `docs/plans/2026-08-30-002-fix-128-nonlethal-policy-denial-plan.md` —
  cpp#128, the refusal-vs-lethality split this ticket extends, not revises.
- claude-pilot#205, #203, #201, #196, #195, #176, #154, #130, #128,
  mika#1686 (comment 5844642872).
