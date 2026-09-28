---
title: "A destructive verb under a designated scratch prefix can be refused without being lethal"
date: 2026-09-28
last_updated: 2026-09-28
module: claude_pilot.permissions
component: permission-classifier
problem_type: design_decision
category: tooling-decisions
severity: high
tags: [permissions, policy, lethality, denial, rm, pilot-scratch, containment, fail-closed, claude-pilot-205, claude-pilot-213, mika-2054, mika-2544, mika-2548]
applies_when: "carving a survivable-deny exemption for a destructive verb pointed at a designated, sandbox-managed path"
---

# A destructive verb under a designated scratch prefix can be refused without being lethal

## Context

cpp#205 ratified rule (a): destructive verbs (`rm -rf`, `chmod -R`, `dd`,
`mkfs`, fork-bombs) stay TERMINAL regardless of target — a syntactic classifier
cannot prove a recursive blast radius safe, so the verb ends the run wherever it
points. That is correct for `rm -rf` aimed at an arbitrary path. It also killed
pilots that ran `rm -rf` against **their own scratch directory inside the
worktree**: mika#2054 pilot 83db3a82 (`rm -rf verif-2054-redcheck`) and mika#2544
pilot cbdd3f5b (`rm -rf .t2544 && git status`). The cleanup was harmless; the
lethality was not.

Dispatch closed the environment half (mika#2548 / PR mika#2550): it pre-creates a
git-excluded `.pilot-scratch/` directory, emptied each run, and forbids `rm` on
it in the prompt. cpp#213 is the classifier half — it makes the refusal of an
`rm`/`rmdir` confined to that prefix **survivable** instead of session-fatal.

## Guidance

### 1. Flip lethality, never admission — and prove the line did not move

The exemption changes exactly one bit: `_denial_is_terminal` returns `False` for
a confined `rm`/`rmdir`. The command is still **denied** — `is_tier3_dangerous`
(the refusal classifier), `is_tier1_auto_approve`, and every YAML rule are
byte-identical to HEAD, and none of them consults the new code. A survivable deny
is still a deny: the pilot routes around it instead of dying on it. Pin this with
a dedicated admission-identity test (`is_tier3_dangerous` still `True`,
`is_tier1_auto_approve` still `False`, policy still `deny`) alongside the
lethality assertion, so a future edit cannot silently turn the carve into an
allow. The sovereign boundary is that a designated-scratch exemption is worth
building **only** because it can be done without moving the admission line; if it
cannot, stop.

### 2. Resolve the prefix fs-aware against the session worktree, fail-closed

`.pilot-scratch/` is relative to the session worktree (`cwd`), so containment is
a path resolution, not a substring match — the same discipline as
`is_within_project`. `is_within_pilot_scratch` resolves `cwd` with
`Path.resolve(strict=True)` (an unresolvable cwd raises and fails closed) and
compares the resolved target against the **literal** `<cwd>/.pilot-scratch` root.
Keeping the root literal — never `.resolve()`'d itself — is what makes an
outbound symlink fail closed: a `.pilot-scratch` that is a symlink to `/outside`
resolves the target away from the literal root, so `relative_to` raises. Every
rejected class stays terminal: absolute paths (rejected outright), `..` escapes
(collapsed by `resolve`, then out of the prefix), symlink components, and any
target elsewhere in the worktree or under `~`/`$HOME`.

### 3. Carve by removing confined segments and re-asking the unchanged question

The clean way to keep every mixed and chained shape terminal is to reuse the
existing classifier rather than re-derive danger. `rm_confined_to_pilot_scratch`
splits the command, drops each `rm`/`rmdir` segment whose **every** operand is
confined, and re-runs the unchanged `is_tier3_dangerous_for_lethality` on what
remains. If the remainder is still dangerous, it stays terminal — no per-shape
logic needed:

- `rm -rf .pilot-scratch/x /etc/y` — a single `rm` with a mixed operand list is
  not fully confined, so it is never dropped and still matches `rm -rf`.
- `rm -rf .pilot-scratch/x && git reset --hard` — the confined `rm` is dropped,
  but the chained destructive verb survives in the remainder and fires.
- `rm -rf .pilot-scratch/x && rm -rf /etc` — the unconfined second `rm` survives.

This is the exact sibling of cpp#201's `_strip_contained_redirects`: strip the
one thing you can prove safe, then let the untouched danger check speak for the
rest. The fall-through matters too — the carve only neutralizes the *verb* short
circuit, so `_denial_is_terminal`'s redirect and destination vetoes still run on
the **full** command and re-arm a confined `rm` that also redirects out of the
worktree.

### 4. Probe the real behavior at the source before and after

`is_within_project` fails closed on a non-existent cwd, so a synthetic test that
skips `git init` reports false survivability. Build a real temp git worktree with
an actual `.pilot-scratch/` (and a symlinked one for the negative), confirm on
HEAD that `rm -rf .pilot-scratch/x` is currently terminal while the escapes are
too, then confirm the flip. Note that `rm -r`, `rmdir`, and plain `rm` were
already survivable on HEAD — only the `rm -rf`/`-fr` form (both flags) matched
the tier3 verb pattern — so the test suite must pin those as staying survivable,
not claim them as new wins.

## Related

- `docs/solutions/tooling-decisions/a-refusal-and-its-lethality-are-two-decisions-not-one.md` — the cpp#128 split this exemption rides on.
- claude-pilot#205 (proven-danger verb set, case a), #201/#209 (the mktemp lethality carve this mirrors), #213 (this carve), mika#2054/#2544 (the pilot deaths), mika#2548 (the dispatch-side `.pilot-scratch/`).
