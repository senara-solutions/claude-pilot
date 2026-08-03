---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
product_contract_source: ce-plan-bootstrap
origin: https://github.com/senara-solutions/claude-pilot/issues/100
plan_type: fix
plan_depth: lightweight
target_repo: claude-pilot-py
---

# fix(permissions): cpp#100 compound allow — `<safe-read>;<echo>;<script-exec>;||<echo>` shape

**Created:** 2026-08-03
**Issue:** senara-solutions/claude-pilot-py#100 (Vincent-ratified 2026-08-03 11:07 UTC via samidarko-proxy)
**Priority:** P0-CRITICAL — loop-productivity substrate block
**Ship path:** DECISION-CORE → Vincent hand-merge → `uv tool install --force --editable /data/workspace/mika-platform/claude-pilot`

---

## Summary

Add a sanctioned compound-shape rule `bash-explore-script-fallback` to `src/claude_pilot/policies/permissions.yaml` and short-circuit it in `src/claude_pilot/permissions.py::_bash_allow_is_chain_safe`, mirroring the cpp#35 (`bash-git-show-redirect`) and cpp#92 (`bash-for-loop-safe-body`) pattern. Unblocks the mika-platform autonomous loop, which has produced 0 auto-merges for 5 days (since #1874, 2026-07-29) because every dev-groom dispatch cannot pass its `scripts/derive-branch-name` sanity check.

---

## Problem Frame

`_bash_allow_is_chain_safe` splits compound commands by `;`/`||`/`&&` and requires each segment to be independently tier1-safe OR a clean (non-tier3) policy allow. dev-groom's dispatch-lib routinely emits a 4-segment compound to sanity-check derivation scripts before invocation:

```
cat scripts/<script-name> 2>/dev/null | head -30 ; echo "===MARKER===" ; ./scripts/<script-name> "<arg1>" "<arg2>" ... 2>/dev/null || echo "(script signature diff...)"
```

Two segments fail the current gate:
- **Segment 1** (`cat <file> 2>/dev/null | head -N`) — `2>/dev/null` redirect + pipe. Redirects are wholesale-vetoed by tier1 (cpp#35 comment: *"a single segment with a redirect is never tier1-safe and is always tier3-dangerous"*).
- **Segment 3** (`./scripts/<name> "<arg>" ... 2>/dev/null`) — local script execution (`./scripts/...`) is not in tier1's safe-command list.

Segments 2 and 4 (`echo "<literal>"`) are tier1-safe. The compound as a whole vetoes on the first failing segment.

**Hard evidence — production halt @ 2026-08-03 08:02:14 UTC:**
- mika-spirit task `1a4244b6-31d3-49b9-abf7-b5bff50a0842` (groom mika#1867)
- callback task `28ec9f02-b076-4a92-808b-3e0636d86287`, PID 42539
- Halt: `[policy:deny] Bash: cat scripts/derive-branch-name 2>/dev/null | head -30; echo "===DERIVE==="; ./scripts/derive-branch-name "fix" "1867" "fidelity mika ressert le meme contenu" 2>/dev/null || echo "(script signature dif...`
- Session terminated: 6 turns, $0.61 spent, 16s wall — no plan, no PR.

**Loop impact:** 5 open ready-labeled tickets stuck (#1867, #1828, #1716, #1689, #1574). Every future dev-groom dispatch will hit this wall — `scripts/derive-branch-name` sanity check is ubiquitous in dispatch-lib.

---

## Requirements

**R1 — Allow the exact halt-event shape.** `_bash_allow_is_chain_safe` returns `True` for:
`cat scripts/derive-branch-name 2>/dev/null | head -30; echo "===DERIVE==="; ./scripts/derive-branch-name "fix" "1867" "fidelity mika ressert le meme contenu" 2>/dev/null || echo "(script signature diff)"`

**R2 — Allow variants within the canonical shape template.** Shape: `<safe-read> ; <echo-literal> ; <script-exec-with-args> [2>/dev/null] || <echo-literal>` where:
- `<safe-read>` = `cat <path>` with optional `2>/dev/null` and optional `| head -N`
- `<echo-literal>` = `echo "<charset-restricted-literal>"`
- `<script-exec-with-args>` = `./scripts/<POSIX-safe-name>` with 0+ quoted args (same charset)
- Optional `|| <echo-literal>` fallback

**R3 — Charset-restricted quoted args exclude chain metacharacters.** Quoted literal body character set excludes `"`, backtick, `$`, `;`, `|`, `<`, `>`, `&`, `\` — no substitution, no chained danger, no escapes ride through.

**R4 — Path/name restrictions prevent traversal + injection.**
- File paths (`cat` operand): charset `[A-Za-z0-9_./\-]+` — no shell metachars.
- Script names (after `./scripts/`): charset `[A-Za-z0-9_.\-]+` — no `..`, no `/` (single-level under `scripts/`).

**R5 — Near-variants outside the canonical shape STILL veto.**
- `./bin/foo` (wrong directory prefix) → veto
- `./scripts/foo ; rm -rf /` (dangerous chain in place of quoted arg) → veto
- `./scripts/foo $EVIL` (unquoted expansion) → veto
- `cat file | sh` (piped to shell exec) → veto
- Backtick / `$'...'` funsub in matching shape → veto (existing invariant preserved)

**R6 — Rule ID coupling fails CLOSED.** If the YAML rule is renamed or dropped, `_bash_allow_is_chain_safe` never short-circuits, and dispatch reverts to the current compound-split veto (safe direction).

**R7 — All existing tests pass.** No regression in the 715+ existing test suite. `uv run pytest -q`, `uv run ruff check src tests`, `uv run mypy src` all clean.

---

## Key Technical Decisions

**KTD1 — Sanctioned-exception pattern via anchored YAML rule + Python short-circuit.**
`(session-settled: user-directed — chosen over per-segment allowlist relaxation: keeps chain-safe's segment-split default; single-purpose rule fails closed on rename.)`
Mirror the shape established by cpp#35 (`bash-git-show-redirect`) and cpp#92 (`bash-for-loop-safe-body`):
1. Add one anchored regex rule to `permissions.yaml` matching the ENTIRE compound as a whole-command shape.
2. Add one `if pd.rule_id == "bash-explore-script-fallback": return True` short-circuit in `_bash_allow_is_chain_safe` (positioned after the existing `bash-git-show-redirect` and `bash-for-loop-safe-body` short-circuits, before the segment split).

Rejected alternative: relaxing per-segment tier1 to accept `<safe-cmd> 2>/dev/null` broadly. Would widen the attack surface across all commands with stderr redirects, not just this bounded shape. cpp#34 doctrine says: closed-world literal-match; sanctioned exceptions constrain the WHOLE command shape, not per-segment loosening.

**KTD2 — Regex charset restricts inside quoted arg bodies.**
`(session-settled: user-directed — chosen over permissive quoted-arg body: prevents `"foo`bar"`, `"$(evil)"`, `"a\";rm b"` from riding through quoted arg positions.)`
Quoted-arg body character class: `[^"`\$;|<>&\\]*`. Excludes:
- `"` (nested quote — would break string boundary and admit injected content)
- `` ` `` (backtick command substitution)
- `$` (variable expansion + `$(...)` substitution)
- `;`, `|`, `&` (chain metachars — would allow `"; rm -rf /` inside the quoted arg)
- `<`, `>` (redirects)
- `\` (escape sequences)

Rejected alternative: broader charset admitting apostrophes, colons, spaces. Would ratchet the attack surface upward without evidence of a legitimate need in dev-groom's dispatch-lib usage. Extend later per-evidence, not preemptively.

**KTD3 — Script directory bound to `./scripts/` single-level.**
`(session-settled: user-directed — chosen over `./bin/*|./tools/*|./scripts/*`: dispatch-lib convention is `./scripts/derive-branch-name` etc. Broader script directories = separate ratification per evidence.)`
Only `./scripts/<POSIX-safe-name>` matches. No `./bin/`, no `./tools/`, no nested `./scripts/subdir/foo`. Rationale: matches dispatch-lib's actual convention (verified via halt event). Broader script surfaces open unbounded local-exec paths.

**KTD4 — `head -N` uses `\d+` (line count only, no `-c` byte variant).**
`(session-settled: user-directed — chosen over `head` broad allow: `head -c N` byte-count variant not in observed dev-groom shape; separate ratification if needed.)`
`\s*\|\s*head\s+-\d+` matches `| head -30`, `| head -50`, etc. Does NOT match `head -c 100` (byte count) or bare `head` (unlimited). Bounded to line-count preview.

**KTD5 — Fully anchored regex (`^...$`) with `re.search` semantics per policy.py.**
`(session-settled: user-directed — chosen over unanchored match: policy.py uses `re.search`, and unanchored would match any command containing this compound as a substring, admitting `evil_cmd && <matching compound>` — full-command anchoring prevents ride-along.)`
The regex begins with `^cat` and ends with `$` (after optional `|| echo` fallback). Ensures the compound is the ENTIRE command string, not a substring within a larger dangerous command.

---

## Implementation Units

### U1. Add `bash-explore-script-fallback` YAML rule

- **Goal:** Insert the anchored compound-shape rule into `permissions.yaml` at the tight-shape rules section (before broader `^cat`/`\sgrep\s` rules per first-match-wins ordering).
- **Requirements:** R1, R2, R3, R4, R6 — per KTD1, KTD2, KTD3, KTD4, KTD5.
- **Dependencies:** none (foundational — U2 depends on the rule_id existing).
- **Files:**
  - `src/claude_pilot/policies/permissions.yaml` (modify)
- **Approach:**
  1. Insert the new rule immediately after `bash-for-loop-safe-body` (line ~63) and before `## ---- Read-only repo orientation ----` marker. Ordering rationale: tight-shape whole-command rules must precede broader single-command rules (`^cat`, `\sgrep\s`) which would otherwise `re.search`-match segment tokens first.
  2. The rule carries a design-comment prefix explaining: founding evidence (halt event), shape template (Vincent-worded), per-segment safety invariants (embedded in regex), pattern lineage (cpp#35, cpp#92), rule_id-coupling-fails-closed guarantee.
  3. Regex pattern (single-line YAML string, escaped):
     ```
     ^cat\s+[A-Za-z0-9_./\-]+(\s+2>/dev/null)?(\s*\|\s*head\s+-\d+)?\s*;\s*echo\s+"[^"`$;|<>&\\]*"\s*;\s*\./scripts/[A-Za-z0-9_.\-]+(\s+"[^"`$;|<>&\\]*")*(\s+2>/dev/null)?(\s*\|\|\s*echo\s+"[^"`$;|<>&\\]*")?$
     ```
- **Patterns to follow:** cpp#35 (`bash-git-show-redirect` rule shape + comment style) and cpp#92 (`bash-for-loop-safe-body` at same insertion band).
- **Test scenarios:** none for YAML alone — behavior is verified via U2 tests exercising the full guard path.
- **Verification:** After U2 lands, the tests in U2 pass. YAML syntactically valid: `python -c "import yaml; yaml.safe_load(open('src/claude_pilot/policies/permissions.yaml'))"` exits 0.

### U2. Add `_bash_allow_is_chain_safe` short-circuit + tests

- **Goal:** Recognize the `bash-explore-script-fallback` rule_id in `_bash_allow_is_chain_safe` and short-circuit before segment split, mirroring the existing `bash-git-show-redirect` and `bash-for-loop-safe-body` short-circuits.
- **Requirements:** R1, R5, R6, R7 — per KTD1.
- **Dependencies:** U1 (rule must exist in YAML).
- **Files:**
  - `src/claude_pilot/permissions.py` (modify — add short-circuit block ~line 436, immediately after `bash-for-loop-safe-body` short-circuit)
  - `tests/test_permissions.py` (modify — add 4 new tests per test scenarios below)
- **Approach:**
  1. In `_bash_allow_is_chain_safe`, add after the `bash-for-loop-safe-body` short-circuit block:
     ```python
     # `bash-explore-script-fallback` (cpp#100) — sanctioned exception mirroring
     # `bash-git-show-redirect` (cpp#35) and `bash-for-loop-safe-body` (cpp#92).
     # The rule's YAML pattern anchors the ENTIRE compound: `^cat <path>
     # [2>/dev/null] [| head -N] ; echo "<literal>" ; ./scripts/<name>
     # [<quoted-args>] [2>/dev/null] [|| echo "<literal>"]$`, charset-
     # restricted quoted args exclude chain metachars (`;`/`|`/`&`/backtick/
     # `$`/`<`/`>`/`\`), so no dangerous tail can ride any layer of the
     # compound. Chain-safe honors the rule_id without splitting on `;`/`||`.
     # Founding evidence: mika-spirit task 1a4244b6 halted 2026-08-03T08:02:14Z
     # (5-day mika-platform loop stall, groom-stage substrate block).
     # The rule_id coupling fails CLOSED — if the YAML rule is renamed or
     # dropped, this never fires and dispatch reverts to the compound-split
     # veto (safe direction).
     if pd.decision == "allow" and pd.rule_id == "bash-explore-script-fallback":
         return True
     ```
  2. Add 4 tests to `tests/test_permissions.py` — see Test Scenarios.
- **Patterns to follow:** `permissions.py:416` (`bash-git-show-redirect` short-circuit shape) and `permissions.py:434` (`bash-for-loop-safe-body` short-circuit shape). Test style: existing `test_guard_allows_cpp95_*` / `test_guard_still_vetoes_*` patterns in `test_permissions.py`.
- **Test scenarios:**
  1. **`test_guard_allows_cpp100_explore_script_fallback_shape`** — pin the EXACT halt-event signature (with real-world `derive-branch-name`, args `"fix" "1867" "fidelity mika ressert le meme contenu"`, both `2>/dev/null` redirects, and the `|| echo "(script signature diff)"` fallback). Assert `_bash_allow_is_chain_safe(policy, "Bash", {"command": <halt-event>})` returns `True`.
  2. **`test_guard_allows_cpp100_variant_no_stderr_redirect`** — variant `cat X | head -30; echo Y; ./scripts/Z A B 2>/dev/null || echo W` (Segment 1 without `2>/dev/null`). Assert `True`.
  3. **`test_guard_still_vetoes_cpp100_near_variants_not_on_allowlist`** — parameterized/table test with 5 dangerous near-variants:
     - `./bin/foo` in place of `./scripts/foo` → False
     - `./scripts/foo;rm -rf /` (dangerous chain riding quoted-arg slot with charset violation) → False
     - `./scripts/foo $EVIL` (unquoted `$` expansion) → False
     - `cat file | sh` (piped exec, not `head`) → False
     - `cat scripts/x 2>/dev/null | head -30; ./scripts/y` (missing middle `echo "..."` segment) → False
  4. **`test_guard_still_vetoes_backtick_and_funsub_in_matching_shape`** — even a command whose overall shape matches, if it contains `` ` `` or `$'...'` anywhere, still vetoes (the tier1 backtick/funsub veto at `permissions.py:380-381` runs BEFORE substitution-redaction and thus before the rule short-circuit is reached). Test: `cat X 2>/dev/null | head -30; echo "$'evil'"; ./scripts/Y "arg" 2>/dev/null || echo "fallback"` → False.
- **Verification:**
  - `uv run pytest tests/test_permissions.py -q` — new tests pass, 715+ existing tests pass
  - `uv run pytest -q` — full suite clean
  - `uv run ruff check src tests` — no lint findings
  - `uv run mypy src` — no type issues

---

## Scope Boundaries

**In scope:** exactly the shape described in KTD1-KTD5. One YAML rule, one Python short-circuit, 4 tests.

**Explicitly OUT of scope (deferred to follow-up if evidence emerges):**
- Broader script directories (`./bin/*`, `./tools/*`, `./scripts/subdir/*`) — separate ratification, per KTD3.
- `head -c N` (byte-count variant) — not in observed dev-groom shape, per KTD4.
- More than the four canonical quoted-arg slots in the observed shape — regex accepts any count via `(...)*`, but semantically bounded by the ratified template.
- `>` redirects (as opposed to `2>`) — cpp#35 already handles the sanctioned `git show` case; other `>` redirect shapes deferred.
- General relaxation of `_bash_allow_is_chain_safe` per-segment tier1 rules — rejected by KTD1 doctrine.
- Documentation update in `docs/solutions/` — `/ce:compound` step at pipeline end handles this if warranted.

**NOT deferred (must happen post-merge):**
- `uv tool install --force --editable /data/workspace/mika-platform/claude-pilot` — ship path per issue body (Vincent-executed).
- Re-trigger stuck grooms (#1867 or ready-label bump) — orchestrator-CC (me) verifies post-install.

---

## System-Wide Impact

- **claude-pilot policy classifier** — one new sanctioned-exception rule + one short-circuit. Attack surface strictly bounded by anchored regex + charset restrictions.
- **mika-platform autonomous loop** — expected to resume (0 auto-merges since 2026-07-29, 5 days). First auto-merged PR post-install = success signal per sami-darko REVIVE contract.
- **Downstream mika/mika-cloud/mika-skills sub-repos** — no direct code impact, but every dev-groom dispatch to any sub-repo currently hits this wall. Fix unblocks the queue for all.
- **No API/CLI/schema change.** No env vars added. No user-visible interface change.
- **Deploy footprint:** rebuild claude-pilot Python package + `uv tool install --force`. Editable install means source edits take effect immediately post-install; no restart needed for cpp itself. mika-spirit engine picks up new cpp binary on next dispatch attempt (no engine restart needed — cpp is invoked as subprocess).

---

## Risks & Dependencies

**R1 — Regex too permissive.** If the anchored regex admits a shape that carries hidden danger, the compound would auto-allow. **Mitigation:** charset restrictions on quoted-arg bodies exclude ALL chain metachars + substitution operators. Test scenario 3 (near-variants) exercises the boundary. Test scenario 4 confirms backtick/funsub veto still fires via the existing `permissions.py:380-381` guard (runs BEFORE rule matching).

**R2 — Regex too restrictive.** If the anchored regex misses a common dev-groom variant, the loop stays partially blocked. **Mitigation:** the ratified template covers the observed halt-event shape exactly; near-variants that fall outside the template will emit the same policy-deny + pipeline halt as before — visible + measurable. Next-evidence expansion follows same-shape ratification.

**R3 — Rule ordering.** If placed AFTER `bash-find` or `bash-grep` (line ~89 area), those broader rules' `re.search` semantics might claim the match first via internal `\sgrep\s` — but this specific shape uses `head`, not `grep`, so no immediate ordering conflict. **Mitigation:** insert in the tight-shape band (line ~63, after `bash-for-loop-safe-body`) per the comment guidance at line 46-52. Tests validate this ordering by exercising the full guard path.

**R4 — Test regression.** Existing tests exercise the current chain-safe behavior; a new short-circuit adds a new True-returning path. **Mitigation:** all existing tests still pass because the short-circuit only fires when `pd.rule_id == "bash-explore-script-fallback"` — pre-existing test commands never match this new rule_id.

**Dependencies:**
- None external. Purely additive change to cpp source.
- Post-merge: Vincent hand-merge (DECISION-CORE) + `uv tool install --force`. No other repo, service, or config change required.

---

## Verification Contract

**Pre-merge (in this PR):**
1. `uv run pytest -q` — full suite passes (715+ existing tests + 4 new tests).
2. `uv run ruff check src tests` — clean, no lint findings.
3. `uv run mypy src` — no type issues in 19+ source files.
4. YAML parses: `python -c "import yaml; yaml.safe_load(open('src/claude_pilot/policies/permissions.yaml'))"` exits 0.
5. Pipeline artifact verification: `bash scripts/verify-pipeline.sh` (per cpp/mika.md step 6).

**Post-merge (Vincent):**
1. `uv tool install --force --editable /data/workspace/mika-platform/claude-pilot` completes.
2. Verify installed cpp has new rule: `/home/samidarko/.local/share/uv/tools/claude-pilot/bin/python3 -c "from pathlib import Path; import yaml; d=yaml.safe_load(open('/home/samidarko/.local/share/uv/tools/claude-pilot/lib/python3.13/site-packages/claude_pilot/policies/permissions.yaml')); assert any(r.get('id')=='bash-explore-script-fallback' for r in d['rules']), 'rule missing'"` exits 0.

**Post-install (orchestrator-CC monitoring):**
1. Re-trigger groom via `ready`-label bump on #1867 (or wait for next auto_pull_groomed 10-min tick).
2. Verify no `PIPELINE FAILURE.*policy deny` in `$MIKA_SPIRIT_LOG_FILE` for the ensuing 30 min window.
3. **Success signal:** first auto-merged PR since #1874 (2026-07-29) — the true measure per sami-darko REVIVE contract.

---

## Definition of Done

- [ ] U1 landed: YAML rule inserted at the correct band (post-`bash-for-loop-safe-body`, pre-`## Read-only repo orientation`).
- [ ] U2 landed: Python short-circuit added, 4 tests written and passing.
- [ ] `uv run pytest -q` full suite clean.
- [ ] `uv run ruff check src tests` clean.
- [ ] `uv run mypy src` clean.
- [ ] PR opened targeting `main` with issue body reference and DECISION-CORE ratification note.
- [ ] Vincent hand-merges (DECISION-CORE gate per cpp forge-gate).
- [ ] Vincent runs `uv tool install --force --editable /data/workspace/mika-platform/claude-pilot`.
- [ ] Orchestrator-CC verifies installed cpp carries the new rule (test above).
- [ ] Orchestrator-CC verifies next dev-groom dispatch passes the policy stage (no `PIPELINE FAILURE.*policy deny` in log).
- [ ] First auto-merged PR since 2026-07-29 lands — success signal.

---

## Sources & Research

- **Founding halt evidence:** mika-spirit task `1a4244b6-31d3-49b9-abf7-b5bff50a0842`, callback `28ec9f02-b076-4a92-808b-3e0636d86287`, PID 42539 — halted 2026-08-03T08:02:14Z with `[policy:deny]` on the specified compound. Queried via `sqlite3 /home/samidarko/.mika/data/mika.db "SELECT result FROM tasks WHERE id='28ec9f02-...'"`.
- **Loop stall baseline:** 0 auto-merges in `senara-solutions/mika` since PR #1874 (2026-07-29). 5-day gap.
- **Predecessor tickets & patterns:**
  - cpp#95 (rupture-D substitution allowlist) — commit `d4bfa7d`
  - cpp#96 (PR)
  - cpp#97 (`bash-cd` YAML rule) + cpp#98 (`git merge-base 2>/dev/null` tokens) — commit `d6e4809`
  - cpp#99 (PR)
- **Design pattern lineage:**
  - cpp#35 `bash-git-show-redirect` — sanctioned exception via anchored YAML + rule_id short-circuit (`permissions.py:416`).
  - cpp#92 `bash-for-loop-safe-body` — same pattern for compound for-loops (`permissions.py:434`, `permissions.yaml:59`).
- **Doctrine anchor:**
  - cpp#34 closed-world literal-match discipline.
  - cpp#83 AC2 doctrine-anchor comments at classifier tier gates.
- **Ratification chain:**
  - Sami-darko ORG directive 2026-08-03 09:53 UTC (mislabeled #1828 root cause).
  - MPC diagnosis reply 2026-08-03 12:04 UTC (evidence-based rebuttal, proposed Option A).
  - Sami-darko GO cpp#100 message 2026-08-03 11:07 UTC (Vincent-ratified Option A).
  - Vincent direct authorization 2026-08-03 (typed `/mika 100`).
- **Load-bearing feedback memories:**
  - `feedback_hard_evidence_before_filing` — hard evidence attached above.
  - `feedback_n_equals_2_is_the_signal` — n=1 on this specific shape, but broader "cpp deny on dev-groom exploratory" class historically n≥3 (cpp#95/97/98 predecessors), and this shape is the current specific block on the 5-day-stalled loop.
  - `feedback_drain_gated_on_substrate_rule` — substrate blocks the loop; this fix restores drain.
