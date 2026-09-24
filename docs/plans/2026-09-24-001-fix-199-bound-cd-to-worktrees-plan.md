---
title: Bound bash-cd to Worktree Paths - Plan
type: fix
date: 2026-09-24
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue
execution: code
---

# Bound bash-cd to Worktree Paths - Plan

## Goal Capsule

Objective: `senara-solutions/claude-pilot#199` — a benign `cd` into a pilot's own dispatch
worktree (`cd /data/workspace/mika-platform/.claude/worktrees/<branch>/mika`) was default-DENIED
in production (mika#2458-iter2, dispatch task `49269200-6e2a-446f-a7e6-c07a09677434`), 26 turns,
$7.71, `Outcome: PIPELINE_INCOMPLETE`. Establish where that deny actually came from (AC3), then
bound the `bash-cd` NAMED policy rule (`policies/permissions.yaml`) so the worktree cd is a
deterministic static `allow`, and so an absolute `cd` NOT under a managed worktree is now a
deterministic `deny` — the rule's pre-existing over-broad hole (`cd /`, `cd /etc`, `cd /root` all
matched `allow`), and the direction this ticket's own AC2 exists to close.

Means: two mutually exclusive shapes in the one `bash-cd` regex — relative path, or absolute path
containing a literal `/.claude/worktrees/` segment — plus a whole-remainder `..`-exclusion
lookahead shared by both. No new rule, no gate change, no env bypass.

Authority hierarchy: operator directive (2026-09-24, this dispatch) > `cpp#199` body > this plan >
implementer judgment. Operator bearing, verbatim: "fix the NAMED allow-rule (never the gate,
never an env bypass); revise BOTH directions (over-block AND under-block); non-reopening of
cpp#176/#178."

Stop conditions: none that halt implementation. One hard scope boundary carried through
Verification below: `tier1.py`'s `SAFE_SHELL_COMMANDS` treats bare `cd <path>` as unconditionally
safe regardless of destination (pre-existing, deliberate, documented design) — that is the
pre-classifier GATE, explicitly off-limits per the operator's bearing, and is NOT touched by this
fix. AC1/AC2 are therefore specified and verified at the level the issue itself prescribes: direct
`policy.evaluate()` probes against the Tier-2 named rule.

Execution profile: single-repo (`claude-pilot`), one YAML file (`policies/permissions.yaml`) plus
its test file (`tests/test_policy_devpilot.py`). No other `.py` change.

Tail ownership: implementer opens the PR through green CI (gated, no merge); MPC reviews/merges.
Backporting the fixed rule into any mika-side policy overlay (see AC3) is explicitly out of scope
— a follow-up for whoever owns that overlay, not this ticket.

## Product Contract

### Summary

`bash-cd` now matches `allow` for exactly two shapes: (i) a relative `cd <path>` (no leading `/`,
`~`, or `$`), or (ii) an absolute `cd <path>` whose target contains a literal `/.claude/worktrees/`
path segment — a pilot's own managed dispatch worktree. Both shapes additionally exclude `..`
anywhere in the target. Everything else that used to match — `cd /`, `cd /etc`, `cd /root`, any
other absolute path with no worktree relation, and relative `..`-traversal — now falls through to
the file's own `default: deny`, exactly as it would have if `bash-cd` did not exist.

### AC3 — static-vs-relay/per-spawn characterization (read this first)

Established via direct source-level investigation (probe API prescribed by the ticket:
`from claude_pilot import policy as POL; POL.evaluate(pol, "Bash", {"command": c})`), confirming
and extending the operator's own prior probe (issue comment, 2026-09-24):

1. **The bundled HEAD policy already allows the exact founding command**, deterministically:

   ```
   POL.evaluate(POL.load_policy(), "Bash",
       {"command": "cd /data/workspace/mika-platform/.claude/worktrees/"
                    "bug-1910-llm-glm-5-2-silent-empty-output-au-max/mika"})
   -> PolicyDecision(decision='allow', reason='...', rule_id='bash-cd')
   ```

   This is **not** an allow-list hole in this repository's bundled `permissions.yaml` at the
   commit the pilot's dependency would resolve to (`9ab3c13`, HEAD at dispatch time). `bash-cd` was
   added by cpp#97 (`d6e4809`), well before this HEAD — it is not new or unreleased.

2. **The same probe shows the pre-existing rule is over-broad**: `cd /` → `allow`, `cd /etc` →
   `allow`, `cd /root` → `allow`, `cd /data/workspace/mika-platform` (a repo root, not a worktree)
   → `allow`, and — via the `.` character already present in the old charset — `cd ../../etc`
   (relative traversal) → `allow` too. `cd ~` was the one exception, already `deny` (charset never
   admitted `~`). This is the "over-block-in-reverse" AC2 exists to close, independent of AC1.

3. **The relay ("haiku") path is not the live mechanism.** `permissions.py:create_permission_handler`
   evaluates Tier 2 (`policy.evaluate`) whenever `MIKA_PILOT_POLICY_DISABLED` is unset (the
   default posture). `policy.evaluate` always returns a decision — `allow`, `deny`, or `escalate`
   — for every input, including the policy's own `default: deny` when no rule matches. The relay
   block is reached only when that env var is set to `1` (see the comment at
   `permissions.py:1693-1694`, `# TODO(mika#1193 Phase C): remove relay block ... only reachable
   when MIKA_PILOT_POLICY_DISABLED=1`, and the repo's own
   `docs/permissions-interactive-fallback.md`: *"the relay path is only reachable when
   `MIKA_PILOT_POLICY_DISABLED=1`"*). Absent evidence that emergency-rollback flag was set for the
   mika#2458-iter2 dispatch, non-deterministic haiku relay classification is **not** the mechanism
   that produced the observed deny.

4. **The production log signature (`[policy:deny] Bash: cd ...` with NO `[rule-id]` bracket before
   `(terminal)`) is exactly what Tier 2's policy DEFAULT looks like** — `pd.rule_id is None`, per
   `permissions.py`'s `log_policy_deny(tool_name, detail, pd.rule_id, ...)` call in the
   `pd.decision == "deny"` branch. That signature is produced when `policy.evaluate()` finds no
   matching rule for the *policy actually loaded at runtime* for that dispatch.

5. **Conclusion (bounded by what is verifiable from inside this repository):** since the bundled
   `permissions.yaml` at HEAD *does* match `bash-cd` for the exact command, the policy loaded by
   that specific production dispatch must have been a **different file** than this repo's current
   bundled default — either (a) an older/pinned `claude-pilot` release predating cpp#97
   (`bash-cd` did not exist yet), resolved via mika-platform's dependency pin at dispatch time, or
   (b) a `MIKA_PILOT_POLICY_PATH` operator overlay (this file's own header documents that override
   as "used for temporary overlays... without rebuilding cpp") that does not carry `bash-cd`.
   `_load_per_spawn_policy` / `MIKA_PERMISSION_POLICY_MODE=per_spawn` does **not** explain it: that
   mode, when it denies a Bash spawn, explicitly falls through to the classic Tier 1 + Tier 2 path
   (`permissions.py:1484`, `# Fall through to classic evaluators — they may still allow`) rather
   than terminating the deny itself — so even under `per_spawn` mode, the classic `bash-cd` rule
   would still have had a chance to fire on fallthrough, absent (a) or (b) above.
   Distinguishing (a) from (b) requires inspecting mika-platform's pinned `claude-pilot` version
   and/or its dispatch-time environment at the moment of that specific run — both outside this
   repository and outside this ticket's explicit scope (`no mika, no pilot run`). What is
   established with confidence, from source alone: it is not a live allow-list hole in this
   file today, it is not the relay, and the fix below is correct and version-independent — once
   whichever policy file actually governs a given dispatch is updated to this PR's `bash-cd`, the
   worktree cd is deterministically allowed and no longer subject to intermittence from any
   upstream classifier.

### Fix

`policies/permissions.yaml`, rule `bash-cd`:

```diff
- pattern: "^cd\\s+[A-Za-z0-9_./\\-]+/?$"
+ pattern: "^cd\\s+(?!.*\\.\\.)(?:(?!/)(?!~)(?!\\$)[A-Za-z0-9_./\\-]+|/(?=[A-Za-z0-9_./\\-]*/\\.claude/worktrees/)[A-Za-z0-9_./\\-]+)/?$"
```

Decomposed (unescaped):

```
^cd\s+(?!.*\.\.)(?:
    (?!/)(?!~)(?!\$)[A-Za-z0-9_./\-]+          # (i)  relative
  | /(?=[A-Za-z0-9_./\-]*/\.claude/worktrees/)[A-Za-z0-9_./\-]+   # (ii) absolute, worktree-prefixed
)/?$
```

- `(?!.*\.\.)` — a single negative lookahead, evaluated once against the WHOLE remainder (not
  just the leading segment), excludes any `..` occurrence from either branch. This closes the
  relative-traversal hole (`cd ../../etc`) that the old charset — which included `.` — never
  blocked, and also blocks smuggling `..` inside an otherwise worktree-prefixed absolute path
  (`cd /.../.claude/worktrees/x/../../../etc`).
- Branch (i), relative: unchanged charset (`[A-Za-z0-9_./\-]+`, no shell metacharacters), with
  explicit negative lookaheads against a leading `/`, `~`, or `$` (the last two were already
  excluded by the charset; spelled out here for symmetry/readability with branch (ii)).
- Branch (ii), absolute worktree: requires a leading `/`, then a **positive lookahead** requiring
  the literal substring `/.claude/worktrees/` to appear later in the target — note the leading
  `/` in that lookahead is load-bearing: it requires a genuine path-segment boundary immediately
  before `.claude`, not merely the bare substring `.claude/worktrees/`, which a sibling directory
  named `foo.claude/worktrees/` could otherwise spoof (verified negative:
  `cd /data/workspace/foo.claude/worktrees/evil` → `deny`,
  `test_cpp199_bash_cd_worktree_prefix_spoof_denied`).
- No Python helper needed. A worktree-prefix check reduces to a literal substring match (no
  filesystem canonicalization, no symlink resolution) — the smallest correct change stays a single
  YAML regex, consistent with every other named rule in this file.
- `cd '; rm -rf ~'`-class injection is unaffected: the charset (unchanged) excludes `;`, `|`, `&`,
  `$`, quotes, and backticks in both branches, so those commands never reach the lookaheads at all
  and fall straight through to the policy default.

### Acceptance criteria

- **AC1 — worktree + relative `cd` allowed, deterministically.** `cd <path>` where the target is
  a relative path, OR an absolute path containing a `/.claude/worktrees/` segment, matches
  `bash-cd` as `allow` via a single deterministic regex — it never falls to a non-deterministic
  classifier (and, per AC3 point 3, could not have anyway while Tier 2 is enabled). Covers the
  exact mika#2458-iter2 command plus ordinary relative navigation (`cd src/foo`, `cd -`).
  → `test_cpp199_bash_cd_worktree_absolute_path_allowed`,
  `test_cpp199_bash_cd_relative_path_allowed`,
  `test_cpp199_bash_cd_end_to_end_handler_allows_worktree_cd` (full
  `create_permission_handler` path, not just the raw policy probe).
- **AC2 — absolute non-worktree `cd` refused (mandatory negative test).** `cd /`, `cd /etc`,
  `cd ~`, `cd /root`, and `cd /data/workspace/mika-platform` (a repo root — distinguishes "an
  absolute path that happens to be a git checkout" from "an absolute path under a managed dispatch
  worktree") all now fall through to `default: deny`. On pristine `main` every one of these except
  `cd ~` matched `bash-cd` as `allow` (see AC3 point 2 and the red-before capture below) — this is
  the over-block-in-reverse closure. Relative `cd src/foo` stays allowed (not collateral damage).
  → `test_cpp199_bash_cd_absolute_non_worktree_denied`.
- **AC3 — static-vs-relay/per-spawn characterization.** See the dedicated section above. Base
  policy allows the founding command deterministically (not a hole in this file); the relay is
  structurally unreachable while Tier 2 is enabled (default); the production deny's signature
  (`rule_id=None`) is consistent with a *different* policy file than this repo's current bundled
  default having governed that specific dispatch (stale pin predating cpp#97, or an operator
  overlay) — which of the two is outside this repository's own visibility and this ticket's scope.
- **Guard — traversal stays refused, either shape.** `cd ..`, `cd ../../etc`,
  `cd foo/../../etc`, and `cd /.../.claude/worktrees/x/../../../etc` (traversal smuggled inside an
  otherwise worktree-prefixed absolute path) all fall through to `deny`.
  → `test_cpp199_bash_cd_traversal_denied_both_shapes`.
- **Guard — worktree-prefix spoof resistance.** The positive lookahead requires a genuine
  `/.claude/worktrees/` path-segment boundary, not the bare substring — a sibling directory named
  to contain that substring does not qualify. → `test_cpp199_bash_cd_worktree_prefix_spoof_denied`.
- **Non-regression — cpp#176/#178 unaffected.** `cd` is not a write; it never reaches
  `_destination_veto_reason` (the cpp#176 redirect-containment code) or the cpp#178 chain-safety
  paths those tickets touch. Full `test_permissions.py` destination-veto battery (the
  `test_destination_veto_*` functions, including the cpp#176 negative-test gate) and the full
  `test_policy_devpilot.py` chain-safety battery re-run unmodified and green (see Verification).
- **Non-regression — existing cpp#97 test updated, not silently broken.**
  `test_bash_cd_rule_allows_worktree_absolute_path` (cpp#97's own positive-shape test) had one
  case removed — `cd /tmp/spawn-worktree` — because that shape is an absolute path with no
  worktree relation, i.e. exactly what AC2 now closes. This is a deliberate, documented behavior
  change (flagged judgment call below), not an accidental regression: the case moved to
  `test_cpp199_bash_cd_absolute_non_worktree_denied`'s negative list.

### Judgment calls (flagged explicitly)

1. **Worktree-prefix definition.** The operator's directive defines it as "a path containing
   `/.claude/worktrees/`". Implemented as a literal-substring lookahead requiring a leading `/`
   immediately before `.claude` (a genuine path-segment boundary), not a bare substring match —
   the bare-substring reading would have been spoofable by a sibling directory name. This is a
   strictly narrower, safer reading of the same directive; flagging in case the operator intended
   the looser bare-substring form for some reason not visible from here.
2. **cpp#97's `cd /tmp/spawn-worktree` case.** Pre-existing test asserted this absolute,
   non-worktree path was `allow` (a residue of cpp#97's original "any absolute path" shape, before
   this repo's `.claude/worktrees/` convention existed as the defined boundary). Under the letter
   of AC2 ("absolute cd NOT under a managed-worktree prefix is refused") this must now deny. No
   `/tmp` scratch exception was added for `cd` (unlike `bash-mkdir-tmp-scratch`'s explicit `/tmp`
   carve-out for `mkdir`) because the operator's contract for this ticket names exactly two shapes
   (relative, worktree-absolute) and no third. If a `/tmp` cd exception is wanted, that is new
   scope for a follow-up ticket with its own evidence, not folded in here.
3. **Relative-cd edge: `cd -` (shell "previous directory" idiom).** Charset already admits a bare
   `-` (it was in the original charset via `\-`), unaffected by this change materially, kept
   working and covered by a new positive test.
4. **Full end-to-end handler AC2 test intentionally NOT written.** `tier1.SAFE_SHELL_COMMANDS`
   treats bare `cd <path>` as unconditionally safe regardless of destination — confirmed
   empirically (`is_tier1_auto_approve("Bash", {"command": "cd /etc"}, cwd)` is `True` on pristine
   `main`, unaffected by this PR). That is the pre-classifier GATE, explicitly off-limits per the
   operator's bearing ("fix the NAMED allow-rule ... never the gate"). AC1/AC2 are therefore
   verified at the Tier-2 policy layer directly, which both matches the operator's own prescribed
   probe method and is the layer where AC3 shows the production incident actually happened
   (`rule_id=None`, a Tier-2 default-deny — Tier 1 was presumably also different/stale in that
   specific dispatch, or the founding mystery noted in cpp#97's own comment — "tier1 auto-approve
   path is not firing for absolute worktree paths — root cause under investigation" — is a
   separate, still-open question this ticket does not resolve). Closing the tier1 gate for `cd` is
   a materially larger, separate change outside this ticket's scope.

## Verification

Red-before (pristine `main`, `git stash` the source edit, same probe API the ticket prescribes):

```
'cd /data/workspace/mika-platform/.claude/worktrees/bug-1910-llm-glm-5-2-silent-empty-output-au-max/mika' -> allow  (bash-cd)
'cd src/foo'                                          -> allow  (bash-cd)
'cd /'                                                 -> allow  (bash-cd)
'cd /etc'                                              -> allow  (bash-cd)
'cd ~'                                                 -> deny   (None)
'cd /root'                                             -> allow  (bash-cd)
'cd /data/workspace/mika-platform'                     -> allow  (bash-cd)
'cd ..'                                                -> allow  (bash-cd)
'cd ../../etc'                                         -> allow  (bash-cd)
'cd /tmp/spawn-worktree'                               -> allow  (bash-cd)
```

Green-after (same probe, fix applied): worktree + relative cases stay `allow` (`bash-cd`); every
absolute non-worktree case (`cd /`, `cd /etc`, `cd /root`, `cd /data/workspace/mika-platform`,
`cd /tmp/spawn-worktree`) and both traversal cases (`cd ..`, `cd ../../etc`) flip to `deny`
(`None`, policy default); `cd ~` is unchanged (`deny`, `None`).

Full battery: `uv run pytest` (1173 passed), `uv run ruff check .` (all checks passed),
`uv run mypy src` (no issues, 23 files), `./scripts/verify-pipeline.sh` (both a `docs/` and a
`src/` change present in the diff — passes the bucket-comparison gate). cpp#176/#178
(`test_destination_veto_*`, `test_cpp190_*`/chain-safety batteries) re-run unmodified, all green —
`cd` never reaches `_destination_veto_reason` or the chain-safety redirect paths those tickets
own, confirmed orthogonal.
