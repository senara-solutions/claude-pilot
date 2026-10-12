---
title: "Admitting a variable assignment means restricting every reader of it"
date: 2026-10-12
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: critical
symptoms:
  - "A legitimate read-only probe (the ce-code-review scope step `BASE=$(git merge-base …) || BASE=origin/main; git diff --name-only $BASE …`) was refused in 7 distinct pilots between 2026-09-28 and 2026-10-02"
  - "The first admission draft let `BASE=main; git symbolic-ref HEAD refs/heads/$BASE; git commit -am x; git push origin HEAD` through tier1 — reproduced in real git, feature work landed on origin/main"
root_cause: missing_validation
resolution_type: code_fix
tags: [permissions, tier1, bash, admission, variable-assignment, readers, literal-guard, symbolic-ref, push-to-main, chain-safe, substitution-allowlist, placeholder, mutation-testing, corpus-replay, mika-1686]
applies_when: "a classifier change admits a shell variable assignment (or any construct that lets the pilot SET a variable), in a gate whose guards match literal text"
---

# Admitting a variable assignment means restricting every reader of it

## Problem

mika#1686 woke on a verbatim replay: the ce-code-review scope probe was refused at admission in 7 pilots in one week, and its only refused segment was the assignment `BASE=$(git merge-base HEAD origin/main …)` / `BASE="origin/main"`. Admitting that one segment looked inert — a shell variable, no I/O. It is not, in this classifier.

## Symptoms

- The probe cost a turn and a rewrite in every ce-code-review step (pilot task ids 2bd6fca0, 39ae2723, 9c880b93, b8f2b4b6 among others).
- The first cut of the fix, in a multi-reviewer code review with an independent validator (2026-10-12), admitted a push to main through `git symbolic-ref` (P0, confirmed with a real `git` replay against a bare origin).

## What Didn't Work

- **"An assignment is inert."** On `main` 1581d74, tier1 already admits `git push origin $BASE`, `git checkout $BASE`, `git tag $BASE` and `git worktree add $BASE`. The push guard is literal text (`_MAIN_MASTER_RE`, `src/claude_pilot/tier1.py:3499` on this branch) and cannot see through a variable: `git push origin main` is refused, `git push origin $X` is not. That guard was written when a pilot could not set a variable at all. Admitting `BASE=main` closes the loop: `BASE=main; git push origin $BASE`.
- **A per-segment predicate.** The policy chain-safe path checks one segment at a time (`permissions._bash_allow_is_chain_safe`, `src/claude_pilot/permissions.py:488`, per-segment loop at `:647`). A `BASE=` predicate in `_is_safe_sub_command` lets `git status; BASE=main; git push origin $BASE` through: every segment passes alone.
- **Reusing the policy path's `_SUB_` placeholder.** `_redact_allowlisted_substitutions` turns every allowlisted substitution (`$(pwd)`, `$(date +%F)`, `$(git rev-parse HEAD)`) into the same `_SUB_` (`src/claude_pilot/permissions.py:250`), so a predicate keyed on `BASE=_SUB_` silently widens past merge-base.
- **"Read-only git, minus `branch`."** The existing `SAFE_GIT_READONLY_SUBCOMMANDS` (`src/claude_pilot/tier1.py:3597`) includes `symbolic-ref` (`:3599`), which WRITES HEAD when handed a ref. A denylist carved out of a set built for another purpose inherits its blind spots.
- **Checking only `$BASE` reads.** `printf -v BASE %s --output=/tmp/a` rewrites the vetted value; `${BASE:+--output=/tmp/a}` injects an option through an expansion operator; `printf -v X %s $BASE` copies it into an unchecked variable. All three were admitted by the reader-only check.

## Solution

`is_ce_review_scope_probe` (`src/claude_pilot/tier1.py:3041`, called from `is_safe_bash_command` at `:3090`) admits the probe as a **whole-command shape**:

- The exact name `BASE`, assigned from one of the 8 allowlisted merge-base tokens (rewritten to the dedicated `_MERGE_BASE_` placeholder, `:2977`, only at an unquoted statement start) or a literal ref.
- At least one other statement in the same call, so a bare `BASE=…` segment never passes wherever it is checked alone.
- Every statement that reads BASE is in an explicit allowlist, `_BASE_READER_GIT_SUBCOMMANDS` = diff, log, show, rev-parse, ls-files, merge-base (`:3000`), or an echo/printf other than `printf -v`, and uses only the plain `$BASE` / `${BASE}` expansion (`_BASE_NONPLAIN_EXPANSION_RE`, `:2991`).
- No statement writes BASE (`_var_has_nonread_occurrence(sub, "BASE")`, `:3058`).
- An escaped separator (`\;`, backslash-newline) disqualifies the shape (`_BASE_ESCAPED_SEPARATOR_RE`, `:2994`): bash does not split there, the classifier's splitter does.
- An inherited `BASE` in the environment disqualifies it.

## Why This Works

The safety of an admitted assignment is a property of its **readers**, not of the assignment. Restricting readers to an allowlist of commands that cannot act on a ref, and refusing every other way to rewrite or splice the value, makes the variable's content irrelevant to what the command can do. Keeping the predicate whole-command is what makes the reader restriction enforceable: the per-segment path never sees the assignment and its readers together.

## Prevention

- Before admitting anything that sets a variable, grep which already-admitted commands accept a `$VAR` argument whose literal form is refused (`git push origin main` refused, `git push origin $X` admitted). Each is a laundering path the new admission opens.
- Prove each condition with a term-by-term mutation that turns a named negative red (19 mutations, measured 2026-10-12, `tests/test_tier1.py` `TestMika1686BaseScopeProbe` and `test_base_reader_outside_the_allowlist_is_refused`). Two conditions only went red once a negative was placed next to a valid assignment (`BASE=main; PATH=bin; git log $BASE`); a negative the fast path short-circuits proves nothing.
- Gate on the real corpus: replay every Bash command from `~/.mika/data/pilot-transcripts/` through old and new handler code (measured 2026-10-12: 26,176 commands, only the 21 probe-shaped commands flipped, all deny→allow), and execute the admitted ones in a scratch repo with a bare origin (0 ref/HEAD/index/origin changes).
- Pre-existing, not closed here: `printf -v X %s main; git push origin $X` and `git diff ${HOME:+--output=/tmp/a}` are tier1-admitted on `main` for any variable name. The BASE predicate refuses its own versions; the general path needs its own ticket.

## Related

- `docs/solutions/security-issues/ce-scratch-admission-must-be-last-wins-not-any-assignment.md` — same family (same-command variable admission); there the risk was the VALUE, here it is the READERS.
- `docs/solutions/security-issues/command-string-policy-allow-rules-are-compound-unsafe.md` — why the per-segment rule exists, and why a whole-command shape is an exception that needs its own proof.
- `docs/solutions/security-issues/re-admitting-the-ce-tmp-scratch-a-uid-only-substitution-whitelist.md` — the closed-world substitution token discipline the merge-base hoist reuses.
