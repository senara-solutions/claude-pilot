---
title: "Loading .env from package root in ESM TypeScript CLI"
category: configuration
date: 2026-03-31
severity: low
tags: [dotenv, esm, import-meta, environment-variables, path-resolution]
modules: [cli, ui]
---

# Loading .env from package root in ESM TypeScript CLI

## Problem

claude-pilot needed per-process environment configuration (e.g., `GH_TOKEN` for bot-authored PRs) but had no mechanism to inject its own env vars. The CLI is an ESM project (`"type": "module"`) targeting Node >=24, built with tsup (`shims: true`), and run via tsx in development.

## Root Cause

No dotenv or equivalent was integrated. The process relied entirely on inherited `process.env` from its parent, which prevented the token-swap pattern needed for `gh pr checks` compatibility (fine-grained PATs don't support `gh pr checks`).

## Solution

Added `dotenv` as a runtime dependency and loaded `.env` from the package root at the top of `main()`, before `parseArgs()`.

### Key implementation details

**Path resolution in ESM**: Use `import.meta.dirname` (available in Node >=21) instead of the `fileURLToPath(import.meta.url)` + `dirname()` pattern. This works in both tsx (dev) and tsup-shimmed (build) contexts:

```typescript
// import.meta.dirname resolves to src/ (dev) or dist/ (build) — one level below package root
const envPath = resolve(import.meta.dirname, "..", ".env");
const envResult = dotenv.config({ path: envPath, override: false });
```

**`override: false` is critical**: Ensures parent process env vars (from mika-dev in autonomous sessions) always take precedence over `.env` values. Without this, a `.env` could hijack `ANTHROPIC_API_KEY` or other critical vars.

**Verbose logging follows `[config]` pattern**: The `logEnv()` function in `ui.ts` logs the resolved path and load status, matching the existing `logConfig()` diagnostic pattern. Only fires with `--verbose`.

**scrubEnv interaction**: Variables loaded from `.env` (e.g., `GH_TOKEN`) are visible to the Claude Code SDK subprocess (inherits `process.env` directly) but scrubbed from the relay child process by `scrubEnv()` in `transport.ts` (matches `/TOKEN/i`). This asymmetry is intentional and correct.

### Worktree behavior

`.env` is gitignored, so `git worktree add` does not copy it. Autonomous sessions running in worktrees inherit env vars from the parent process. The `.env` is for local interactive development convenience only.

## Prevention / Best Practices

1. **Use `import.meta.dirname` over `fileURLToPath` dance** in Node >=21 ESM projects. It's cleaner and avoids shadowing tsup's `__dirname` shims.
2. **Always use `override: false`** when loading dotenv in a CLI that may be invoked by other processes. Parent env must win.
3. **Use `DotenvConfigOutput` type** from dotenv instead of inline type shapes to avoid type drift.
4. **Broaden `.gitignore` to `.env*`** with `!.env.example` exception to catch conventional variants (`.env.local`, `.env.production`).
5. **Log config discovery results** at startup following a consistent `[tag] key=value [STATUS]` pattern — silent config failures are the hardest to debug (see `docs/solutions/integration-issues/silent-relay-disabled-no-diagnostics.md`).

## Related

- GitHub issue: senara-solutions/claude-pilot#21
- `docs/solutions/integration-issues/silent-relay-disabled-no-diagnostics.md` — config path logging pattern
- `docs/solutions/code-quality/code-review-fixes-type-safety-and-security-hardening.md` — scrubEnv patterns
