---
title: "feat: load .env file from package root"
type: feat
status: completed
date: 2026-03-31
---

# feat: load .env file from package root

## Overview

Add dotenv support to claude-pilot so it loads a `.env` file from its own package root directory at startup, before the SDK session begins. This enables per-process configuration (e.g., `GH_TOKEN` for bot account PRs) without polluting the target project's environment.

## Problem Statement / Motivation

`gh pr checks` doesn't work with fine-grained GitHub PATs — a known GitHub CLI limitation. Currently, claude-pilot inherits `process.env` from its parent and has no way to inject its own env vars. The `.env` file enables a token swap where:

- Agents (`run_gh`) fall back to host `gh auth` (classic PAT) — `gh pr checks` works
- claude-pilot sessions use a **different** `GH_TOKEN` (bot's fine-grained PAT) so PRs appear from the bot account

## Proposed Solution

Load a `.env` file from the claude-pilot package root at the very top of `main()`, before `parseArgs()`. Use dotenv's `override: false` to preserve existing `process.env` values.

### Key design decisions

1. **Package root, not `--cwd`**: The `.env` is claude-pilot's own config. The `--cwd` flag targets the project Claude Code operates on — these must not be mixed.
2. **`import.meta.url` for path resolution**: The project is ESM (`"type": "module"`). Native ESM does not provide `__dirname`. Use `fileURLToPath(import.meta.url)` which works in both tsx (dev) and tsup-shimmed (build) contexts.
3. **No override**: Existing `process.env` takes precedence. This lets parent processes (mika-dev) inject vars that `.env` cannot override.
4. **Silent on missing file**: dotenv natively handles missing files gracefully. No error, no warning.
5. **Verbose logging**: Log `.env` discovery result when `--verbose` is active, following the existing `[config]` logging pattern.

## Technical Considerations

### Path resolution across environments

| Context | Entry point | `import.meta.url` resolves to | `../.env` resolves to |
|---------|-------------|-------------------------------|----------------------|
| Dev (`bin/claude-pilot`) | `tsx src/cli.ts` | `src/cli.ts` | package root `.env` |
| Dev (`npm run dev`) | `tsx src/cli.ts` | `src/cli.ts` | package root `.env` |
| Production (`dist/cli.js`) | tsup bundle | `dist/cli.js` | package root `.env` |

### Env var visibility

| Consumer | Sees `.env` vars? | Mechanism |
|----------|-------------------|-----------|
| Claude Code SDK subprocess | Yes | Inherits `process.env` directly |
| Relay child process (`execFile`) | Filtered | `scrubEnv()` strips sensitive patterns (`/TOKEN/i`, `/KEY/i`, etc.) |

This is the desired behavior: `GH_TOKEN` reaches Claude Code (for `gh` commands) but is scrubbed from the relay agent.

### Worktree behavior

`.env` is gitignored, so `git worktree add` will not copy it. Worktree-based autonomous sessions inherit env vars from the parent process instead. This is acceptable — `.env` is for local interactive development convenience.

## Acceptance Criteria

- [x] `dotenv` added as runtime dependency — `src/cli.ts`
- [x] `.env` loaded from package root as first operation in `main()` — `src/cli.ts`
- [x] Existing `process.env` values are NOT overridden (dotenv `override: false`)
- [x] Missing `.env` file does not produce errors or warnings
- [x] `.env` added to `.gitignore`
- [x] `.env.example` created with documented variables — `.env.example`
- [x] CLAUDE.md updated with `.env` documentation — `CLAUDE.md`
- [x] Verbose logging shows `.env` load status — `src/cli.ts`

## MVP

### src/cli.ts (top of file — new import)

```typescript
import dotenv from "dotenv";
import { fileURLToPath } from "node:url";
```

### src/cli.ts (top of main(), before parseArgs)

```typescript
async function main(): Promise<void> {
  // Load .env from package root (does not override existing env)
  const __filename = fileURLToPath(import.meta.url);
  const __dirname = path.dirname(__filename);
  const envPath = path.resolve(__dirname, "..", ".env");
  const envResult = dotenv.config({ path: envPath, override: false });

  const opts = parseArgs(process.argv);
  // ... existing code
```

### src/cli.ts (after parseArgs, in verbose block)

```typescript
if (opts.verbose) {
  logEnv(envPath, envResult);
}
```

### src/ui.ts (new function)

```typescript
export function logEnv(envPath: string, result: { error?: Error; parsed?: Record<string, string> }): void {
  if (result.error) {
    log("env", `path=${envPath} [NOT FOUND]`);
  } else {
    const count = result.parsed ? Object.keys(result.parsed).length : 0;
    log("env", `path=${envPath} [LOADED] vars=${count}`);
  }
}
```

### .env.example

```bash
# claude-pilot environment configuration
# Copy to .env and fill in values. Existing process.env takes precedence.

# GitHub token for PR creation (fine-grained PAT for bot account)
# GH_TOKEN=ghp_xxx
```

### .gitignore (append)

```
.env
```

### CLAUDE.md (add to Configuration section)

```markdown
## Environment Variables

Place a `.env` file in the claude-pilot root directory (alongside `package.json`) to set process-level env vars. Values do NOT override existing `process.env` entries.

Example: create `.env` with `GH_TOKEN=<bot-pat>` so PRs appear from the bot account while agents use the host's `gh auth` for `gh pr checks`.

The `.env` file is gitignored and not copied to worktrees. Autonomous sessions inherit env vars from the parent process instead.
```

## Dependencies & Risks

- **New dependency**: `dotenv` — lightweight, well-maintained, zero transitive deps
- **Risk**: Low. The change is additive (no existing behavior modified) and gracefully handles missing files
- **Build impact**: dotenv will be bundled by tsup into `dist/cli.js` — negligible size increase

## Sources

- GitHub issue: senara-solutions/claude-pilot#21
- Institutional learning: `docs/solutions/code-quality/code-review-fixes-type-safety-and-security-hardening.md` — scrubEnv patterns are intentionally broad
- Institutional learning: `docs/solutions/integration-issues/silent-relay-disabled-no-diagnostics.md` — config path logging pattern
