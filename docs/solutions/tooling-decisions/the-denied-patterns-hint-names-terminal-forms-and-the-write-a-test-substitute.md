---
title: "The denied-patterns hint names the TERMINAL forms and the 'write a test, not a shell probe' substitute"
date: 2026-10-02
problem_type: tooling_decision
track: knowledge
module: tier1
component: model-facing-prevention-hint
tags:
  - claude-pilot
  - tier1
  - denied-bash-patterns-hint
  - lethality
  - terminal-denial
  - system-prompt
  - shell-probe
applies_when: "A pilot keeps dying by running an ad-hoc shell/git probe to OBSERVE a behaviour; or you want to tell every pilot which refused forms END the session (not just get refused) and what to reach for instead; or you are adding/removing a terminal form the hint should name."
---

# The denied-patterns hint names the TERMINAL forms and the "write a test" substitute

## Context

`DENIED_BASH_PATTERNS_HINT` (`tier1.py`) is the single model-facing prevention
payload appended to every pilot's system prompt by `agent.py`. Since mika#1409 it
named the commonly-refused Bash patterns and steered them to native tools. Since
cpp#128/#205 most denials are *survivable*: the refusal comes back as a
`tool_result` error the model can adapt to, and only a proven-danger verb or an
out-of-worktree write **ends the session** (`permissions._denial_is_terminal`).

The hint did not encode that distinction. It listed refused patterns but not
which ones KILL the run, nor what to do instead when the goal was to *observe*
a behaviour rather than mutate state.

## Problem

Three pilots were killed in 24 h (`93bac846` mika#2626, `c722b251` mika#2623,
`debf318f` mika#2634) by the same ad-hoc "shell probe": each wanted to observe a
shell/git behaviour (a captured stderr, a git state, a redirect outcome) and ran
a one-shot script instead of writing a test. `bash -c 'exit 1'`, `sh -c …`,
`eval`, `sed -i` out-of-worktree, an out-of-worktree redirect, and `rm -rf` on an
unresolved target are all *terminal by design* — refusal plus run-death, no retry.
Each death was followed by a per-ticket instruction, which does not scale. The
remedy had to be said once, in the one place every pilot reads.

## Decision

This is a **prompt-layer measure**, not a classifier change. It names the terminal
forms and gives the substitute, and it complements — does **not** replace — the
structural lethality carves (cpp#268/#272/#279), which are the real defense.

1. **Lead the hint with a terminal-forms block.** A terminal denial is the
   costliest failure a pilot can hit, so it goes first: five one-liners, each
   "refused AND the session ENDS" — `bash -c`/`sh -c`, `eval`, `sed -i`
   out-of-worktree, an out-of-worktree `>`/`>>` redirect, `rm -rf` on an
   unresolved/uncontained target.

2. **Give the substitute.** To OBSERVE a shell/git behaviour, do not script it —
   WRITE A TEST in the repo harness (durable, re-runnable), or run ONE simple
   command per call with LITERAL relative paths under `.pilot-scratch/` — no
   variable, no `;`/`&&`, no sub-shell. The cpp#272/#279 carves now admit
   `.pilot-scratch/<subpath>` literals, so that substitute is actually available.
   A final guard line (MPC gate condition on cpp#278) closes the substitute block:
   "Never `pip install` on the host (it clobbers the shared launcher); to test, use
   `uv run` in a clone or a throwaway venv." A host `pip install` rewrites the shared
   `[console_scripts]` launcher (`~/.local/bin/claude-pilot`) and breaks the
   production entry point. It is pure hint text — advisory, NOT a
   `_TERMINAL_FORM_REGISTRY` entry (it classifies no terminal form), so it leaves
   admission/lethality and the registry-centric drift guard untouched.

3. **Derive the hint from tier1's own lists — no drift.** A small registry,
   `_TERMINAL_FORM_REGISTRY`, co-located with the lethality tuples, pairs each
   bullet with the EXACT tier1 pattern object the classifier matches on. The five
   `TIER3_PATTERNS` entries the hint names are hoisted to named constants
   (`_BASH_C_PATTERN`, `_SH_C_PATTERN`, `_SED_I_SHORT_PATTERN`, `_RM_RF_PATTERN`,
   `_GENERIC_REDIRECT_PATTERN`; `eval` already was `_EVAL_COMMAND_POSITION_RE`) and
   referenced by the registry. `_render_terminal_forms_block()` builds the block
   from the registry; the hint is that block plus the existing body.

## Why it works / what it guarantees

A drift-guard test (`test_terminal_forms_hint_derived_from_tier1`) asserts, per
registry entry: its bullet is present in the rendered hint, AND its pattern is
really in the lethality set — a verb in `_matches_proven_dangerous_lethality_verb`'s
union, or the one generic-redirect entry deliberately excluded from the verb set
(governed by the cwd-aware destination veto). So a registered terminal form
dropped from the hint, or a pattern that silently leaves the lethality set, turns
the suite red. Both red directions were verified by toggle.

**Admission and lethality are byte-identical.** Hoisting patterns to named
constants leaves `TIER3_PATTERNS` holding the same compiled objects in the same
order, so `is_tier3_dangerous` and `is_tier3_dangerous_for_lethality` are
unchanged; `is_tier1_auto_approve`, the YAML, and egress are untouched. Proven by
a sha256 digest over a 107-row corpus (all tier3 verbs, pilot-scratch/mktemp
carves, in/out-worktree redirects, cpp205 verbs, find/xargs, read-only commands,
compounds, non-Bash tools) that is identical before and after the change.

## Gotchas / limits

- **Prompt measures are stochastic.** This reduces the RATE of the shell-probe
  death; the structural carves (cpp#268/#272/#279) close the class. Keep both.
- **The drift guard is registry-centric.** It forces a hint update when a
  *registered* form drifts, not when an arbitrary new verb is added to tier1
  without a registry entry. That is intentional: the registry is the curated
  subset the concise hint exposes.
- **The hint names a curated subset** of the ~20 lethality verbs — the "probe"
  forms a grooming/implementing pilot actually reaches for, not `git push
  --force`/`DROP TABLE`/`dd`/`mkfs`. Naming all of them would bloat a block that
  is prepended to every prompt.

## See also

- `src/claude_pilot/tier1.py` — `_TERMINAL_FORM_REGISTRY`,
  `_render_terminal_forms_block`, `DENIED_BASH_PATTERNS_HINT`.
- Plan: `docs/plans/2026-10-02-007-fix-278-hint-terminal-forms-plan.md`.
- Founding hint: cpp#59/mika#1409. Lethality doctrine: cpp#128, cpp#205.
  Structural carves this complements: cpp#268/#272/#279.
