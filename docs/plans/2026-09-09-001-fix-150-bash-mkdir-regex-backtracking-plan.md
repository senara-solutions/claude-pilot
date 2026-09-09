---
title: bash-mkdir Regex Backtracking Fix - Plan
type: fix
date: 2026-09-09
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# bash-mkdir Regex Backtracking Fix - Plan

## Goal Capsule

Objective: the `bash-mkdir` rule in `policies/permissions.yaml` claims — in its own `reason`
field — to allow only RELATIVE `mkdir` targets. Make the regex actually enforce that, for any
combination of flags, without regressing the one thing that (by accident) depended on it not
enforcing that: cpp#143's sanctioned `/tmp` scratch-directory exception.

Means: replace the vulnerable negative-lookahead-after-optional-group construction with the same
whole-remainder-scanning lookaheads `bash-cp-mv` already uses one rule below it in the same file
(proven immune to the backtracking class by construction, not by luck), and restore the `/tmp`
scratch allowance explicitly, as its own fully-anchored, positively-matched rule — never again as
a side effect of a regex defect.

Authority hierarchy: `senara-solutions/claude-pilot#150` body > this plan > implementer judgment.
The issue is pre-decided toward "fix the rule, keep it defense-in-depth, not a live-hole close" —
confirmed independently below (Verification, Divergence 1).

Stop conditions: none that halt. The one non-negotiable constraint is R5 below (no regression on
cpp#143); see Divergences for why the obvious literal reading of the issue's own suggested test
list conflicts with it, and how that was resolved.

Execution profile: single-repo, one YAML file (`policies/permissions.yaml`) plus its test file
(`tests/test_policy_devpilot.py`). No code (`.py`) change — the runtime destination veto
(`_destination_veto_reason`) already classifies writes structurally by leading command word, not
by policy `rule_id`, so it needs no change for this fix to be safe (see Divergence 2 of the
cpp#143 plan, which named this exact defect and deferred it here).

Tail ownership: implementer opens the PR through green CI (gated, no merge); MPC reviews/merges.

## Product Contract

### Summary

`bash-mkdir` now matches `allow` **only** for a genuinely relative `mkdir` target, for any
combination of flags. `mkdir -p /etc/evil` — the issue's own reproduction — no longer matches
the rule as `allow`, regardless of how the optional flag-group backtracks. A second, new,
narrowly-scoped rule (`bash-mkdir-tmp-scratch`) explicitly restores the one absolute shape that
must still be allowed: a literal `/tmp/...` operand (cpp#143), so that fixing this defect does not
silently regress that one.

### Problem Frame

`policies/permissions.yaml` (`bash-mkdir`, then at line 371):

```
pattern: '^mkdir(\s+-\S+)*\s+(?!/)(?!~)(?!\$)(?!.*\.\.)\S'
```

Confirmed empirically (Python 3.14, stdlib `re`):

```python
>>> re.compile(r'^mkdir(\s+-\S+)*\s+(?!/)(?!~)(?!\$)(?!.*\.\.)\S').match("mkdir -p /etc/evil")
<re.Match object; span=(0, 7), match='mkdir -'>
```

**Root cause.** The optional flag-group `(\s+-\S+)*` is greedy but backtracks. On
`"mkdir -p /etc/evil"`, the engine first tries consuming `-p` as one flag repetition, which
forces the mandatory `\s+` to land on the space before `/etc/evil` — the lookaheads correctly see
`/` there and reject. But `re` then backtracks the flag-group down to **zero** repetitions, which
lets the SAME mandatory `\s+` re-anchor on the space right after `mkdir` instead — the lookaheads
now test the flag token `-p` (never `/`, `~`, `$`, or `..`-bearing) instead of the real target,
pass trivially, and `\S` consumes the `-`. The pattern has no `$` anchor requiring the rest of the
line to also qualify, so this partial, semantically-empty match is enough for `re.search` (which
is what `policy.evaluate()` actually calls) to report a hit. The result: `evaluate()` returns
`decision=allow, rule_id=bash-mkdir` for `mkdir` with **any** combination of short flags followed
by **any** absolute path, `~`-path, or `$`-path — not just the relative shapes the rule's own
`reason` field claims to allow.

**Why this is not a live security hole.** `_destination_veto_reason` (permissions.py) determines
every actual `mkdir` write destination at runtime by shlex-tokenizing the segment and checking
worktree containment + the control-plane denylist — classified structurally by the segment's
leading command word (`_segment_write_kind`), never by which policy `rule_id` matched
(`permissions.py`'s own documented "shadow-rule" discipline, cpp#42 adversarial review). A
wrongly-`allow`ed `mkdir -p /etc/evil` still reaches that chokepoint and is still vetoed,
unconditionally terminal. This defect was already found and named during cpp#143's implementation
(that plan's Divergence 2) and deliberately deferred as "a follow-up candidate, not fixed in this
PR" — this issue and this plan are that follow-up.

**Why it is still worth fixing.** The YAML rule is the auditable statement of intent for what
`mkdir` shapes this rule believes it is allowing; the fact that it doesn't match its own `reason`
is fragile — a future refactor of the runtime destination-veto chokepoint (the actual safety
boundary) could silently expose the gap this rule's text claims doesn't exist.

### Key Decisions

- **Fix mirrors `bash-cp-mv`'s own construction, one rule below it in the same file.** That rule
  already scans the *whole remainder* for `\s/`, `\s~`, `\s$` (`(?!.*\s/)(?!.*\s~)(?!.*\s\$)`), not
  just the position immediately after the mandatory `\s+`. Adding the same three lookaheads to
  `bash-mkdir` closes the exact backtracking path above: no matter how many reps the flag-group
  backtracks to, the *remaining string from that point* still contains the forbidden
  whitespace-then-metachar sequence somewhere, and the lookahead — which scans forward, not just
  at the anchor position — catches it. Verified empirically for every flag-count and target
  combination tried (see Verification).
- **`/tmp/...` gets its OWN rule, not a widened `bash-mkdir`.** Merging the exception into
  `bash-mkdir` via alternation was tried mentally and rejected: it would require the SAME
  negative-lookahead style to somehow special-case one absolute prefix while still rejecting
  every other one, reintroducing exactly the kind of "prove a negative through backtracking"
  construction this ticket is fixing. A dedicated, fully end-anchored (`^...$`), POSITIVELY
  matched rule — mirroring `bash-cat-heredoc-tmp`'s own established precedent for a `/tmp`
  carve-out getting its own rule — cannot suffer the same failure mode: it does not try to prove
  "not absolute" via a lookahead an optional group can dodge; it requires every operand to
  *literally* start with `/tmp/`, checked after full-string anchoring. Also matches the repo's own
  stated design philosophy for the control-plane denylist ("each entry carries its own blast-radius
  rationale, broadening is evidence-gated") — cpp#143's sanction is its own auditable rule, not a
  clause buried in `bash-mkdir`'s.
- **No `.py` change.** `_segment_write_kind`/`_extract_mkdir_destinations`/
  `_destination_veto_reason`/`_denial_is_terminal` are all keyed off the segment's leading command
  word or off `decision == "allow"`, never off a specific `rule_id` string, for exactly this
  reason (cpp#42's shadow-rule discipline). Adding a second `Bash` rule whose leading word is also
  `mkdir` needs no code change to be honored identically to `bash-mkdir` at the destination-veto
  layer.

**Divergences from the issue's literal ask, applied during implementation:**

1. **The issue's suggested negative-test list includes `/tmp/x`; this plan does NOT deny it.**
   Tracing the full call path (`permissions.py:1362` — `_destination_veto_reason` is invoked
   unconditionally on every `Bash` policy-allow, and independently inside `_denial_is_terminal` on
   every default-deny) surfaced that `mkdir -p /tmp/<scratch>` currently reaches `decision=allow`
   **only** as a side effect of this SAME backtracking bug — verified by tracing the ORIGINAL
   pattern against `"mkdir -p /tmp/x"`: it matches via the identical zero-flag-backtrack path as
   `/etc/evil` does. `tests/test_permissions.py::test_mkdir_tmp_scratch_is_permitted_but_other_outside_targets_stay_lethal`
   asserts `PermissionResultAllow` (the directory is actually CREATED, not merely non-terminally
   refused) for exactly this shape — that is cpp#143, shipped and green on `main`. A literal fix
   that made `/tmp/x` deny-as-bash-mkdir without restoring the allowance some other way would pass
   the full test suite only by also breaking that test — which it does (confirmed: reverting to a
   naive scan-lookahead-only `bash-mkdir` fix with no companion rule fails
   `test_mkdir_tmp_scratch_is_permitted_but_other_outside_targets_stay_lethal`'s first assertion).
   Denying `/tmp/x` outright would be a functional regression of shipped, evidenced behavior
   (session `0160cce6`, cpp#143's own founding incident), not a hardening. Resolution: `/tmp/x`
   still evaluates to `decision=allow` overall — via the new dedicated `bash-mkdir-tmp-scratch`
   rule, never via `bash-mkdir` itself. The tests below assert precisely that distinction
   (`rule_id == "bash-mkdir-tmp-scratch"`, not `"bash-mkdir"`) rather than a blanket deny, which is
   both truer to the dispatch's own request ("do NOT match `bash-mkdir` as allow") and safe.
2. **`_destination_veto_reason` still independently validates every `/tmp` operand
   (`_is_sanctioned_tmp_scratch`) at runtime**, so `bash-mkdir-tmp-scratch`'s own YAML-level
   `..`-traversal guard is defense-in-depth, not the only line of defense — consistent with this
   ticket's own framing that the YAML layer is a pre-exec shape filter, never the sole safety
   boundary.

### Requirements

- R1. `bash-mkdir` does not match `decision=allow` for `mkdir` with an absolute (`/...`), `~`-,
  or `$`-prefixed target, nor for a target containing `..` anywhere, for ANY combination of
  leading short flags (zero, one, or more).
- R2. `bash-mkdir` still matches `decision=allow` for a genuinely relative target, with or without
  flags (including a flag that itself takes a value, e.g. `-m 755`).
- R3. The issue's own reproduction, `mkdir -p /etc/evil`, does not match `bash-mkdir` as allow.
- R4. `~/x` and `../x` targets do not match `bash-mkdir` as allow (issue's explicit list, minus the
  `/tmp/x` divergence in R5).
- R5. **Non-negotiable.** `mkdir -p /tmp/<token>` (any shape `_extract_mkdir_destinations` already
  returns as a literal `/tmp/...` string, no `..`) still evaluates to `decision=allow` end-to-end
  through the production handler (cpp#143 non-regression) — via a rule other than `bash-mkdir`.
  A `/tmp`-prefixed operand containing `..`, or a target list mixing a `/tmp` operand with a
  non-`/tmp` one, does not qualify for this allowance.
- R6. No `.py` source file changes; the fix is confined to `policies/permissions.yaml` and its
  test coverage in `tests/test_policy_devpilot.py`.
- R7. Full existing test suite stays green — in particular every `mkdir`-shaped assertion in
  `tests/test_permissions.py` and `tests/test_policy_devpilot.py` (`_denial_is_terminal` truth
  table, cpp#38/#42 destination-validator suite, cpp#143's own scratch-exception suite, cpp#151
  lethality-logging suite).

### Scope Boundaries

In scope: `policies/permissions.yaml` (`bash-mkdir` pattern + one new `bash-mkdir-tmp-scratch`
rule), `tests/test_policy_devpilot.py`.

Out of scope, explicitly: `permissions.py`/`policy.py` (no code change needed or made); widening
or tightening `bash-cp-mv` (it has its own, structurally different, target-flag `-t` handling and
is not reported as backtracking-vulnerable — its lookaheads already scan the whole remainder, so
it was never exposed to this class); the runtime `_destination_veto_reason` chokepoint itself
(unchanged, already correct — this ticket hardens the pre-exec shape filter that sits in front of
it, per the issue's own framing).

## Implementation

### Step 1 — fix `bash-mkdir` (`policies/permissions.yaml`)

```yaml
- id: bash-mkdir
  tool: Bash
  pattern: '^mkdir(\s+-\S+)*\s+(?!/)(?!~)(?!\$)(?!.*\s/)(?!.*\s~)(?!.*\s\$)(?!.*\.\.)\S'
  decision: allow
  reason: "mkdir (relative, no .. / absolute / ~ / $ expansion) -- dev-pilot worktree scaffolding (mika#1116: mkdir -p crates/mika-os/src)"
```

Only the pattern changes (three lookaheads added, mirroring `bash-cp-mv`'s own three); `reason`
is unchanged since it already stated the intended behavior correctly — only the regex was wrong.

### Step 2 — add `bash-mkdir-tmp-scratch` (new rule, placed immediately after `bash-mkdir`)

```yaml
- id: bash-mkdir-tmp-scratch
  tool: Bash
  pattern: '^mkdir(\s+-\S+)*(?!.*\.\.)(\s+/tmp/[\w./-]+)+\s*$'
  decision: allow
  reason: "mkdir under /tmp only, every operand literally /tmp/... and ..-free -- cpp#143 sanctioned scratch-directory exception, restored explicitly by cpp#150 (previously an accident of the same regex bug that rule fixed)"
```

Fully anchored (`^...$`): every non-flag token after the flags must itself be a `/tmp/...`
literal, or the whole pattern fails to match — a mixed list (`/tmp/x /etc/evil`) or one still
carrying `..` is rejected by construction, not by a lookahead that an optional group could
backtrack around.

### Step 3 — doctrine comments in the YAML

Inline comments above both rules record: the backtracking mechanism, why the fix is
backtracking-immune (positive full-string matching vs. negative lookahead after an optional
group), and the cpp#143 interaction (Divergence 1) so a future reader does not "simplify" the two
rules back into one and reintroduce the regression.

### Step 4 — tests (`tests/test_policy_devpilot.py`, cpp#150 section)

- `test_cpp150_bash_mkdir_absolute_target_no_longer_allowed` — the issue's own repro
  (`mkdir -p /etc/evil`) plus flag-count variants, asserting NOT `(allow, bash-mkdir)`.
- `test_cpp150_bash_mkdir_home_and_var_expansion_no_longer_allowed` — `~/x`, `$FOO/x`, `$HOME/x`.
- `test_cpp150_bash_mkdir_traversal_no_longer_allowed` — `../x` and an embedded `a/../../etc`.
- `test_cpp150_bash_mkdir_tmp_scratch_matches_dedicated_rule_not_bash_mkdir` — R5, asserting
  `rule_id == "bash-mkdir-tmp-scratch"` specifically (not merely `decision == "allow"`).
- `test_cpp150_bash_mkdir_tmp_traversal_still_denied` — `/tmp/../etc/evil` stays denied.
- `test_cpp150_bash_mkdir_mixed_tmp_and_absolute_denied` — `/tmp/x /etc/evil` stays denied.
- `test_cpp150_bash_mkdir_relative_target_still_allowed` — POSITIVE control, R2, including the
  mika#1116 founding example and a value-taking flag (`-m 755`).
- `test_cpp150_end_to_end_handler_still_denies_etc_evil` — full handler, `PermissionResultDeny`,
  `interrupt is True` (defense-in-depth confirmation, not a behavior change).
- `test_cpp150_end_to_end_handler_still_allows_tmp_scratch` — full handler, R5 non-regression,
  `PermissionResultAllow`.

## Verification

- Regex confirmed directly against Python's `re` (this repo's engine) for every case above before
  editing the YAML — see the assistant's tool transcript; not re-derived from memory.
- `uv run pytest tests/test_policy_devpilot.py -k cpp150 -v`: 9 passed (new tests only).
- `uv run pytest` (full suite): 1050 passed, 0 failed — includes every pre-existing `mkdir`-shaped
  assertion named in R7.
- `uv run ruff check .`: All checks passed.
- `uv run mypy src`: Success: no issues found in 23 source files.
- `./scripts/verify-pipeline.sh`: passes (this plan doc pairs with the YAML + test source change).
- **Anti-vacuity**: reverting `policies/permissions.yaml` to its pre-fix `bash-mkdir` pattern (and
  removing `bash-mkdir-tmp-scratch`) while keeping the new tests makes
  `test_cpp150_bash_mkdir_absolute_target_no_longer_allowed` (and its sibling negative tests) FAIL
  — `evaluate()` returns `decision="allow", rule_id="bash-mkdir"` for `mkdir -p /etc/evil` again,
  confirming the new tests actually exercise the defect and are not vacuously true.
