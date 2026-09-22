---
issue: claude-pilot#190
title: "bash-grep readonly research chain (echo && sed -n && grep) denied inside a compound — resolves the cpp#189 class - Plan"
type: fix
scope_repo: claude-pilot
priority: p1-important
date: 2026-09-22
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# bash-grep readonly research chain (echo && sed -n && grep) denied inside a compound — resolves the cpp#189 class - Plan

## Goal Capsule

**Objective.** A read-only research chain — `echo "…" && sed -n '<range>p' <file>
&& echo "…" && grep -n <pattern> <files…>` — halted a dev-groom architect pass
(mika#2331, cpp#190) and, in a sibling shape, produced five non-terminal denies
during a successful groom (mika#2334, cpp#189). Every segment of these chains is
read-only: no write, no network, no `rm`/`mv`, no redirection. The only thing
that made them fail was `sed -n '<addr-range>p'` (print a line range,
print-only) — a shape that had **no allow-list entry anywhere** in the general
(non-contained) permission path, because `sed` is deliberately excluded from
`SAFE_SHELL_COMMANDS` entirely (cpp#27 — both `sed` and `awk` are
general-purpose interpreters with arbitrary-code sub-features an exhaustive
guard can't enumerate). Once the `sed -n` segment failed, per-segment
chain-safety denied the WHOLE compound — logged under whichever unrelated
rule_id `policy.evaluate`'s first-match-wins `re.search` happened to match on
the raw string (`[bash-grep]`, because `\sgrep\s` matched a LATER segment of
the same command; the log line names the rule that matched, not the rule that
actually decided).

**Fix.** Add one narrow, closed-world, self-contained predicate —
`_is_safe_sed_print_only` (`src/claude_pilot/tier1.py`) — that admits EXACTLY
`sed -n '<addr>[,<addr>]p' [FILE...]`: `-n` mandatory and the sole flag, the
single-quoted script is exactly one address or address range followed by the
bare `p` (print) command with the closing quote anchored right after it (so no
other sed command letter — `w`/`W` write, `e`/`r`/`R` exec/read, `d`/`s`/`y`/
`q`/`n`/`a`/`i`/`c`/`!` — can ride inside the script), no `-i`, no `-e`/`-f`
(multi-script / script-file). Wired into `_is_safe_sub_command`, the function
every compound segment is checked against in BOTH the tier1 fast path
(`is_tier1_auto_approve` → `is_safe_bash_command`) and the general chain-safety
guard (`permissions.py::_bash_allow_is_chain_safe`'s per-segment loop).

**What did NOT need to change.** `git show <ref>:<path>` (no redirect) was
already tier1-safe standalone (`is_safe_git_command` — `show` is a
`SAFE_GIT_SUBCOMMANDS` member) and therefore already safe as a chain segment.
Bare `grep`/`echo`/`ls`/`tail`/`cat` segments, and `gh pr create --help` piped
into `grep`, were already tier1-safe (`grep`/`echo`/`ls`/`tail`/`cat` are
unconditional `SAFE_SHELL_COMMANDS` members; `gh pr create` is on
`SAFE_GH_SUBCOMMANDS`). The per-segment chain-safety LOOP in
`_bash_allow_is_chain_safe` (`permissions.py:654-664`) already admits a chain
whose every segment is individually tier1-safe or a clean policy allow — that
mechanism needed no change. The fix is exactly one new leaf predicate; the
compound-safety machinery around it is untouched.

## Rule locus (confirmed in-repo, not the SDK)

```
$ grep -rn 'bash-grep' src/
src/claude_pilot/tier1.py:782        (doc comment, unrelated code path)
src/claude_pilot/permissions.py:376  (doc comment referencing the misattribution)
src/claude_pilot/permissions.py:635  (doc comment, same)
src/claude_pilot/permissions.py:637  (doc comment, same)
src/claude_pilot/permissions.py:831  (doc comment, same)
src/claude_pilot/permissions.py:1593 (doc comment, same)
src/claude_pilot/policies/permissions.yaml:55   (doc comment)
src/claude_pilot/policies/permissions.yaml:128  - id: bash-grep
src/claude_pilot/policies/permissions.yaml:201  (doc comment)
```

All three files (`permissions.yaml`, `permissions.py`, `tier1.py`) are wholly
inside `claude-pilot` (`src/claude_pilot/`). Nothing in this class touches
`claude-agent-sdk` (upstream) — `policy.py::evaluate` (first-match-wins regex
over the YAML rules), `tier1.py::is_safe_bash_command`/`_is_safe_sub_command`
(the allow-list), and `permissions.py::_bash_allow_is_chain_safe` (the
compound guard) are all claude-pilot-authored and fully overridable here. No
dispatch-context route is needed; this is an ordinary source fix.

## The compound-unsafe mechanism, precisely

1. `is_tier1_auto_approve("Bash", …)` calls `is_safe_bash_command(command)`,
   which — after the metachar/tier3 guards — splits the WHOLE command on
   `&&`/`||`/`;`/`|`/`&`/newline (`_split_compound_command`) and requires
   **every** segment to pass `_is_safe_sub_command`. Before this fix, a
   `sed -n '<range>p'` segment matched none of `is_safe_git_command` /
   `is_safe_build_command` / `is_safe_make_command` / `is_safe_shell_command`
   (sed is not in `SAFE_SHELL_COMMANDS`, cpp#27) / `is_safe_gh_command` /
   `is_safe_mika_dispatch` / `is_safe_exec_when_contained` — so the WHOLE
   command failed tier1, even though `echo`/`git show`/`grep`/`cat` segments
   in the same chain were each individually fine.
2. Denied at tier1, the command falls to the tier2 policy evaluator
   (`policy.py::evaluate`), which does a first-match-wins `re.search` of each
   YAML rule's pattern against the WHOLE raw string — not per-segment. In the
   cpp#190 founding command, no rule matches the `sed -n` segment (there was
   no sed rule of any kind in `permissions.yaml`), but `bash-grep`'s
   `"^grep\\s|\\sgrep\\s"` matches ` grep ` in the LAST segment, anywhere in
   the string — so `policy.evaluate` returns `decision=allow,
   rule_id=bash-grep` for the ENTIRE command, purely because of a segment that
   has nothing to do with the one that actually blocks it.
3. `permissions.py::_bash_allow_is_chain_safe` then re-splits the command and
   re-validates EVERY segment (mirroring tier1's allow-list model, per the
   compound-unsafe doctrine in
   `docs/solutions/security-issues/command-string-policy-allow-rules-are-compound-unsafe.md`
   §1/§11.2): for each segment, either `is_safe_bash_command(seg)` or a clean
   (non-tier3) policy allow on that segment alone. The `sed -n` segment fails
   both — no rule in `permissions.yaml` matches it either — so the guard
   returns `False` and the whole compound is denied. The deny is logged at the
   handler under the `rule_id` the TOP-LEVEL `policy.evaluate` call returned
   (`bash-grep`), which is why the operator-facing log names a rule that never
   actually decided anything — the compound-unsafe doctrine's §11.2 lesson
   ("classify write-capability structurally, never by the matched rule_id")
   applies symmetrically here to the DENY path's diagnostic, not just the
   allow path's security boundary.

## The fix

`src/claude_pilot/tier1.py`, adjacent to the existing `sed 's///'`
pure-substitution helper (`_is_safe_sed_pure_substitution`, itself wired ONLY
into the containment-gated `is_git_readonly_compound_when_contained` path —
not reachable by the ordinary, uncontained dev-groom pilot this ticket is
about):

```python
_SED_ADDR = r"(?:\d+|\$|/(?:[^/\\]|\\.)*/)"
_SAFE_SED_PRINT_RE = re.compile(
    rf"^\s*sed\s+-n\s+'{_SED_ADDR}(?:,{_SED_ADDR})?p'\s*(?:[A-Za-z0-9_./-]+\s*)*$"
)


def _is_safe_sed_print_only(sub: str) -> bool:
    """True iff sub is `sed -n '<addr>[,<addr>]p' [FILE...]` (print-only)."""
    return bool(_SAFE_SED_PRINT_RE.match(sub))
```

…and one new disjunct in `_is_safe_sub_command`:

```python
def _is_safe_sub_command(sub: str) -> bool:
    return (
        is_safe_git_command(sub)
        or is_safe_build_command(sub)
        or is_safe_make_command(sub)
        or is_safe_shell_command(sub)
        or is_safe_gh_command(sub)
        or is_safe_mika_dispatch(sub)
        or is_safe_exec_when_contained(sub)
        or _is_safe_sed_print_only(sub)   # cpp#189/#190 — new
    )
```

**Design choices (each evidence-gated to the exact forms seen, cpp#34
discipline — over-block is the safe direction):**

- `-n` is REQUIRED. Without it, `sed 'ADDRp' file` prints the addressed line
  TWICE (sed's default auto-print plus the explicit `p`) — read-only, but a
  DIFFERENT, unenumerated shape than the evidence. Deliberately not admitted.
- The script must be EXACTLY one address or address range (`NUMBER`, `$`
  last-line, or `/regex/` with the same `(?:[^/\\]|\\.)*` escape-aware
  charset the existing `s///` helper uses) followed by a bare `p` — nothing
  else. The closing `'` is anchored immediately after `p`, so `1,20p;w evil`,
  `1,20w evil`, `1,20e`, `1,20r /etc/passwd` all fail to match (proven in
  tests below) — not because of a denylist, but because the closed-world
  shape has no room for them.
- No `-e`/`-f` (multi-script / script-file) — only a single bare `-n
  '<range>p'` invocation is admitted.
- File operands share `_SAFE_CAT_RE`'s charset (`[A-Za-z0-9_./-]+`, no shell
  metacharacter, no redirect char) — consistent with every other bounded pipe
  tool in this file (`_SAFE_HEAD_TAIL_RE`, `_SAFE_CAT_RE`, `_SAFE_WC_RE`).
- **Not added to `SAFE_SHELL_COMMANDS`.** `is_safe_shell_command("sed …")`
  stays `False` for every sed shape, including this one — the cpp#27 doctrine
  (sed/awk excluded wholesale because their sub-features can't be
  exhaustively guarded) is untouched. `_is_safe_sed_print_only` is a separate,
  narrow, self-contained predicate called directly from
  `_is_safe_sub_command`, exactly the same relationship
  `_is_safe_sed_pure_substitution` already has to `is_safe_shell_command` —
  this fix extends an existing pattern, it does not introduce a new one.
- **No change to `_bash_allow_is_chain_safe`, `_split_compound_command`, or
  any other chain-safety machinery.** The per-segment loop already admits a
  chain whose every segment is individually safe; the ONLY thing missing was
  one segment predicate. This directly satisfies the ticket's "fix the INPUT"
  framing — the gate itself was never wrong, one legitimate read-only leaf
  form was simply absent from the leaf-safety allow-list.

## Both directions — writes stay denied (mandatory proof)

Nothing about redirection, substitution-based writes, or the pre-existing
`bash-git-show-redirect` sanctioned exception (cpp#35/#166) was touched. The
new predicate's own shape makes every write form structurally inadmissible
(closed-world: the closing quote sits right after `p`, so no other sed
command — including the write commands `w`/`W` — can appear), and this is
additionally proven end-to-end against the full decision chain, not just the
new predicate in isolation.

| Command | Before | After | Why |
|---|---|---|---|
| `sed -i 's/a/b/' f` | deny | deny | `-i` never matches `-n '<range>p'`; also independently tier3-dangerous (`\bsed\s+(-\w*i\|-i\w*)\b`) |
| `sed -n '1,20p;w evil' file` | deny | deny | closing `'` anchored after `p`; `;w evil` breaks the match |
| `sed -n '1,20w evil' file` | deny | deny | `w` is not `p`; no match |
| `sed -n '1,20e' file` | deny | deny | `e` is not `p`; no match |
| `sed -n '1,20r /etc/passwd' file` | deny | deny | `r` is not `p`; no match |
| `sed -n -e '1,20p' file` | deny | deny | `-e` is not the sole admitted flag; no match |
| `sed -n -i '1,20p' file` | deny | deny | `-i` present; no match |
| `sed '1,20p' file` (no `-n`) | deny | deny | `-n` is mandatory; no match |
| `sed -n '1,20p' file > out` | deny | deny | bare `>` is tier3-dangerous at the whole-command/segment level, upstream of `_is_safe_sub_command` entirely |
| `grep x f > out` | deny | deny | unchanged — bare `>` tier3-dangerous, and `_bash_allow_is_chain_safe`'s explicit `not is_tier3_dangerous(seg)` re-check on the segment |
| `echo hi && grep x f > out` | deny | deny | unchanged — the write segment still fails independently in the per-segment loop |
| `sed -n '1,20p' file && rm -rf ~` | deny | deny | the new read-only leaf passing does not make a DANGEROUS tail pass — `rm -rf` is still tier3-dangerous |
| `sed -n '1,20p' file && echo hi > out` | deny | deny | same — a write tail still fails independently |
| `echo "$(echo bad > /tmp/pwn)"` | deny | deny | unrelated axis (write sub-shell) — untouched by this fix |
| `git show <ref>:<path> > /etc/evil` | deny | deny | unchanged — `bash-git-show-redirect`'s target lookaheads (`(?!/)(?!~)(?!.*\.\.)`) still reject absolute/`..` targets |
| `git show <ref>:<path> > out` (safe relative target) | **allow** | **allow** | UNCHANGED, pre-existing cpp#35/#166 sanctioned exception — not granted by this fix, pinned in tests so a future change to the sed/grep read forms cannot be mistaken for having touched it |

## Positive tests — red-before / green-after (verbatim)

Filtered run of the new tests against **pristine `main`** (fix reverted via
`git stash push -- src/claude_pilot/tier1.py`, new tests kept):

```
$ uv run pytest -q tests/test_policy_devpilot.py -k "cpp190 or cpp189 or mika2471 or sed_in_place or git_show_redirect or grep_redirect_still or write_segment or write_subshell or cpp190_write_tail"
.................................FF...F.......                           [100%]
=================================== FAILURES ===================================
__________________ test_cpp190_founding_chain_now_chain_safe ___________________
    assert _bash_allow_is_chain_safe(_POLICY, "Bash", _bash(cmd)) is True
E   assert False is True
___________ test_cpp189_sed_range_pipe_cat_numbering_now_chain_safe ____________
    assert _bash_allow_is_chain_safe(_POLICY, "Bash", _bash(cmd)) is True
E   assert False is True
_______________ test_cpp190_sed_print_chain_full_handler_allows ________________
    assert isinstance(result, PermissionResultAllow)
E   AssertionError: assert False
    +  where False = isinstance(PermissionResultDeny(behavior='deny',
       message='policy allow (bash-grep) vetoed — command chains a
       tier3-dangerous or command-substitution tail onto the allowed prefix',
       interrupt=False), PermissionResultAllow)
----------------------------- Captured stderr call -----------------------------
[policy:deny] Bash: echo "label" && sed -n '1,20p' docs/plans/README.md &&
grep -n "x" docs/plans/README.md [bash-grep] (non-terminal)
=========================== short test summary info ============================
FAILED tests/test_policy_devpilot.py::test_cpp190_founding_chain_now_chain_safe
FAILED tests/test_policy_devpilot.py::test_cpp189_sed_range_pipe_cat_numbering_now_chain_safe
FAILED tests/test_policy_devpilot.py::test_cpp190_sed_print_chain_full_handler_allows
3 failed, 43 passed, 219 deselected in 0.56s
```

Note the captured stderr line — `[policy:deny] … [bash-grep] (non-terminal)`
— reproduces the EXACT misattributed log shape from the cpp#190 issue body,
on this repo's own test harness, before the fix. The other 43 tests in this
filtered set (the `_is_safe_sed_print_only` unit tests importable only after
the fix — collected as an `ImportError` on `tests/test_tier1.py` when
attempted, itself additional red-before evidence for that module — plus
`cpp189_ls_grep`, `cpp189_gh_help`, `mika2471`, and every negative test) were
**already green on pristine `main`**; they are included as same-issue
regression guards, not claimed as new red-before fixes. Only the three above,
plus the new `_is_safe_sed_print_only` unit tests in `tests/test_tier1.py`
(which cannot even be collected pre-fix — `ImportError: cannot import name
'_is_safe_sed_print_only'`), are genuinely red-before.

After restoring the fix (`git stash pop`), full targeted run:

```
$ uv run pytest -q tests/test_tier1.py tests/test_policy_devpilot.py
........................................................................ [ 10%]
........................................................................ [ 20%]
........................................................................ [ 30%]
........................................................................ [ 40%]
........................................................................ [ 51%]
........................................................................ [ 61%]
........................................................................ [ 71%]
........................................................................ [ 81%]
........................................................................ [ 91%]
.........................................................                [100%]
705 passed in 1.23s
```

## Full suite / lint / type / gate (verbatim, post-fix)

```
$ uv run pytest -q
[... 1152 passed in 13.87s]
1152 passed in 13.87s
```

(1124 passed on `main` before this branch's test additions; +28 new tests, 0
failed, 0 regressed.)

```
$ uv run ruff check .
All checks passed!
```

```
$ uv run mypy src
Success: no issues found in 23 source files
```

```
$ ./scripts/verify-pipeline.sh
[... bucket check: docs/plans/ present (this file) + source changes present]
Verification PASSED
```

## Existing containment/redirection/lethality guards — unchanged (confirmed)

All named regression classes pass UNCHANGED as part of the full 1152-test
green run above; none of their assertions were modified:

- cpp#38 / cpp#42 (`_destination_veto_reason` — worktree containment,
  control-plane denylist)
- cpp#128 (`_denial_is_terminal` — denial lethality narrowing)
- cpp#130 / cpp#154 (`is_tier3_dangerous_for_lethality`, contained-redirect
  lethality)
- cpp#143 (`/tmp` mkdir scratch sanction)
- cpp#150 (`bash-mkdir` regex backtracking)
- cpp#155 (`_segment_write_kind` bare-redirect classification)
- cpp#157 (quoted-lethality tier3 guard)
- cpp#158 (shared quote-span helper)
- cpp#166 (`bash-git-show-redirect` ref-charset widening)
- cpp#176 (absolute/worktree-containment redirect)
- cpp#27 (sed/awk excluded from `SAFE_SHELL_COMMANDS` — `test_tier1_rejects_all_sed_forms`
  and `test_is_safe_shell_command_still_rejects_sed_n_print` (new, added by this
  branch specifically to pin that `_is_safe_sed_print_only` staying OUT of
  `SAFE_SHELL_COMMANDS` is a deliberate, tested invariant) both pass)

No existing test's expected value (pass/fail direction) was changed anywhere
in this diff — only new test functions and the two new production symbols
(`_SED_ADDR`, `_SAFE_SED_PRINT_RE`, `_is_safe_sed_print_only`) plus one new
disjunct in `_is_safe_sub_command` were added.

## Resolves the cpp#189 class

cpp#189 named four denied read-only pipe forms. Status after this fix:

1. `sed -n '<range>p' <file> | cat -n` — **fixed** by `_is_safe_sed_print_only`.
2. `cd <worktree> && …` (truncated in the log) — the `cd` segment itself was
   already tier1-safe (`bash-cd` YAML rule / `cd` in `SAFE_SHELL_COMMANDS`);
   whatever the truncated remainder was, it is out of scope here without the
   full log line, but `cd && sed -n '<range>p' …` now passes as a chain.
3. `ls DIR | grep PATTERN | tail -N` — **already tier1-safe** on pristine
   `main` (`ls`/`grep`/`tail` are all unconditional `SAFE_SHELL_COMMANDS`
   members); not a fix, a pre-existing pass, pinned as a regression guard.
4. `gh … --help | grep …` — **already tier1-safe** on pristine `main`
   (`gh pr create` is on `SAFE_GH_SUBCOMMANDS`; `2>&1` is fd-duplication, not
   a write redirect); not a fix, a pre-existing pass, pinned as a regression
   guard.

Form 1 was the genuine gap; forms 3 and 4 were mis-diagnosed as denied by the
[bash-grep] misattribution mechanism described above (the log names
`bash-grep` regardless of which segment actually failed) even though they
were not actually blocked by anything sed-related — they were denied ONLY
when chained alongside form-1's `sed -n`, and pass standalone. This plan
resolves the whole cpp#189 class; final ticket disposition (close vs.
downgrade) is left to MPC per the dispatch instruction.

## mika#2471 — included, already passing (not a new fix)

The task brief's third cited case, `cd <worktree> && echo && git show
origin/main:crates/mika-common/src/home`, was measured to already be
chain-safe on pristine `main` before any change in this branch — `git show
<ref>:<path>` with no redirect is `is_safe_git_command`-safe standalone
(`show` ∈ `SAFE_GIT_SUBCOMMANDS`, no denied flags), and a bare `echo` segment
is unconditionally `SAFE_SHELL_COMMANDS`-safe regardless of its (possibly
empty, per the truncated log) argument. This is included as a regression
guard test (`test_mika2471_cd_echo_git_show_chain_already_chain_safe`), not
claimed as a red-before/green-after fix — flagged as a judgment call: the
issue's TERMINAL-halt framing implies something more than this reconstructed
command actually reproduces (possibly the real echoed message or another
detail was lost to truncation in the issue log), but nothing in the given
evidence localizes a further gap, and no plausible variant of this shape was
found to still fail after the sed-print fix (several were probed: different
echo content, `2>/dev/null` suffixes, absolute vs. relative worktree paths —
none reproduced a denial). If MPC has the untruncated original log line, it
should be replayed directly against this fix; absent that, this is the
closest faithful reconstruction from the issue text.

## Judgment calls / edges not proven safe beyond the three named read forms

- The new predicate is intentionally narrower than "every read-only `sed -n`
  usage" — e.g., `sed -n '1p;3p'` (multiple `p` commands joined by `;` inside
  the quotes) is NOT admitted (routes to relay, the safe direction); only a
  single address/range + one `p` was evidenced. Widening further is a
  separate, evidence-gated ticket (cpp#34 discipline), not folded in here.
- Double-quoted sed scripts (`sed -n "1,20p" file`) are NOT admitted — every
  founding case used single quotes, and the existing `s///` sibling helper
  is single-quote-only too; kept consistent rather than widened on a hunch.
- No env bypass was used or needed anywhere in this fix; `MIKA_PILOT_CONTAINED`
  is untouched, and the new predicate is unconditional (works with or
  without containment) because print-only `sed -n` carries no write/exec
  surface to bound.
- Nothing beyond the three named read forms (git show read, sed -n print,
  grep) was widened — no other tier1/policy allow-list entry, chain-safety
  branch, or destination-veto rule was touched.

## Requirements checklist

- [x] Full suite green (`uv run pytest`, `uv sync --extra dev`)
- [x] `uv run ruff check` clean
- [x] `uv run mypy src` clean
- [x] `./scripts/verify-pipeline.sh` passes (this plan doc pairs the source fix)
- [x] Positive tests: red-before (pristine `main`) / green-after (this branch), verbatim
- [x] Negative tests: denied both before and after, verbatim
- [x] Existing containment/redirection/lethality guards unchanged (full-suite pass + no modified assertions)
- [x] Resolves cpp#189 class (documented above)
- [x] No env bypass, no widening beyond the three named read forms
