---
issue: claude-pilot#201
title: mktemp/heredoc write-destination veto TERMINAL despite refusal being correct — rendre survivable - Plan
type: fix
scope_repo: claude-pilot
priority: p1-lethality
date: 2026-09-24
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# mktemp/heredoc write-destination veto TERMINAL despite refusal being correct — rendre survivable - Plan

## Goal Capsule

**Objectif.** mika#2458: a dev-groom died (terminal, `PIPELINE_INCOMPLETE`,
$7.71) on the compound `cd <wt>/mika ; T=$(mktemp -d) ; cat >"$T/log"
<<'EOF' … EOF`. Only the first `cd` line was read at triage; the ACTUAL
cause is the heredoc write to `$T/log`. Established at source (HEAD
`d66dba5`, probed): this compound is `policy=deny(rule_id=None)`,
`_denial_is_terminal=True`. The write to a `$`-bearing, lexically
unresolvable target is correctly REFUSED (fail-closed: it cannot be proven
contained — `_is_lexically_disqualified_redirect_target`, cpp#154 D3) but
the refusal kills the session — the same lethality class cpp#195/#196
(`_denial_is_terminal` / write-destination-veto path) closed for
`git show … > /tmp/x`. This plan makes the class survivable without
loosening the refusal in either direction, and without touching the
ADMISSION gate (`is_tier1_auto_approve`).

## Cause established at source (HEAD `d66dba5`, probed in this worktree)

Probe (`policy.evaluate`, `_denial_is_terminal`, `is_tier3_dangerous_for_
lethality`, `_destination_veto_reason`, `_redirect_destination_veto_reason`,
`_segment_write_kind`, `_extract_write_destinations`) on the exact incident
command and controls, `cwd` = a temporary worktree:

| function | verdict on the incident command | why |
|---|---|---|
| `policy.evaluate` | `deny`, `rule_id=None` | no matching rule — default-deny |
| `_segment_write_kind("cat >\"$T/log\" <<'EOF'")` | `bash-redirect` | classified structurally (cpp#155) |
| `_extract_write_destinations` | `['"$T/log"']` | target text, quotes included (extraction is quote-blind) |
| `is_tier3_dangerous_for_lethality(command)` | `True` | the bare `>` pattern is never stripped for a target that fails `_is_contained_redirect_target` — and `"$T/log"` fails it (charset rejects the embedded quotes / leading `$`) |
| `_destination_veto_reason(command, cwd)` | non-`None` ("... is not a literal contained path ... denied fail-closed") | `_is_lexically_disqualified_redirect_target` disqualifies the target outright |
| `_denial_is_terminal(...)` (pre-fix) | **`True`** | **the bug** — `is_tier3_dangerous_for_lethality`'s `True` alone is enough to short-circuit `_denial_is_terminal` to terminal, before the destination-veto checks are even reached |

**Two independent contributors, both needed fixing.** `_denial_is_terminal`
is `is_tier3_dangerous_for_lethality(command) or
_redirect_destination_veto_reason(command, cwd) or
_destination_veto_reason(command, cwd) is not None`. For this command the
FIRST check alone already returns `True` (the redirect is never stripped
from the bare-`>` TIER3 pattern), so fixing only `_destination_veto_reason`
would have been silently masked by the still-`True` first check. Both
`is_tier3_dangerous_for_lethality`'s redirect-stripping (`tier1.py`) and
`_destination_veto_reason`'s per-target loop (`permissions.py`) needed the
same narrowing, or the two would drift apart — exactly the failure mode
cpp#151 B0 names.

## The cut — considered, tried, and REJECTED first

The dispatch's own framing ("an UNRESOLVABLE target — contains `$`, a
command substitution, a `~` that needs runtime state — cannot be resolved
statically") was implemented FIRST, literally: any redirect target
disqualified specifically because it contains `$` (regardless of which
variable) was exempted from the lethality-only bare-`>` strip. **This was
WRONG and caught by the existing test suite itself**, not by inspection:

```
FAILED tests/test_permissions.py::test_denial_is_terminal_redirect_gap_closed_at_destination_veto_too
FAILED tests/test_policy_devpilot.py::test_cpp154_home_expansion_target_stays_terminal
FAILED tests/test_policy_devpilot.py::test_cpp157_a_real_redirect_still_ends_the_run
FAILED tests/test_tier1.py::TestTier3ContainedRedirectLethality::test_leading_expansion_target_stays_lethal
FAILED tests/test_tier1.py::TestTier3QuotedRedirectCharLethality::test_real_redirect_stays_lethal
5 failed, 1168 passed in 14.60s
```

These five tests are a RATIFIED invariant from cpp#154 plan D3 / cpp#157's
AC replay, not incidental coverage: `$HOME/x`, `${HOME}/.bashrc`,
`$OLDPWD/y`, `$(whoami)`, and a bare `$` are pinned to stay TERMINAL, on
purpose, with the rationale spelled out in the code itself — "`$HOME/x`
names the same destination as `~/x`; admitting it would make the `~`
rejection one respelling away from useless." A blanket "any `$` disqualified
target is survivable" rule silently reopens that closed hole: it would make
`echo hi > $HOME/.ssh/authorized_keys` non-terminal, undoing a deliberate
anti-respelling protection — an actual regression, not a conservative
widening.

**Judgment call, flagged.** I corrected course to a narrower cut BEFORE
opening any PR, verified it against the code, and it was independently
reviewed and confirmed by the dispatching seat mid-task (see `Review
record` below).

## The fix — the mktemp idiom specifically, not "any `$`"

The incident's `$T` is qualitatively different from `$HOME`: it is not an
ambient/environment variable an attacker or a confused pilot can point
anywhere — it is assigned, in the SAME command, from the literal output of
`mktemp`, a fixed system utility whose contract is a fresh path under
`$TMPDIR`/`/tmp` (the ticket's shape never supplies a template argument that
would say otherwise). That is provable LEXICALLY, without resolving
anything on disk (cpp#143's rule holds: never resolve to GRANT — this
recognizes a fixed textual idiom instead, the same way
`_is_sanctioned_pure_heredoc` already recognizes the
`bash-cat-heredoc-tmp` idiom).

**New predicates, `src/claude_pilot/tier1.py`** (pure, lexical, no `cwd`,
consulted ONLY for lethality):

- `_MKTEMP_ASSIGNMENT_RE` / `_mktemp_scratch_variable_names(command)` — every
  variable name the command assigns, anywhere in its raw text, from
  `$(mktemp …)` or `` `mktemp …` ``.
- `_MKTEMP_SCRATCH_TARGET_RE` / `_is_mktemp_scratch_redirect_target(command,
  dest)` — whether an already-disqualified `dest` is a `$VAR`/`"$VAR"`/
  `${VAR}` reference (optionally followed by a `..`-free relative tail,
  anchored both ends) where `VAR` is one of `command`'s mktemp-assigned
  names.

**Two call sites updated to consult it, both LETHALITY-only:**

1. `tier1._strip_contained_redirects` (the only caller of which is
   `is_tier3_dangerous_for_lethality`, consulted ONLY by
   `permissions._denial_is_terminal` — verified by grep, zero other
   callers): now strips a redirect whose target is `_is_contained_redirect_
   target` **OR** `_is_mktemp_scratch_redirect_target(command, target)`.
2. `permissions._destination_veto_reason` gains an `for_lethality: bool =
   False` keyword. Every existing call site keeps the default (`False`,
   byte-identical behavior — the REFUSAL question, unchanged). The single
   new call, `_denial_is_terminal`'s final line, passes `for_lethality=True`:
   inside the per-target loop's disqualification branch, when `for_lethality`
   is `True` and `_is_mktemp_scratch_redirect_target(command, dest)` is
   `True`, the loop `continue`s (no veto contribution to lethality) instead
   of returning the veto-reason string. The string itself, and every other
   caller's behavior, is completely unchanged.

**`_denial_is_terminal` diff (before/after), `permissions.py:767+`:**

```diff
     if is_tier3_dangerous_for_lethality(command):
         return True
     if _redirect_destination_veto_reason(command, cwd) is not None:
         return True
-    return _destination_veto_reason(command, cwd) is not None
+    return _destination_veto_reason(command, cwd, for_lethality=True) is not None
```

(`is_tier3_dangerous_for_lethality` itself is unchanged at this call site —
its OWN narrowing, inside `_strip_contained_redirects`, is what changed;
see point 1 above.)

**What is explicitly NOT touched:** `is_tier3_dangerous` (the REFUSAL
classifier), `is_safe_bash_command`, `is_tier1_auto_approve`,
`SAFE_SHELL_COMMANDS`, any YAML allow-rule, the gate, `_redirect_destination_
veto_reason`, `_is_lexically_disqualified_redirect_target`,
`_is_contained_redirect_target`. No env bypass added.

## AC1 message — the survivable deny now names the reason

`create_permission_handler`'s `pd.decision == "deny"` branch (the site that
already computes `deny_terminal = _denial_is_terminal(...)` and used to
always set `message=pd.reason`, e.g. the generic "no matching policy rule —
denied by default", which names no destination): now, scoped tightly to
`tool_name == "Bash" and not deny_terminal`, it additionally calls
`_destination_veto_reason(command, cwd)` (the REFUSAL-facing default,
`for_lethality=False`) and, if it returns a reason, uses that string
VERBATIM as the deny message instead of `pd.reason` — reusing the existing
string per the ticket's own preference, not inventing new wording.

This can only ever fire for the exact class cpp#201 creates: before this
fix, "non-terminal AND `_destination_veto_reason` non-`None`" was
UNREACHABLE (every destination veto was unconditionally terminal), so no
pre-existing deny's message is affected — confirmed by the full suite
passing unmodified.

## Both-directions proof

**Positive (become NON-terminal, still denied) — red-before/green-after,
verbatim:**

Red (source stashed to `d66dba5`, only the two new cpp#201 tests that assert
survivability fail; the anti-vacuity/negative ones — written against the
SAME pre-fix HEAD — already pass, proving they test a real property, not a
vacuous one):

```
tests/test_permissions.py ...
FAILED tests/test_permissions.py::test_cpp201_denial_is_terminal_mika_2458_replay_survivable
FAILED tests/test_permissions.py::test_cpp201_denial_is_terminal_bare_survivable
FAILED tests/test_permissions.py::test_cpp201_handler_end_to_end_still_denied_but_survivable
3 failed, 17 passed, 58 deselected in 0.56s
```

Green (fix restored):

```
tests/test_permissions.py -k cpp201: 20 passed, 58 deselected
tests/test_tier1.py -k Cpp201: 16 passed, 440 deselected
```

- `test_cpp201_denial_is_terminal_mika_2458_replay_survivable` — the EXACT
  incident compound (`cd … ; T=$(mktemp -d) ; cat >"$T/log" <<'EOF' …
  EOF`) → `_denial_is_terminal(...) is False`, AND
  `_destination_veto_reason(...) is not None` (still refused).
- `test_cpp201_handler_end_to_end_still_denied_but_survivable` — same
  command through the REAL `create_permission_handler`:
  `PermissionResultDeny` (never `Allow`), `interrupt is False`, and
  `result.message` contains the destination-veto reason text (AC1).
- `test_cpp201_denial_is_terminal_bare_survivable` — same shape minus the
  leading `cd`, isolating that the fix is about the write, not `cd`.

**Negative (STAY terminal, both worlds) — pass unchanged pre- and
post-fix:**

`test_cpp201_negative_stays_terminal` (13 parametrized cases) —
`/etc/passwd`, `/var/outside/x` (resolvable, out-of-worktree, non-`/tmp`),
`rm -rf x`, `sed -i`, `git push --force`, AND the anti-widening battery:
`$HOME/…`, `${HOME}/…`, `$OLDPWD/…`, `$(whoami)`, bare `$`, `~/escape`,
`../escape`, a redirect to a DIFFERENT un-assigned variable in a command
that DOES assign one from mktemp (`'T=$(mktemp -d) ; cat > "$OTHER/log"'`),
and a traversal riding a legitimate scratch-var prefix (`"$T/../../etc/
passwd"`). `test_cpp201_unassigned_dollar_var_stays_terminal` — the SAME
`"$T/log"` shape, WITHOUT the `T=$(mktemp -d)` assignment anywhere in the
command, stays terminal — the anti-vacuity control that proves the
carve-out is keyed on the provable mktemp origin, not on the mere presence
of `$`.

## Non-reopening

`test_cpp201_non_reopening_cpp195_cpp154_ac4` replays the exact cpp#195/#196
mika#2471 command (`_denial_is_terminal(...) is False`, unchanged) and the
cpp#154 AC4 sanctioned heredoc shape (`_destination_veto_reason(...) is
None`, unchanged — it is an ALLOW, never reaches the deny path at all).
Full suite: every cpp#128/#151/#154/#155/#166/#176/#195/#196 test file is
UNMODIFIED by this diff (only `test_permissions.py` and `test_tier1.py`
gained NEW tests; no existing assertion was edited) and passes.

## Admission-identity proof (sovereign constraint, verified twice)

`_is_mktemp_scratch_redirect_target` / `_mktemp_scratch_variable_names` are
reachable ONLY via `tier1._strip_contained_redirects` (sole caller:
`is_tier3_dangerous_for_lethality`, itself consulted ONLY by
`permissions._denial_is_terminal` per its own docstring, verified by grep)
and via `permissions._destination_veto_reason`'s new `for_lethality=True`
kwarg (default `False` everywhere else, zero behavior change for every
pre-existing caller). Neither is reachable from `is_tier1_auto_approve` →
`is_safe_bash_command` → `is_tier3_dangerous` (the unnarrowed REFUSAL
classifier admission is built on) — disjoint call graphs, confirmed by
grep.

A 15-command battery (the incident compound, the bare heredoc, a `/tmp`
heredoc, `jq`, `git status`/`log`/`show`-redirect, `rm -rf`, `sed -i`,
`mktemp` variants, `$HOME`, `curl`, `ls`, `grep`) run through
`is_tier1_auto_approve` on `main` (HEAD `d66dba5`, via `git stash`) and on
this branch: **byte-identical, zero drift** during development. That
`git stash` repro is NOT what ships — the operator required this proof to
be a COMMITTED, durable test, not a one-off diff: the SAME 15-command
battery is now pinned as `test_cpp201_admission_battery_unaffected`
(parametrized, `tests/test_tier1.py`), asserting each command's expected
`is_tier1_auto_approve` verdict explicitly. It fails loudly on any FUTURE
change that shifts admission, not just on this one.
`test_admission_classifier_unaffected` (in `test_tier1.py`) additionally
pins this at the unit level for the incident command specifically:
`is_tier3_dangerous(...)`,
`is_safe_bash_command(...)`, and `is_tier1_auto_approve(...)` all return
their pre-fix values.

## Full suite / lint / gate (verbatim)

```
$ uv run pytest -q
1224 passed in 14.15s   (1173 baseline + 31 new tier1 tests + 20 new permissions tests)

$ uv run ruff check .
All checks passed!

$ uv run mypy src
Success: no issues found in 23 source files

$ ./scripts/verify-pipeline.sh
(pass once this plan doc is present alongside the source diff)
```

## Review record

Mid-task, the dispatching seat (samidarko-claude/ssc) placed a sovereign
HOLD pending scope confirmation, specifically challenging whether
`_is_unresolvable_redirect_target` (the FIRST, broad "any `$`" cut) touched
tier1 ADMISSION. Answered with the call-graph proof above and the
byte-identical admission battery; the seat then confirmed the ALREADY-IN-
PROGRESS narrowing (to the mktemp-assigned-in-the-same-command idiom,
independently discovered via the 5 failing ratified tests) as the correct
both-directions cut, and asked for exactly the proofs this doc now carries
(a–f). PR remains gated — not opened/merged from this task; the operator's
explicit go-ahead is relayed separately.

## Acceptance criteria

- **AC1 — exact replay survivable, still refused, with a clear message.**
  The exact mika#2458 command
  (`cd <wt>/mika ; T=$(mktemp -d) ; cat >"$T/log" <<'EOF' … EOF`) yields
  `_denial_is_terminal(...) is False`; via `create_permission_handler`,
  `PermissionResultDeny` (never `Allow`) with `interrupt is False`; and the
  deny `message` carries the existing `_destination_veto_reason` string
  (not the generic default-deny reason), so the pilot has something to
  adapt to. → `test_cpp201_denial_is_terminal_mika_2458_replay_survivable`,
  `test_cpp201_denial_is_terminal_bare_survivable`,
  `test_cpp201_handler_end_to_end_still_denied_but_survivable`.
- **AC2 — resolvable out-of-worktree and tier3-dangerous stay terminal; the
  ratified `$HOME` anti-respelling invariant is not reopened.**
  `/etc/passwd`, `/var/outside/x`, `rm -rf`, `sed -i`, `git push --force`,
  `~/escape`, `../escape`, AND — the anti-widening proof specific to this
  ticket's own cut — `$HOME/…`, `${HOME}/…`, `$OLDPWD/…`, `$(whoami)`, a
  bare `$`, a DIFFERENT un-assigned variable in a command that does assign
  one from mktemp, and a traversal riding a legitimate scratch-var prefix,
  all stay `_denial_is_terminal(...) is True`, on both `main` and this
  branch. → `test_cpp201_negative_stays_terminal` (13 cases),
  `test_cpp201_unassigned_dollar_var_stays_terminal`; pre-existing
  `test_cpp154_home_expansion_target_stays_terminal`,
  `test_cpp157_a_real_redirect_still_ends_the_run`,
  `test_leading_expansion_target_stays_lethal`,
  `test_real_redirect_stays_lethal`,
  `test_denial_is_terminal_redirect_gap_closed_at_destination_veto_too`
  UNMODIFIED and green.
- **AC3 — non-reopening cpp#128/#195/#196/#154-AC4, and zero admission
  drift.** The cpp#195/#196 mika#2471 `/tmp` git-show-redirect replay stays
  survivable; the cpp#154 AC4 sanctioned `bash-cat-heredoc-tmp` shape is
  unaffected (it is an ALLOW, never reaches this deny path). Full suite for
  cpp#128/#151/#154/#155/#166/#176/#195/#196 is unmodified and green. The
  15-command `is_tier1_auto_approve` admission battery is byte-identical
  between `main` (HEAD `d66dba5`) and this branch. →
  `test_cpp201_non_reopening_cpp195_cpp154_ac4`,
  `test_admission_classifier_unaffected`,
  `test_cpp201_admission_battery_unaffected` (15 parametrized cases,
  committed), full suite (1224 passed), the
  stash-diff admission battery above.

## References

- `src/claude_pilot/permissions.py:767` — `_denial_is_terminal`.
- `src/claude_pilot/permissions.py:1106` — `_destination_veto_reason`,
  gains `for_lethality`.
- `src/claude_pilot/tier1.py:355` — the mktemp-scratch predicates (new).
- `src/claude_pilot/tier1.py:575` — `is_tier3_dangerous_for_lethality`.
- `docs/plans/2026-09-22-002-fix-195-tmp-redirect-refusal-survivable-plan.md`
  — cpp#195/#196, the same lethality-path class, /tmp git-show-redirect.
- `docs/plans/2026-08-30-002-fix-128-nonlethal-policy-denial-plan.md` —
  cpp#128, refusal vs. lethality split.
- `docs/plans/2026-09-04-002-fix-154-redirection-fichier-letalite-plan.md`
  — cpp#154, D3: the `$HOME`-as-`~`-respelling invariant this ticket does
  NOT reopen.
- claude-pilot#201, mika#2458.
