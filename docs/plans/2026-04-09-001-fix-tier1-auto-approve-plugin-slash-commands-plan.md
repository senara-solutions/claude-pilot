---
title: "fix: TIER 1 auto-approve plugin slash commands"
type: fix
status: completed
date: 2026-04-09
issue: "#33"
---

# fix: TIER 1 auto-approve plugin slash commands

## Overview

The `/mika` pipeline is fully blocked because plugin-provided slash commands (`/ralph-loop`, `/ce:*`, `/compound-engineering:*`, `/mika-doc-audit`, `/mika`) are routed through the `canUseTool` relay to mika-dev, which fabricates denials with non-existent citations. These slash commands are the pipeline's own orchestration steps -- they should never reach an external LLM for approval.

## Problem Statement

Every `/mika` pipeline dispatch is DOA. When Claude Code invokes a plugin slash command via the `Skill` tool, it hits the `default` case in `isTier1AutoApprove()` (returns `false`) and gets relayed to mika-dev. mika-dev (minimax-m2.7) sees an unfamiliar tool name, defaults to deny, and invents a plausible-sounding precedent. Confirmed impact: mika-skills#104 aborted ($0.20), mika#487 stalled ($1.04), all autonomous dispatch blocked.

This is a structural problem, not an LLM prompt problem. The fix is to not ask the LLM in the first place.

## Proposed Solution

Add a `case "Skill":` block to the `isTier1AutoApprove` switch in `src/tier1.ts` that extracts `input.skill` and checks it against a `Set` of known-safe pipeline skill names. Follow the established pattern (module-level constant, exact string matching, conservative default).

### Key Design Decisions

1. **Exact match via `Set`, not prefix matching.** Prefix matching (`startsWith("ce:")`) risks approving unexpected skills. The deny-list-first principle in tier1.ts favors explicit allowlists. The Set is easy to extend -- one line per new skill.

2. **Both short and fully-qualified forms.** The Skill tool can receive either form (e.g., `ce:plan` or `compound-engineering:ce-plan`). Both must be in the allowlist to prevent bypass. The system prompt's skill list confirms both forms exist.

3. **Include ralph-loop sub-commands.** The system lists `ralph-loop:ralph-loop`, `ralph-loop:cancel-ralph`, `ralph-loop:help` as distinct skill names. These must be in the allowlist since they arrive as separate `input.skill` values, not as args to `ralph-loop`.

4. **Add `summarizeInput` case for `"Skill"`.** Clean log output: `ce:plan [args]` instead of raw JSON.

## Technical Approach

### Files to Modify

| File | Change |
|------|--------|
| `src/tier1.ts` | Add `TIER1_SAFE_SKILLS` constant + `case "Skill":` block |
| `src/permissions.ts` | Add `case "Skill":` to `summarizeInput()` |
| `test/tier1.test.ts` | Add Skill tool test suite |
| `CLAUDE.md` | Document TIER 1 scope for pipeline slash commands |

### Implementation: `src/tier1.ts`

Add a module-level constant following the `SAFE_GIT_SUBCOMMANDS` pattern:

```typescript
// src/tier1.ts

/**
 * Pipeline-internal slash commands that bypass relay approval.
 * These are the agent's own orchestration — never ask mika-dev.
 *
 * Includes both short forms (ce:plan) and fully-qualified forms
 * (compound-engineering:ce-plan) because the SDK can send either.
 */
const TIER1_SAFE_SKILLS: ReadonlySet<string> = new Set([
  // /mika pipeline entrypoint
  "mika",
  // CE workflow commands (short form)
  "ce:plan",
  "ce:work",
  "ce:review",
  "ce:compound",
  "ce:brainstorm",
  // CE workflow commands (fully-qualified form)
  "compound-engineering:ce-plan",
  "compound-engineering:ce-work",
  "compound-engineering:ce-review",
  "compound-engineering:ce-compound",
  "compound-engineering:ce-brainstorm",
  // CE utility commands
  "compound-engineering:resolve_todo_parallel",
  // Ralph loop (all sub-commands)
  "ralph-loop",
  "ralph-loop:ralph-loop",
  "ralph-loop:cancel-ralph",
  "ralph-loop:help",
  // Doc audit
  "mika-doc-audit",
]);
```

Add the switch case:

```typescript
case "Skill": {
  const skill = typeof input.skill === "string" ? input.skill.trim() : "";
  return TIER1_SAFE_SKILLS.has(skill);
}
```

### Implementation: `src/permissions.ts`

Add to `summarizeInput()`:

```typescript
case "Skill": {
  const skill = String(input.skill ?? "unknown");
  const args = input.args ? ` ${String(input.args).slice(0, 100)}` : "";
  return `${skill}${args}`;
}
```

### Implementation: `test/tier1.test.ts`

Add a new `describe("Skill tool — pipeline slash commands")` block covering:

- All allowlisted skills return `true` (both short and fully-qualified forms)
- Non-allowlisted skills return `false`
- Missing `input.skill` returns `false`
- Non-string `input.skill` returns `false`
- Empty string `input.skill` returns `false`
- Whitespace-padded skill names are trimmed and matched
- Skill names are case-sensitive (`CE:PLAN` returns `false`)

### Implementation: `CLAUDE.md`

Add a paragraph to the Architecture section:

> **Pipeline slash commands bypass mika-dev approval.** The `Skill` tool invocations for `/mika`, `/ce:*`, `/ralph-loop`, `/compound-engineering:resolve_todo_parallel`, and `/mika-doc-audit` are auto-approved at TIER 1. These are the agent's own orchestration steps -- routing them through the relay exposes them to LLM-driven approval that can rationalize fabricated denials. The allowlist is in `TIER1_SAFE_SKILLS` in `src/tier1.ts`.

## System-Wide Impact

- **Interaction graph**: `canUseTool` → `isTier1AutoApprove` → (new) `TIER1_SAFE_SKILLS.has()` → returns `true` → auto-approve. No relay call, no transport, no idle timer pause/resume. Sub-agent Skill calls are already auto-allowed before reaching `canUseTool`.
- **Error propagation**: No new error paths. The `typeof` + `.trim()` guards handle malformed input by falling through to `false` (relay decides).
- **State lifecycle risks**: None. This is a pure filter -- no state persisted, no side effects.
- **API surface parity**: The `Skill` tool is the only tool for slash command invocations. No other interface needs updating.
- **Integration test scenarios**: (1) Run a `/mika` pipeline dispatch and verify it progresses past step 1 without relay intervention. (2) Invoke an unknown skill and verify it still gets relayed.

## Acceptance Criteria

- [x] `TIER1_SAFE_SKILLS` constant in `src/tier1.ts` with all pipeline slash commands (short + fully-qualified forms)
- [x] `case "Skill":` in `isTier1AutoApprove` extracts `input.skill`, trims whitespace, checks against Set
- [x] `case "Skill":` in `summarizeInput` for clean log output
- [x] Tests in `test/tier1.test.ts`: all allowlisted skills approved, non-allowlisted relayed, defensive edge cases
- [x] `CLAUDE.md` documents TIER 1 scope for pipeline slash commands
- [x] `npm test` passes, `npx tsc --noEmit` passes

## Sources

- Issue: senara-solutions/claude-pilot#33
- Tier 1 filter: `src/tier1.ts:18-48`
- Permission handler: `src/permissions.ts:32-123`
- summarizeInput: `src/permissions.ts:264-281`
- Test suite: `test/tier1.test.ts`
- Learnings: `docs/solutions/architecture/tier1-permission-filter-deny-list-first-pattern.md`
- Learnings: `docs/solutions/architecture/tier1-auto-approve-expansion-map-consolidation.md`
- Learnings: `docs/solutions/security-issues/tier1-shell-redirect-bypass.md`
