---
title: Sanctioned /tmp Scratch Directory for mkdir - Plan
type: fix
date: 2026-09-03
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# Sanctioned /tmp Scratch Directory for mkdir - Plan

## Goal Capsule

Objective: a pilot that needs an ephemeral working directory outside its worktree (test
fixtures, run logs — content a worktree `git status` would see and that has no business being
committed) can `mkdir -p /tmp/<scratch>` and have it actually succeed, instead of the session
dying on it.

Means: give `mkdir` the same narrow, lexical `/tmp` exception `permissions.py` already gives a
`cat`-heredoc file write, at the one chokepoint (`_destination_veto_reason`) both the explicit-
allow and default-deny routes fall through to — not a blanket allow, not a change to the
worktree-containment or control-plane checks.

Authority hierarchy: `senara-solutions/claude-pilot#143` body (AC1–AC4) > this plan >
implementer judgment. AC1 is pre-decided by the issue body toward the "sanctioned ephemeral
scratch zone" branch (the alternative — making `/tmp` wholly off-limits, including the existing
heredoc — is explicitly named and explicitly not chosen here, since the issue's own dispatch
comment commits to "Fix + plan-doc").

Stop conditions: none that halt. AC3 (negative control) is treated as the one non-negotiable
constraint; any implementation choice that could not keep AC3 green was discarded during design
(see Key Decisions) rather than shipped and flagged.

Execution profile: single-repo, one source file (`permissions.py`) plus its test file. No YAML
policy change, no wire-format change, no new `decision: allow`.

Tail ownership: implementer opens the PR through green CI; `samidarko` merges.

## Product Contract

### Summary

`mkdir -p /tmp/<scratch>` from a pilot session is now **permitted and executes** — not merely
refused-non-terminally — under the same narrowness the codebase already applies to writing a
*file* under `/tmp`. Every other out-of-worktree `mkdir` target (a system path, a worktree
symlink that resolves into `/tmp`, a `..`-laden `/tmp`-looking path) stays refused **and
terminal**, exactly as before.

### Problem Frame

Session `0160cce6-8b1f-4b7f-aa20-ad12178e36b8` (dispatch of `mika#2116`, 2026-09-02) ran 2h52,
72 tool calls — the highest volume measured on this substrate — and died here (verbatim,
`/var/log/claude-pilot/0160cce6-….stderr`):

```
[tool:request] Bash: mkdir -p /tmp/rt005-empty-nobatch /tmp/rt005-empty-runs/runs
[policy:deny]  Bash: mkdir -p /tmp/rt005-empty-nobatch /tmp/rt005-empty-runs/runs [bash-mkdir]
```

This reproduces exactly (byte-for-byte on the stderr line) against this repo's HEAD without the
fix — see Verification.

**Root cause, traced to source.** `permissions.py:335` in `policies/permissions.yaml`
(`bash-mkdir`) is meant to allow only RELATIVE `mkdir` targets (`(?!/)(?!~)(?!\$)(?!.*\.\.)`).
It has an unrelated pre-existing regex-backtracking defect — confirmed empirically (see
Divergences) — that lets it match ANY `mkdir` invocation carrying a short flag (`mkdir -p
<anything>`, including an absolute path), regardless of the target. That defect is why the
command reaches `evaluate()` with `decision == "allow", rule_id == "bash-mkdir"` at all. From
there, `_bash_allow_is_chain_safe` passes it through (a single un-chained segment), and the
handler calls `_destination_veto_reason(command, cwd)` (`permissions.py:1006-1018` pre-fix),
which — correctly, by design (cpp#38) — sees `/tmp/rt005-empty-nobatch` resolve outside the
worktree `cwd` and vetoes it. That call site is `permissions.py`'s **one deliberate exception**
to cpp#128's denial-lethality split (non-lethal-by-default): a containment escape is
unconditionally `interrupt=True`. The session dies.

**The inconsistency the issue names.** The SAME file, `permissions.py:322-348`
(`_is_sanctioned_pure_heredoc` / YAML `bash-cat-heredoc-tmp`), already lets a pilot write a
*file* to `/tmp` with **no containment veto at all** — not because `/tmp` is exempted from
containment generically, but because `cat` is not a write-kind `_segment_write_kind`
classifies, so a heredoc's `/tmp` destination never reaches `_destination_veto_reason` in the
first place. `mkdir` IS classified (`_segment_write_kind` returns `"bash-mkdir"`), so the exact
same boundary — a scratch path under `/tmp`, outside the worktree by construction — was refused
and session-fatal for a directory while being routine for a file. A pilot cannot deduce this: it
sees its heredoc succeed, infers `/tmp` is open, and dies on the first `mkdir`.

The lost work (11 fixtures, a report, a test — `009-excl-failed`, `010-excl-empty`,
`011-excl-unattr`, needing a working directory for empty/missing-directory test cases) was only
recovered by post-mortem rescue (`mika#2138`, `wip-rescue`), which the `/mika` pipeline never
saw (`rescue-pipeline-verified: no` — no `/ce:review`, no doc audit, no `/ce:compound`).

### Key Decisions

- **AC1 resolved toward the affordance branch, not the restriction branch.** The issue names
  both as defensible and leaves the call to grooming; the dispatch comment on the issue
  ("Fix + plan-doc") already commits to shipping a fix, so tightening `/tmp` into a second
  terminal case is not this PR's direction. If grooming disagrees, that is a policy-tightening
  PR against the now-consistent baseline, not a revert of this one.
- **The exception is LEXICAL, not filesystem-resolved — mirroring `_is_sanctioned_pure_heredoc`
  exactly, not just its target.** This was NOT the first design tried; see Divergences below for
  why a resolve-based version was rejected mid-implementation.
- **Scoped to `mkdir` only.** `cp`/`mv`/`git show >` into `/tmp` are untouched and still vetoed.
  The issue's incoherence is specifically file-vs-directory under `/tmp`; widening the exception
  to every write-kind is a different, unasked-for change.
- **No YAML change, no fix to the `bash-mkdir` backtracking defect.** See Divergences — the
  defect is real but not on the path this fix needs to close, and touching it is out of scope
  per the issue body ("ce ticket ne demande pas de l'affaiblir" — restated here as: this ticket
  does not ask for a rule to be widened OR tightened, only for the `/tmp` incoherence to be
  resolved one way).

**Divergences from the obvious first approach, applied during implementation:**

1. **A resolve-based (`Path.resolve()`, symlink-following) version of the exception was tried
   first and reverted.** It checked whether the destination's CANONICAL path landed under a
   resolved `/tmp` root — matching how `is_within_project`/`_is_control_plane_path` already
   resolve their targets. Running the full existing test suite against it broke four pre-existing
   cpp#38 symlink-escape tests in `tests/test_policy_devpilot.py`
   (`test_dest_validator_ac38_4_mkdir_symlink_escape_denied` and three siblings): those tests
   build a worktree symlink (`esc -> ../OUTSIDE`) using pytest's `tmp_path` fixture, which is
   itself nested under `/tmp` — so the symlink's resolved target landed under `/tmp` and the
   resolve-based exception exempted a genuine cpp#38 containment escape. The blast radius is
   arguably identical either way (a pilot could always `mkdir /tmp/x` directly), but the MECHANISM
   was wrong: it made the exception's scope depend on where a path resolves rather than on what
   the pilot's own command says, which is precisely the “oracle” shape cpp#128's own destination-
   veto doctrine warns against widening. Switching to a pure lexical match on the raw operand —
   the same mechanism the heredoc exception already uses — closes this: a symlink or relative
   route into `/tmp` never spells `/tmp/...` in the command text, so it does not qualify and
   stays caught by the ordinary containment veto. This is recorded here because it is exactly the
   kind of design-narrowing an implementer is expected to apply and flag, not silently redo.
2. **The `bash-mkdir` YAML regex-backtracking defect (confirmed, not fixed).** Empirically:
   `re.compile(r'^mkdir(\s+-\S+)*\s+(?!/)(?!~)(?!\$)(?!.*\.\.)\S').match("mkdir -p /etc/evil")`
   matches (`span=(0, 7), match='mkdir -'`) — the greedy flag-group backtracks to zero
   repetitions, the lookaheads then apply only to the flag token `-p` (which is not `/`, `~`,
   `$`, or `..`-bearing) rather than to the real absolute target, and the pattern has no `$`
   anchor to require the rest of the string to also qualify. This means `bash-mkdir` currently
   matches (and `evaluate()` returns `decision=allow` for) an absolute `mkdir -p <anything>`,
   including `/etc/evil` — not just `/tmp`. **This is not a live security hole**: the runtime,
   shlex-based `_destination_veto_reason` independently determines every actual write
   destination regardless of which YAML rule matched (`_segment_write_kind` classifies by the
   segment's leading command word, never by `rule_id` — precisely the cpp#42 "shadow-rule"
   discipline already documented at `permissions.py:599-607`), so a wrongly-"allowed" `mkdir
   /etc/evil` is still caught and still terminal by the destination veto, exactly as
   `test_denial_is_terminal_predicate`/`test_dest_validator_*` pin. Flagged here as a follow-up
   candidate, not fixed in this PR: fixing it would not change any observable behavior this issue
   is about (an absolute-path denial routes through the SAME `_destination_veto_reason` chokepoint
   either via the explicit-allow branch or via `_denial_is_terminal`'s internal call on the
   default-deny branch — both terminal, both today and after a hypothetical regex fix), so
   bundling it here would be scope creep against a ticket the issue explicitly bounds.

### Requirements

- R1. `mkdir -p /tmp/<token>` (and any operand shape `_extract_mkdir_destinations` returns as a
  literal `/tmp/...` string, no `..`) from a pilot session is **allowed and executes** — not
  merely refused-non-terminally.
- R2. A `mkdir` targeting any location outside the worktree that is NOT the sanctioned `/tmp`
  operand — a system path (`/etc/...`), or a worktree symlink/relative path that only *resolves*
  into `/tmp` without the command text itself saying `/tmp/...` — stays refused and **terminal**
  (AC3, the mandatory negative control; this is the one requirement treated as non-negotiable).
- R3. The exception is scoped to `mkdir` (`_segment_write_kind == "bash-mkdir"`) only. `cp`/`mv`/
  `git show >` destinations under `/tmp` are unaffected.
- R4. A `/tmp`-prefixed operand containing `..` anywhere is rejected by the exception's own
  pattern (mirrors `_is_sanctioned_pure_heredoc`'s `(?!.*\.\.)`), independent of any resolve step.
- R5. Existing cpp#38/cpp#42 destination-validation tests keep their original security assertion.
  Where a test's OWN fixture choice (a `tmp_path`-derived "outside" target, itself under `/tmp`)
  becomes ambiguous post-fix, the test is updated to use an unambiguous target — never its
  assertion weakened.
- R6. No `permissions.yaml` rule is added, removed, widened, or tightened.

### Scope Boundaries

In scope: `permissions.py` (`_destination_veto_reason` and one new helper +
regex constant), `tests/test_permissions.py`.

Out of scope, explicitly: `policies/permissions.yaml` (including the `bash-mkdir` backtracking
defect named above); `per_spawn.py` (the mika#1708 per-spawn evaluator is a generic engine
shipping an empty default policy in this repo — the production incident traced to the classic
tier1/tier2 path this fix touches, confirmed by the `[bash-mkdir]` rule-id in the reproduced
stderr line, which is this repo's YAML rule-id convention, not the per-spawn evaluator's);
extending the exception to `cp`/`mv`; AC2's "startup-context" framing (advertising the `/tmp`
affordance to the pilot proactively) — the issue leaves this to whichever system-prompt surface
already documents tier1/tier2 behavior for pilots and is not this module's concern.

## Implementation

### Step 1 — sanctioned-scratch helper (`src/claude_pilot/permissions.py`)

Immediately above `_destination_veto_reason`, add a doctrine block mirroring the existing
`_is_sanctioned_pure_heredoc` commentary, then:

```python
_TMP_SCRATCH_MKDIR_RE = re.compile(r"^/tmp/(?!.*\.\.)[\w./-]+$")


def _is_sanctioned_tmp_scratch(dest: str) -> bool:
    return bool(dest) and _TMP_SCRATCH_MKDIR_RE.match(dest) is not None
```

Purely lexical on the raw, un-resolved operand string `_extract_mkdir_destinations` already
returns — no `Path`, no filesystem access, no `cwd` parameter. This is the corrected design
after Divergence 1 above.

### Step 2 — wire into `_destination_veto_reason`

Inside the per-destination loop, before the containment check:

```python
for dest in dests:
    if kind == "bash-mkdir" and _is_sanctioned_tmp_scratch(dest):
        continue
    if not is_within_project(dest, cwd):
        ...
```

Both call sites that reach `_destination_veto_reason` — the explicit-allow branch's own
unconditional-terminal call, and `_denial_is_terminal`'s internal call on the default-deny/
chain-veto routes — benefit automatically; this is the single chokepoint both already share.

### Step 3 — doctrine comments

Update the cpp#128 doctrine block's `_destination_veto_reason` bullet (the one that says
`mkdir -p /outside/x` matches `bash-mkdir` and halts) to note the `/tmp` carve-out no longer
makes that true for `/tmp` specifically.

### Step 4 — tests (`tests/test_permissions.py`)

- New `test_mkdir_tmp_scratch_is_permitted_but_other_outside_targets_stay_lethal`: positive (the
  exact `0160cce6` shape → `PermissionResultAllow`), AC3 negative control (a system path stays
  denied + terminal), a `..`-in-operand negative control, a worktree-symlink-into-`/tmp` negative
  control (pins Divergence 1's fix), and a unit-level truth table on the helper itself.
- Extend `test_denial_is_terminal_predicate`'s R3 block with the `/tmp` exception (mkdir → non-
  terminal) alongside the untouched `cp`/`mv` cases (still terminal).
- Fix `test_containment_escape_is_lethal_on_the_default_deny_route`: its "outside" target was
  `tmp_path / "outside"`, itself under `/tmp` post-fix. Replaced with a fixed literal
  (`/definitely/outside/x`, already this file's convention elsewhere) so the test still tests a
  genuine escape rather than accidentally landing inside the new exception (R5).

## Verification

- Anti-vacuity, confirmed by reverting the source change and re-running: the two touched tests
  fail, and the failure reproduces the issue's cited stderr line byte-for-byte:
  `[policy:deny]  Bash: mkdir -p /tmp/rt005-empty-nobatch /tmp/rt005-empty-runs/runs [bash-mkdir]`.
- `uv run pytest` (full suite, post-fix): 961 passed, 0 failed.
- `uv run pytest tests/test_permissions.py -v`: 23 passed (see PR body for verbatim output).
- `uv run ruff check`: All checks passed.
- `uv run mypy src`: Success: no issues found in 22 source files.
- Full-suite run also serves as the R5 check: the four `test_policy_devpilot.py` cpp#38 tests
  that broke under the (reverted) resolve-based design pass unmodified against the shipped
  lexical design — they were never edited, because the corrected design doesn't need them to be.
