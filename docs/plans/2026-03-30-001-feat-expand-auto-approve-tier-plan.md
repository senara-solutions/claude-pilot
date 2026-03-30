---
title: "feat: expand auto-approve tier to reduce relay overhead"
type: feat
status: completed
date: 2026-03-30
issue: "#18"
---

# feat: expand auto-approve tier to reduce relay overhead

## Overview

Expand the Tier 1 auto-approve filter in `src/tier1.ts` to cover additional safe command patterns, reducing unnecessary relay round-trips to mika-dev. Session audits show ~60% of relayed tool calls are for provably safe commands (cargo build/test, read-only git, read-only gh). Each relay call costs ~24K input tokens and introduces latency + failure risk (4 documented relay failure modes).

## Problem Statement

During task audit of mika#321, 26 of 42 tool calls went through the relay. Most were harmless commands that the tier1 filter should auto-approve. The current auto-approve rate is ~53% — expanding the safe patterns can push this to ~85%+, saving ~550K tokens of orchestration overhead per task and improving session resilience when relay fails.

## Proposed Solution

Add safe patterns in three categories, maintaining the deny-list-first invariant:

### 1. Additional cargo subcommands (`src/tier1.ts` — `SAFE_CARGO_SUBCOMMANDS`)

Add to existing Set: `clean`, `doc`, `bench`, `tree`, `metadata`

- `cargo clean` — only removes `target/` directory, project-scoped regardless of flags
- `cargo doc` — generates docs to `target/doc/`, project-scoped
- `cargo bench` — executes benchmark code (same risk level as already-approved `cargo test`)
- `cargo tree` — read-only dependency tree to stdout
- `cargo metadata` — read-only JSON metadata to stdout

### 2. Additional npm/npx patterns (`src/tier1.ts` — `isSafeBuildCommand`)

Add `npm test` and `npm start` as npm built-in aliases (currently only `npm run test`/`npm run start` match). Add `npx prettier` and `npx eslint` (consistent with auto-approved `cargo fmt` which also modifies files in-place).

New regex patterns needed alongside existing `npm run` matcher:
- `npm test` / `npm start` (without `run` prefix)
- `npx prettier` / `npx eslint` (with any flags — `--write`/`--fix` are acceptable, consistent with `cargo fmt`)

### 3. Additional gh subcommands (`src/tier1.ts` — rename `isSafePrCommand` → `isSafeGhCommand`)

Add read-only subcommands using Set-based lookups (consistent with existing `gh pr`/`gh issue` pattern):

| Subdomain | Safe subcommands | Notes |
|-----------|-----------------|-------|
| `gh repo` | `view` | Read-only repo info |
| `gh release` | `view`, `list` | Read-only release info |
| `gh workflow` | `view`, `list` | Read-only workflow info. `run` intentionally excluded (triggers dispatch) |

### Excluded from this change (with rationale)

| Pattern | Reason |
|---------|--------|
| `bash scripts/*.sh` | Script contents are arbitrary. Deny-list scans the launcher, not the script. Creates two-step exfiltration chain: Write script (auto-approved within project) → execute script (auto-approved by path). Violates core principle that safe-listed commands have deterministic behavior. |
| `mkdir` | Requires argument parsing to extract paths for `isWithinProject()` check. GNU coreutils flag formats are complex (`-p -m 0755 dir1 dir2`, `--parents dir`, `-pm755 dir`). Implementation complexity outweighs benefit — Write tool creates parent directories implicitly. |
| `gh workflow run` | Triggers workflow dispatch — visible side effect. Falls through to relay naturally. |
| `cargo install` | Installs binaries system-wide — not project-scoped. |
| `cargo publish` | Already in deny-list. |

## Technical Considerations

### Architecture

- **Deny-list-first invariant maintained.** All new patterns are checked AFTER `isTier3Dangerous()` scans the raw command string. Shell redirects (`>`, `>>`), command substitution (`$(...)`, backticks), `xargs`, and `eval` are blocked regardless of command.
- **Rename `isSafePrCommand` → `isSafeGhCommand`.** The function now handles 6 gh subdomains (`pr`, `issue`, `run`, `api`, `repo`, `release`, `workflow`). The "Pr" name is misleading.
- **Consistent pattern: Set-based lookups** for all gh subdomains. Refactor `gh run` from inline regex to Set-based (aligns with `gh pr`/`gh issue` pattern).
- **No new architectural patterns.** All additions fit existing safe-list categories. No path-containment checks needed for Bash commands (only commands with no write side effects are added).

### Security

Per the three-question test from `docs/solutions/architecture/tier1-permission-filter-deny-list-first-pattern.md`:

| New pattern | Can write outside project? | Write side-effects via flags? | Redirect side-effects? |
|------------|---------------------------|------------------------------|----------------------|
| `cargo clean/doc/bench/tree/metadata` | No (all write to `target/`) | No | Caught by deny-list |
| `npm test/start` | No (project-scoped) | No | Caught by deny-list |
| `npx prettier/eslint` | Files within project only | `--write`/`--fix` modify project files (acceptable, same as `cargo fmt`) | Caught by deny-list |
| `gh repo/release/workflow view/list` | No (read-only API calls) | No write flags available for `view`/`list` | Caught by deny-list |

### Performance

- Expected auto-approve rate increase: ~53% → ~85%+
- Per-session savings: ~500K tokens, ~60s latency reduction
- Relay failure exposure reduced by ~60%

## Acceptance Criteria

- [x] `SAFE_CARGO_SUBCOMMANDS` includes `clean`, `doc`, `bench`, `tree`, `metadata` (`src/tier1.ts`)
- [x] `isSafeBuildCommand()` matches `npm test`, `npm start` (without `run` prefix) (`src/tier1.ts`)
- [x] `isSafeBuildCommand()` matches `npx prettier`, `npx eslint` (with any flags) (`src/tier1.ts`)
- [x] `isSafePrCommand` renamed to `isSafeGhCommand` across `src/tier1.ts` and all consumers (`src/permissions.ts`, `test/tier1.test.ts`)
- [x] `isSafeGhCommand()` handles `gh repo view`, `gh release view/list`, `gh workflow view/list` via Set-based lookups (`src/tier1.ts`)
- [x] All new patterns have positive test cases (command auto-approved) (`test/tier1.test.ts`)
- [x] All new patterns have negative test cases (dangerous variant relayed) (`test/tier1.test.ts`)
- [x] Compound command tests: new safe command `&&` existing safe command → auto-approved (`test/tier1.test.ts`)
- [x] Deny-list interaction tests: new command with redirect (e.g., `cargo tree > output.txt`) → blocked (`test/tier1.test.ts`)
- [x] Existing tests pass unchanged (no regressions)
- [x] `npx tsc --noEmit` passes (type-check)

## Files to Modify

- `src/tier1.ts` — Add safe patterns, rename function
- `test/tier1.test.ts` — Add test cases for all new patterns
- `src/permissions.ts` — Update import if function is renamed (check if it imports `isSafePrCommand` directly)

## Sources

- **Issue:** [#18](https://github.com/senara-solutions/claude-pilot/issues/18) — session audit showing relay overhead
- **Security baseline:** `docs/solutions/security-issues/tier1-shell-redirect-bypass.md` — prior bypass that informs what NOT to add
- **Architecture doc:** `docs/solutions/architecture/tier1-permission-filter-deny-list-first-pattern.md` — deny-list-first invariant
- **Relay fragility:** `docs/solutions/integration-issues/relay-json-extraction-from-noisy-stdout.md`, `docs/solutions/integration-issues/silent-relay-disabled-no-diagnostics.md` — 4 documented relay failure modes
