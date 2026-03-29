---
title: "Tier 1 auto-approve expansion with Map-based gh consolidation"
category: architecture
date: 2026-03-30
severity: medium
module: tier1, permissions
tags: [auto-approve, tier1, safe-list, gh-cli, performance, relay-overhead]
related_issue: "#18"
---

# Tier 1 auto-approve expansion with Map-based gh consolidation

## Problem

Session audits showed ~60% of relayed tool calls were for provably safe commands (cargo build/test, read-only git, read-only gh CLI). Each relay round-trip costs ~24K input tokens and introduces latency + failure risk (4 documented relay failure modes). The `isSafeGhCommand` function (formerly `isSafePrCommand`) also accumulated copy-pasted regex+Set blocks for each gh subdomain — 6 identical three-line patterns.

## Root Cause

The tier1 safe-list was conservative by design but hadn't been expanded since initial implementation. Several safe command categories were missing: additional cargo subcommands (`clean`, `doc`, `bench`, `tree`, `metadata`), npm built-in aliases (`npm test`, `npm start` without `run` prefix), npx tools (`prettier`, `eslint`), and read-only gh subdomains (`repo view`, `release view/list`, `workflow view/list`). The `gh api` check also missed the `--input` flag which implies a mutation body.

## Solution

### 1. Expand safe patterns (additive, no new architecture)

- **Cargo:** Added `clean`, `doc`, `bench`, `tree`, `metadata` to `SAFE_CARGO_SUBCOMMANDS` Set
- **npm/npx:** Added `npm test`/`npm start` regex, expanded npx regex to include `prettier`/`eslint`
- **gh CLI:** Added `repo`, `release`, `workflow` subdomains with read-only subcommands

### 2. Consolidate gh subdomains into `Map<string, Set<string>>`

Replaced 6 separate `Set` constants and 6 copy-pasted match blocks with a single `SAFE_GH_SUBCOMMANDS` Map:

```typescript
const SAFE_GH_SUBCOMMANDS: ReadonlyMap<string, ReadonlySet<string>> = new Map([
  ["pr",       new Set(["create", "view", "list", "checkout", "diff", "checks"])],
  ["issue",    new Set(["view", "list"])],
  ["run",      new Set(["view", "list"])],
  ["repo",     new Set(["view"])],
  ["release",  new Set(["view", "list"])],
  ["workflow", new Set(["view", "list"])],
]);
```

Adding a new gh subdomain is now a one-liner instead of a copy-paste block. The `gh api` special case remains separate (flag-based gating, not subcommand lookup).

### 3. Block `gh api --input` flag

Added `--input` to the `gh api` deny check — this flag supplies a request body which implies a mutation.

### 4. Document trusted-project assumption

Added comments clarifying that build/test commands (`cargo`, `npm`, `npx`) are safe only because the project itself is trusted. If the threat model ever changes to include untrusted repos, all build commands must move to Tier 2.

## Key Decisions

- **Excluded `bash scripts/*.sh`:** Script contents are arbitrary; deny-list scans the launcher, not the script. Creates a two-step exfiltration chain.
- **Excluded `mkdir`:** Can create dirs outside project; would need complex argument parsing for path containment. Write tool creates parent directories implicitly.
- **Allowed `npx prettier --write` / `npx eslint --fix`:** Consistent with already-approved `cargo fmt` — formatters modify project files in-place at the same trust level.

## Prevention

- **Three-question test for future safe-list additions:** (1) Can this write outside the project? (2) Can flags change behavior from read to write? (3) Can shell redirects cause side effects? (Already caught by deny-list, but verify.)
- **Deny-list-first invariant:** All new patterns checked AFTER `isTier3Dangerous()` scans raw command string. Never bypass this ordering.
- **Use `ReadonlyMap`/`ReadonlySet` types** for safe-list constants to prevent accidental mutation.
- **Test each new pattern with deny-list interaction tests** (redirect, command substitution) to verify the deny-list catches abuse.

## Impact

- Expected auto-approve rate increase: ~53% → ~85%+
- Per-session savings: ~500K tokens, ~60s latency reduction
- Relay failure exposure reduced by ~60%
- Net LOC reduction despite adding features (Map consolidation removed ~40 lines of copy-paste)
