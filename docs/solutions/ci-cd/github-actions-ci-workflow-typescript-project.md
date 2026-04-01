---
title: "GitHub Actions CI workflow for TypeScript project"
category: ci-cd
date: 2026-04-01
severity: high
tags: [ci, github-actions, typescript, ci-gate, lefthook-parity]
modules: [claude-pilot]
---

# GitHub Actions CI Workflow for TypeScript Project

## Problem

claude-pilot had no CI workflow. PRs were merged with only manual QA review — `gh pr checks` returned "no checks reported." The autonomous dev loop (claude-pilot in worktrees) relies on lefthook pre-commit hooks, but worktrees often bypass hooks when lefthook is not installed, making CI the only defense-in-depth layer.

## Root Cause

Greenfield repository that shipped without CI infrastructure from day one. Discovered during dev run audit of PR #25.

## Solution

Added `.github/workflows/ci.yml` following the mika-cloud CI architecture pattern:

```yaml
# Key structure — single TypeScript job + ci-gate aggregator
jobs:
  typescript:
    name: TypeScript
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@<sha>  # v6 — SHA-pinned
      - uses: actions/setup-node@<sha>  # v6 — SHA-pinned
        with:
          node-version-file: '.node-version'
          cache: npm
          cache-dependency-path: package-lock.json
      - run: npm ci
      - run: npx tsc --noEmit      # mirrors lefthook typecheck
      - run: npm run build          # mirrors lefthook build
      - run: npm test               # CI-only (vitest)

  ci-gate:
    if: always()
    needs: [typescript]
    # Fails if any upstream job is failure or cancelled
```

### Key conventions (matching mika-cloud):

| Convention | Value |
|-----------|-------|
| Actions | SHA-pinned with version comments |
| Runner | `ubuntu-22.04` (pinned, not `latest`) |
| Permissions | `contents: read` (least-privilege) |
| Concurrency | `${{ github.workflow }}-${{ github.ref }}` + `cancel-in-progress` |
| Node version | From `.node-version` file (not hardcoded) |
| Gate job | `ci-gate` with `if: always()` — single required status check |

### Lefthook parity rule

Every check in `lefthook.yml` must have a corresponding CI step using identical commands. CI can add additional checks (like `npm test`) that are too slow for pre-commit. See `docs/solutions/build-errors/lefthook-pre-commit-hooks-cross-repo.md`.

## Prevention

- **New repos**: Add CI workflow before the first PR is merged.
- **New checks**: When adding a check to lefthook, add it to CI in the same PR (and vice versa).
- **Post-merge**: Add `CI / CI Gate` as required status check in branch protection settings.
- **Action updates**: Use SHA pins, not tags. Update SHAs across all repos simultaneously.

## Related

- mika-cloud CI reference: `mika-cloud/.github/workflows/ci.yml`
- Lefthook parity: `docs/solutions/build-errors/lefthook-pre-commit-hooks-cross-repo.md`
- Worktree hook bypass: `mika-platform/docs/solutions/build-errors/lefthook-not-installed-worktree-ci-failure.md`
- Toolchain version pinning: `mika/docs/solutions/ci-cd/ci-rust-toolchain-version-mismatch.md`
- GitHub issue: #26
