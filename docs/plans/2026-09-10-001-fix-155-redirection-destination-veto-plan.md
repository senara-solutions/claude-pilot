---
issue: claude-pilot#155
title: "`_segment_write_kind` does not classify redirections — destination veto is blind to `> /etc/passwd` - Plan"
type: fix
scope_repo: claude-pilot
priority: p1-security
date: 2026-09-10
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# `_segment_write_kind` does not classify redirections — destination veto is blind to `> /etc/passwd` - Plan

## Goal Capsule

**Objective.** `_destination_veto_reason` (`permissions.py`) — the worktree
containment (cpp#38) and control-plane (cpp#42) veto — classifies a
write-capable command segment STRUCTURALLY, by its leading command word
(`_segment_write_kind`). That classifier knows three shapes: `cp`/`mv`,
`mkdir`, and `git show … >`. A shell redirection from any OTHER verb —
`echo hi > /etc/passwd`, `cat > /etc/shadow`, `tee < in > /etc/passwd` — is
none of the three, so its target is never extracted and
`_destination_veto_reason` returns `None`: a containment gap on the function
whose whole job is to be the containment boundary.

Teach `_segment_write_kind` to also classify a real file-target shell
redirect (`>`, `>>`, `N>`, `N>>`, `&>`, `&>>`) from any verb, so
`_destination_veto_reason` sees it too — while reusing, verbatim, the SAME
lexical `/tmp` scratch exception cpp#143/#154 already grant, so the
currently-allowed `cat > /tmp/x <<'EOF'` heredoc (`bash-cat-heredoc-tmp`,
cpp#154 AC4) is not regressed.

**Means.** A new write-kind, `bash-redirect`, added to `_segment_write_kind`
as a FALLBACK after the four existing leading-word cases (so `cp`/`mv`/
`mkdir`/`git show` keep their exact current classification and extraction,
unchanged). Its destination extraction reuses `tier1._redirect_targets` +
`tier1._mask_quoted_redirect_chars` — the same lexical extractor cpp#154/#157
already built and already import into `permissions.py`. Its `/tmp` exception
in `_destination_veto_reason` reuses `tier1._is_contained_redirect_target` +
the `/tmp/` literal prefix — the SAME predicate `_redirect_destination_veto_
reason` (cpp#154) already uses to answer the identical question for the
LETHALITY axis, so the two can never drift on what counts as sanctioned
scratch.

**Authority hierarchy.** `senara-solutions/claude-pilot#155` body (which
supersedes and closes decision D4 of `docs/plans/2026-09-04-002-fix-154-
redirection-fichier-letalite-plan.md`, the deferred follow-up it names in
those exact words) > this plan > implementer judgment.

**Stop conditions.** None that halt outright. The one non-negotiable
constraint is R1 below (cpp#154 AC4 / `bash-cat-heredoc-tmp` must stay
reachable, non-terminal, end to end) — see § AC4 Non-Regression Proof.

**Execution profile.** Two source files (`permissions.py`, `tier1.py` —
docstring-only in `tier1.py`, no logic change there), two test files
(`tests/test_permissions.py`, `tests/test_policy_devpilot.py`). Single repo,
`claude-pilot` only.

**Tail ownership.** Implementer opens the PR through green CI (gated, no
merge — MPC reviews a security-boundary change).

## Measurement — `main` before this fix (`ab6868c`)

Probed with the SAME real decision functions the cpp#154 plan probed
(`policy.evaluate`, `_bash_allow_is_chain_safe`, `_destination_veto_reason`,
`is_tier3_dangerous_for_lethality`, `_denial_is_terminal`), against a fresh
clone at `ab6868c` (HEAD of `main` at the time this plan was written):

| command | policy | chain_safe | `_segment_write_kind` (1st seg) | `_destination_veto_reason` | `_denial_is_terminal` |
|---|---|---|---|---|---|
| `echo hi > /etc/passwd` | deny (default) | False | `None` | **`None`** | True |
| `echo hi > ../escape` | deny (default) | False | `None` | **`None`** | True |
| `echo hi > ~/x` | deny (default) | False | `None` | **`None`** | True |
| `echo hi > $VAR/x` | deny (default) | False | `None` | **`None`** | True |
| `echo hi > .git/hooks/pre-commit` | deny (default) | False | `None` | **`None`** | True |
| `echo hi > esc/x` (esc → outside, symlink) | deny (default) | False | `None` | **`None`** | False* |
| `cat > /tmp/x <<'EOF' … EOF` | allow `bash-cat-heredoc-tmp` | True | `None` | `None` | (vacant, never consulted) |
| `echo hi > /tmp/scratch` | deny (default) | False | `None` | `None` | False |
| `grep -c a b >/dev/null` | allow `bash-grep` | False | `None` | `None` | False |

`*` `echo hi > esc/x` (a worktree symlink resolving outside) already measures
`_denial_is_terminal = True` on `main`, but NOT through
`_destination_veto_reason` — through `_redirect_destination_veto_reason`
(cpp#154), a separate function reached only from `_denial_is_terminal`. See
Two constats below.

Two constats drive the plan:

**M1 — the gap is real and exactly where the ticket says it is.**
`_destination_veto_reason` returns `None` for every one of the first six rows
— including `.git/hooks/pre-commit` (a control-plane write) and the symlink
escape `esc/x` — because `_segment_write_kind` never sees a bare `>`. This is
the function the ticket names, and it is blind exactly as measured.

**M2 — the AGGREGATE verdict (`_denial_is_terminal`) is already correct for
every one of these commands today, via TWO OTHER mechanisms cpp#154/#157
already built:** `is_tier3_dangerous_for_lethality` (an un-contained redirect
target is never stripped from the tier3 `>` pattern, so it stays lethal on
its own) and `_redirect_destination_veto_reason` (a cpp#154 function, reached
only from `_denial_is_terminal`, that re-derives containment/control-plane
specifically for redirect targets). **This fix changes NO externally
observable `_denial_is_terminal` verdict for any command in this table** —
every row's `_denial_is_terminal` value is identical before and after. What
changes is that `_destination_veto_reason` itself — the ONE function also
consulted on the ALLOWED path (`create_permission_handler`, the
`pd.decision == "allow"` branch) — now proves the same thing directly,
instead of leaving that path uncovered for any future allow rule on a
redirecting verb. This is architectural completeness and defense-in-depth
against a *future* regression, not a fix to a *live* exploit: no YAML rule
today allows an unrestricted echo/cat/tee redirect (only the /tmp-pinned
`bash-cat-heredoc-tmp`), so nothing is reachable through the allow path today
that was not already correctly refused.

## Decisions

### D1 — The redirect classification is a FALLBACK, after the four existing cases

`_segment_write_kind` checks `cp`/`mv`, `mkdir`, `git show … >` FIRST, exactly
as today; only if none match does it check whether the segment carries a
real file-target redirect. A `cp`/`mv`/`mkdir`/`git show` segment that
*also* carries a trailing redirect (`cp a b > log`) keeps its existing
write-kind and existing (unchanged) destination extraction — extending that
combination is a different, un-measured gap and out of scope here.

### D2 — Reuse the exact `/tmp` lexical exception, not a new one

`_destination_veto_reason`'s new `bash-redirect` branch exempts a
destination when `_is_contained_redirect_target(dest) and
dest.startswith("/tmp/")` — the identical predicate
`_redirect_destination_veto_reason` already uses. `_is_contained_redirect_
target` (cpp#154, `tier1.py`) is purely lexical: no `..` anywhere, not
`~`/`$`-leading, a restricted charset (`[\w./$@{}-]+`, admitting mid-path `$n`
parameter expansion — the exact shape of the founding mika#2158 incident's
`/tmp/2158bodies/$n.md` targets). Reusing it verbatim — not
reimplementing an equivalent regex — is what guarantees the two functions
cannot answer "is this /tmp scratch" differently for the same string.

### D3 — `~`/leading-`$`/absolute-non-`/tmp`/`..` fail CLOSED before `is_within_project` ever runs

`is_within_project` (the disk-resolving containment check cp/mv/mkdir already
use) does no shell-expansion: `Path(cwd) / "~/x"` and `Path(cwd) / "$VAR/x"`
resolve to literal same-named subdirectories INSIDE the worktree and would
read as "contained" — the opposite of what bash would actually do (expand to
the real home directory, or an unpredictable value). `cp`/`mv`/`mkdir` never
hit this problem because their YAML allow rules already reject `~`/`$`/`..`/
absolute operands before ever reaching `_destination_veto_reason` — but a
redirect has no such upstream YAML gate (there is no generic "echo redirect"
allow rule). So the new branch checks `_is_contained_redirect_target(dest)`
FIRST and vetoes immediately (fail-closed, before any resolve) when it is
`False` — which is exactly the `~`/leading-`$`/`..`/absolute-outside-`/tmp`
disqualifier set. A destination that DOES pass (worktree-relative, no `..`)
falls through to the existing `is_within_project` resolve, unchanged — so a
symlink escape via a bare redirect (`echo hi > esc/x`, `esc` → outside) is
still caught the same way cp/mv/mkdir escapes already are.

### D4 — `/dev/null` is exempted explicitly, mirroring cpp#130

`_is_contained_redirect_target("/dev/null")` is `False` by its own docstring
("covered upstream … deliberately NOT duplicated here"). Without an explicit
skip, `grep -c a b >/dev/null` — non-terminal since cpp#130, and pinned by
`test_denial_is_terminal_predicate` and `TestTier3RedirectLethality` — would
regress into a hard destination veto the moment a bare redirect became a
classified write-kind. The new branch checks `dest == "/dev/null"` before
the containment check and skips it (writes nowhere; not a containment
question).

### D5 — Heredoc BODY text must never be read as a second command

`_destination_veto_reason` splits the command into segments with
`_split_compound_command`, which splits on a bare newline (cpp#103) as well
as `&&`/`||`/`;`/`|`. A sanctioned `cat > /tmp/x <<'EOF'` heredoc can carry
ANY text in its body (the quoted delimiter makes it inert — bash performs no
expansion, cpp#47) — including a line that reads, verbatim, like a dangerous
redirect: `echo hi > /etc/passwd`. Before this fix such a line was harmless
BECAUSE `_segment_write_kind` didn't classify `echo` at all. After teaching
it to, a naive per-segment walk would misclassify that inert body line as a
live write and veto a routine, currently-allowed heredoc — a NEW class of
false positive this fix must not introduce.

Fix: `_destination_veto_reason` checks `_is_sanctioned_pure_heredoc(command)`
(the same predicate `_bash_allow_is_chain_safe` already uses to admit this
one heredoc shape) BEFORE splitting, and returns `None` immediately when it
matches. This is provably safe, not merely convenient: `_is_sanctioned_pure_
heredoc` requires the WHOLE first line to be the opener
(`_SANCTIONED_HEREDOC_OPENER_RE`, itself already pinned to
`/tmp/(?!.*\.\.)[\w./-]+`) and nothing executable after the closing `EOF`
line — so a command this predicate accepts cannot contain a second real
command, only inert body text. See `test_destination_veto_heredoc_body_text_
not_misread_as_a_command` / `test_cpp155_heredoc_body_text_is_not_misread_
as_a_live_redirect`.

### D6 — Out of scope: argument-based write verbs (`tee FILE`, `touch FILE`)

The ticket's own scope section, and the doctrine block above
`_denial_is_terminal` this fix updates, name a SECOND, DIFFERENT residual
debt: a verb whose OWN ARGUMENT names a write target with no shell redirect
at all (`tee /etc/passwd`, `touch /etc/passwd`, `dd of=/etc/passwd`). This
fix does not touch that class — `_segment_write_kind("tee /etc/passwd")` is
still `None` after this change, pinned by
`test_segment_write_kind_classifies_redirects_from_any_verb`. `tee > file`
(redirected via the shell, not via its own argument) IS covered — the
distinction is the presence of a real `>` operator, which is exactly what
`_segment_write_kind`'s new branch keys on. Closing the argument-based class
is a separate, unmeasured change with its own per-verb argument-parsing
surface (mirroring `_extract_cp_mv_destination`'s complexity, once per verb)
and is explicitly left open.

## AC4 Non-Regression Proof (cpp#154, `docs/plans/2026-09-04-002-…-plan.md` § D4)

**The trap.** `_destination_veto_reason` is consulted at TWO sites:
`_denial_is_terminal` (a refused command's lethality), and — unconditionally,
`interrupt=True` as a Python literal (cpp#128) — the ALLOWED path in
`create_permission_handler`, immediately after a policy `allow` passes
chain-safety. `cat > /tmp/x <<'EOF'` is allowed today by `bash-cat-heredoc-
tmp`. If the new `bash-redirect` classification vetoed its own `/tmp` target,
this fix would silently withdraw a rule cpp#154's AC4 pins as reachable end
to end — the exact regression the ticket calls out by name.

**The proof, in three layers:**

1. **Unit.** `_segment_write_kind("cat > /tmp/cpp155_ac4_probe.rs <<'EOF'")
   == "bash-redirect"` — the new classification DOES fire on the heredoc's
   opener line (pinned so the proof below cannot be mistaken for a no-op).
   `_destination_veto_reason(cmd, cwd) is None` — and yet the veto still does
   not fire, because the target `/tmp/cpp155_ac4_probe.rs` passes the D2
   exception.
2. **Component chain.** `evaluate(...).decision == "allow"`,
   `.rule_id == "bash-cat-heredoc-tmp"`, `_bash_allow_is_chain_safe(...) is
   True` — unchanged, this fix touches neither.
3. **End to end.** `create_permission_handler(...)` driven with the real
   `command`; `isinstance(result, PermissionResultAllow)`.

Test: `tests/test_policy_devpilot.py::test_cpp155_bash_cat_heredoc_tmp_ac4_not_
regressed`, alongside the PRE-EXISTING
`test_cpp154_bash_cat_heredoc_tmp_is_reachable_end_to_end` (untouched, still
green — proves cpp#154's own promise independent of this fix).

**The reused /tmp exception is the lexical one, proven twice:**
`_is_contained_redirect_target` (`tier1.py`) does no `Path.resolve()`, no
`stat`, no `cwd` — it is `(str) -> bool` on the literal text as written,
exactly the cpp#143 lesson (`permissions.py:922-968` in the pre-fix tree):
resolving symlinks to GRANT an exemption is unsafe (a worktree symlink
`esc -> ../../../tmp` would wrongly qualify). `_destination_veto_reason`'s
new branch calls this SAME function — imported, not reimplemented — so
`test_cpp155_destination_veto_reason_tmp_scratch_and_worktree_relative_still_
none` and `_redirect_destination_veto_reason`'s own existing tests
(`TestTier3ContainedRedirectLethality` in `tests/test_tier1.py`) are, by
construction, testing the identical predicate.

## Requirements / Test Evidence

### Anti-vacuity (captured red, `git stash` of `permissions.py`+`tier1.py`
against the new tests, pre-fix source):

```
FAILED tests/test_permissions.py::test_segment_write_kind_classifies_redirects_from_any_verb
FAILED tests/test_permissions.py::test_destination_veto_now_fires_for_redirects_to_system_or_escaped_paths
FAILED tests/test_permissions.py::test_destination_veto_symlink_escape_via_bare_redirect
FAILED tests/test_permissions.py::test_denial_is_terminal_redirect_gap_closed_at_destination_veto_too
FAILED tests/test_policy_devpilot.py::test_cpp155_destination_veto_reason_now_sees_redirects
FAILED tests/test_policy_devpilot.py::test_cpp155_bash_cat_heredoc_tmp_ac4_not_regressed
6 failed, 269 passed in 1.00s
```

### Green, post-fix, full suite

```
1062 passed in 12.36s
```

(1050 pre-existing + 12 new: 7 in `tests/test_permissions.py`, 5 in
`tests/test_policy_devpilot.py`.)

### `uv run ruff check .` / `uv run mypy src`

Both clean (`All checks passed!` / `Success: no issues found in 23 source
files`).

## Non-Goals / Residuals Named, Not Closed

- **D6** above — argument-based write verbs (`tee FILE`, `touch FILE`, `dd
  of=FILE`) with no shell redirect. Still non-terminal-but-refused, unchanged.
- Removing/merging `_redirect_destination_veto_reason` into the newly-widened
  `_destination_veto_reason`, even though the former is now a logical subset
  of the latter for redirect targets. Left in place, deliberately, as
  defense-in-depth on a containment-boundary PR — see its updated docstring.
  `_denial_is_terminal` still calls both; redundancy here cannot produce a
  false ALLOW (both must independently return `None`), only, at worst, a
  duplicate `True`.
- A `cp`/`mv`/`mkdir`/`git show` segment that ALSO carries a trailing,
  unrelated redirect (D1) — pre-existing, unmeasured, unaffected by this fix.

## References

- `senara-solutions/claude-pilot#155` — the ticket.
- `docs/plans/2026-09-04-002-fix-154-redirection-fichier-letalite-plan.md`
  § D4 — the deferred follow-up this plan closes.
- `src/claude_pilot/permissions.py` — `_segment_write_kind`,
  `_extract_write_destinations`, `_destination_veto_reason` (all edited);
  `_redirect_destination_veto_reason`, `_is_sanctioned_pure_heredoc`,
  `_is_sanctioned_tmp_scratch` (read, reused, docstrings updated).
- `src/claude_pilot/tier1.py` — `_redirect_targets`,
  `_is_contained_redirect_target`, `_mask_quoted_redirect_chars`,
  `is_tier3_dangerous_for_lethality` (read, reused via import; docstrings
  updated, no logic change).
- `src/claude_pilot/policies/permissions.yaml:215` — `bash-cat-heredoc-tmp`.
- `tests/test_permissions.py`, `tests/test_policy_devpilot.py`,
  `tests/test_tier1.py` (existing suites replayed for non-regression).
