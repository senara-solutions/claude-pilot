---
title: "TIER 1 auto-approve for pipeline slash commands (Skill tool)"
category: architecture
date: 2026-04-09
tags: [tier1, permissions, skill, slash-commands, pipeline, auto-approve]
module: tier1, permissions
severity: high
issue: "senara-solutions/claude-pilot#33"
---

# TIER 1 auto-approve for pipeline slash commands (Skill tool)

## Problem

Every `/mika` pipeline dispatch was blocked at step 1 because plugin-provided slash commands (`/ralph-loop`, `/ce:*`, `/compound-engineering:*`, `/mika-doc-audit`) were routed through the `canUseTool` relay to mika-dev. mika-dev (running minimax-m2.7) saw unfamiliar `Skill` tool invocations, defaulted to deny, and fabricated plausible-sounding precedent citations that did not exist. The autonomous dev loop was fully blocked.

Impact: mika-skills#104 aborted ($0.20, 3 turns), mika#487 stalled ($1.04, 12 turns), all sub-repo `/mika` dispatches DOA.

## Root Cause

The `isTier1AutoApprove` switch in `src/tier1.ts` had no `case "Skill":` — all Skill tool invocations fell through to `default: return false`, triggering relay to mika-dev. This is a structural problem: the pipeline's own orchestration commands were being submitted to an external LLM for approval, which has no way to know it configured the pipeline to call these commands. Same class of failure as `feedback_prompt_enforcement_fragile` — negative rules enforced via LLM judgment are unreliable.

## Solution

Added a `TIER1_SAFE_SKILLS` ReadonlySet in `src/tier1.ts` containing both short and fully-qualified forms of all pipeline slash commands (18 entries total). Added `case "Skill":` to `isTier1AutoApprove` that extracts `input.skill`, trims whitespace, and checks against the Set. Conservative default preserved — unknown skills still relay.

Key implementation details:
- **Exact match via `Set.has()`, not prefix matching.** `startsWith("ce:")` would risk approving unexpected skills. Explicit allowlists are safer.
- **Both short and fully-qualified forms.** The SDK can send either `ce:plan` or `compound-engineering:ce-plan`. Both must be allowlisted.
- **Args intentionally ignored.** The skill name is the trust boundary. Skills are prompt-only orchestration; any tool calls they make go through their own `canUseTool` check independently.
- **`summarizeInput` updated** with `case "Skill":` for clean log output (`ce:plan #33` instead of raw JSON), with `scrubSecrets()` on args.

Files changed:
- `src/tier1.ts` — `TIER1_SAFE_SKILLS` constant + `case "Skill":` block
- `src/permissions.ts` — `case "Skill":` in `summarizeInput()`
- `test/tier1.test.ts` — 12 test cases covering allowlisted skills, non-allowlisted, defensive edge cases
- `CLAUDE.md` — documented TIER 1 scope for pipeline slash commands

## Prevention

1. **When adding new pipeline skills**, add them to `TIER1_SAFE_SKILLS` in `src/tier1.ts`. The comment documents the extension point: "To add a new pipeline skill: add the exact `input.skill` string here."
2. **Apply the three-question test** (from `docs/solutions/architecture/tier1-auto-approve-expansion-map-consolidation.md`) before adding any skill: Can it write outside the project? Can args change behavior from safe to destructive? Are downstream tool calls individually gated?
3. **Never use prefix matching for skill allowlists.** The deny-list-first, conservative-default principle in `tier1.ts` requires explicit entries.
4. **Don't route pipeline-internal orchestration through LLM-driven gates.** The LLM has no way to distinguish "this is my own pipeline step" from "this is an unfamiliar tool" — it will rationalize denials.

## Related

- `docs/solutions/architecture/tier1-permission-filter-deny-list-first-pattern.md` — foundational deny-list-first architecture
- `docs/solutions/architecture/tier1-auto-approve-expansion-map-consolidation.md` — previous tier1 expansion (three-question test)
- `docs/solutions/security-issues/tier1-shell-redirect-bypass.md` — precedent for safe-list over-permissiveness
- senara-solutions/claude-pilot#33 — the issue
- senara-solutions/mika-skills#111 — same class of LLM rationalization failure (qa-review CI)
