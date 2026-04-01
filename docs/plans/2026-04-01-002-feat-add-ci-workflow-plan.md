---
title: "feat: Add CI workflow (TypeScript build, lint, test)"
type: feat
status: completed
date: 2026-04-01
issue: "#26"
---

# feat: Add CI workflow (TypeScript build, lint, test)

## Overview

claude-pilot has no GitHub Actions CI workflow. PRs are merged with only manual QA review — no automated type-checking, building, or test execution. This was discovered during the dev run audit of PR #25 where `gh pr checks` returned "no checks reported."

## Problem Statement

Without CI, regressions can slip through — broken builds, type errors, and failing tests are only caught locally via lefthook pre-commit hooks. The autonomous dev loop (claude-pilot in worktrees) relies on these hooks, but CI is defense-in-depth. Every check in `lefthook.yml` must have a corresponding CI step using identical commands (per institutional learning: `docs/solutions/build-errors/lefthook-pre-commit-hooks-cross-repo.md`).

## Proposed Solution

Add `.github/workflows/ci.yml` following the mika-cloud CI pattern with a single TypeScript job and a ci-gate job.

### Job Structure

Following the mika-cloud `web` job pattern — a single `typescript` job with typecheck, build, and test as sequential steps (they share the same Node/npm setup), plus a `ci-gate` job.

```
┌─────────────────────┐
│    typescript        │
│  ├─ npm ci           │
│  ├─ tsc --noEmit     │  ← mirrors lefthook typecheck
│  ├─ npm run build    │  ← mirrors lefthook build
│  └─ npm test         │  ← CI-only (vitest)
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│     ci-gate         │
│  if: always()       │
│  needs: [typescript] │
│  fail on failure/   │
│    cancelled        │
└─────────────────────┘
```

### Workflow Configuration

| Setting | Value | Rationale |
|---------|-------|-----------|
| Triggers | `push: [main]`, `pull_request:` | Match mika-cloud (unfiltered PR trigger) |
| Concurrency | `${{ github.workflow }}-${{ github.ref }}` + `cancel-in-progress: true` | Match mika-cloud |
| Permissions | `contents: read` | Least-privilege |
| Runner | `ubuntu-22.04` | Match mika-cloud (pinned, not `latest`) |
| Node version | `.node-version` file (24) | Match local dev environment |
| npm cache | `cache: npm`, `cache-dependency-path: package-lock.json` | Match mika-cloud web job |
| Actions | SHA-pinned with version comments | Match mika-cloud security practice |

### Action SHAs (reuse from mika-cloud)

- `actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd` — v6
- `actions/setup-node@53b83947a5a98c8d113130e565377fae1a50d02f` — v6

### Registry Access

`@anthropic-ai/claude-agent-sdk` is on the public npm registry — no `registry-url` or `NODE_AUTH_TOKEN` needed (unlike mika-cloud's web job which uses GitHub Packages).

### Lefthook Parity

| Check | lefthook | CI | Notes |
|-------|----------|-----|-------|
| `npx tsc --noEmit` | ✅ | ✅ | Same command verbatim |
| `npm run build` | ✅ | ✅ | Same command verbatim |
| `npm test` (vitest) | ❌ | ✅ | CI-only — too slow for pre-commit |
| json-syntax | ✅ | ❌ | Local-only guard |
| no-secrets | ✅ | ❌ | Local-only guard |
| no-large-files | ✅ | ❌ | Local-only guard |

## Technical Considerations

- **No ESLint configured:** The project has no linting tooling. `tsc --noEmit` serves as the static analysis step. Adding ESLint is out of scope for this issue.
- **tsup target mismatch:** `tsup.config.ts` targets `node22` while `.node-version` is `24`. Pre-existing — not blocking for CI but worth noting for a follow-up.
- **Branch protection:** After merging, `CI / CI Gate` should be added as a required status check on `main`. This is an operational step outside the workflow file.

## Acceptance Criteria

- [x] `.github/workflows/ci.yml` exists with `typescript` and `ci-gate` jobs
- [x] CI runs on all PRs (unfiltered `pull_request:` trigger)
- [x] CI runs on pushes to `main`
- [x] `typescript` job runs: `npm ci`, `npx tsc --noEmit`, `npm run build`, `npm test`
- [x] `ci-gate` job uses `if: always()`, `needs: [typescript]`, fails on `failure`/`cancelled`
- [x] Actions are SHA-pinned with version comments
- [x] Node version read from `.node-version` file
- [x] npm dependencies cached via `actions/setup-node` cache
- [x] Concurrency group cancels in-progress runs
- [x] Permissions scoped to `contents: read`
- [x] Lefthook typecheck and build commands match CI exactly

## MVP

### .github/workflows/ci.yml

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: "${{ github.workflow }}-${{ github.ref }}"
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  typescript:
    name: TypeScript
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd  # v6
      - uses: actions/setup-node@53b83947a5a98c8d113130e565377fae1a50d02f  # v6
        with:
          node-version-file: '.node-version'
          cache: npm
          cache-dependency-path: package-lock.json

      - name: Install dependencies
        run: npm ci

      - name: Type-check
        run: npx tsc --noEmit

      - name: Build
        run: npm run build

      - name: Test
        run: npm test

  ci-gate:
    name: CI Gate
    runs-on: ubuntu-22.04
    if: always()
    needs: [typescript]
    steps:
      - name: Check job results
        env:
          TYPESCRIPT: ${{ needs.typescript.result }}
        run: |
          results=("$TYPESCRIPT")
          for r in "${results[@]}"; do
            if [[ "$r" == "failure" || "$r" == "cancelled" ]]; then
              echo "::error::Job result: $r"
              exit 1
            fi
          done
          echo "All jobs passed or were skipped"
```

## Post-Merge

- Add `CI / CI Gate` as a required status check in branch protection settings for `main`

## Sources

- Reference CI: `mika-cloud/.github/workflows/ci.yml` — job structure, SHA pinning, ci-gate pattern
- Lefthook parity: `docs/solutions/build-errors/lefthook-pre-commit-hooks-cross-repo.md` — CI must mirror lefthook
- Toolchain pinning: `mika/docs/solutions/ci-cd/ci-rust-toolchain-version-mismatch.md` — never use floating versions
- Related issue: #26
