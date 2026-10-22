---
title: "A flag guard must also refuse a glob that can spell the flag, at every layer that admits"
date: 2026-10-22
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
symptoms:
  - "A git read admitted by a prefix policy rule could carry an option that makes git write its output to a file"
  - "tier1 refused the literal option, but the policy layer re-admitted it, and tier1 itself admitted an unquoted glob that bash can expand into it"
root_cause: missing_validation
resolution_type: code_fix
related_components: [policy, chain-safe]
tags: [permissions, tier1, policy, chain-safe, git, output, glob, lexer, parity, mutation-testing, tightening]
applies_when: "adding or reviewing a classifier guard that refuses a flag by reading the command text, on any layer that can admit the command"
---

# A flag guard must also refuse a glob that can spell the flag, at every layer that admits

## Problem

A git subcommand admitted as a read must write no file. `git log`, `git show` and `git diff` accept `--output`, which writes their output to a file. tier1 refused the literal option (`_GIT_DENIED_GLOBAL_FLAG_RE`, `src/claude_pilot/tier1.py`), but the policy rule `bash-git-readonly` is a prefix match (`src/claude_pilot/policies/permissions.yaml:323`) and `_bash_allow_is_chain_safe` did not replay that guard, so the end-to-end decision was still allow. The rule is now tightened: no git invocation that may carry `--output` is admitted on either path.

## What Didn't Work

**1. A literal-only veto on the policy path.** The first cut, `git_option_writes_a_file`, refused a quote-removed word equal to `--output` or starting with `--output=`, and was wired only into `_bash_allow_is_chain_safe`. Its own tests were green and every mutation turned them red. An independent adversarial review then found that an unquoted glob whose literal head is still compatible with the option is not the option in the text, but bash can expand it into the option before git reads it. Two blind spots stacked: the predicate never matched such a word, and tier1 admitted it directly, so the new veto was never even consulted. Measured through `create_permission_handler`: tier1 auto-approved each glob spelling.

**2. Refusing any glob in a `-`-led word.** Considered and rejected: `--grep=fix*` cannot become `--output`, and refusing it buys nothing. The test that decides is whether the glob can still spell the flag.

**3. Refusing the option after `--` too.** The first cut kept scanning past `--` "for simplicity". The first glob clause then refused `git diff -- *.py`, a pinned admission in `tests/test_tier1_git_value_hidden.py`. After an unquoted `--` git reads paths, not options; a word there names a file, it writes none. The scan now stops there, as `git_argument_value_hidden` already did.

## Solution

`git_option_writes_a_file(command)` (`src/claude_pilot/tier1.py:3886`), one predicate consulted by both layers:

- `is_safe_bash_command`, right after `git_argument_value_hidden` (`src/claude_pilot/tier1.py:3086`);
- `_bash_allow_is_chain_safe`, right after the same veto, rule-id independent (`src/claude_pilot/permissions.py:560`).

For every word `git` in every segment, it reads the following quote-removed words up to the first unquoted `--`. A word refuses when it is `--output`, starts with `--output=`, or is an unquoted glob whose head, cut at its first glob character, is a prefix of `--output` (`_glob_may_spell_output`, `src/claude_pilot/tier1.py:3871`). A quoted `--` does not end the scan; that is the safe side. An unlexable segment is left to `git_argument_value_hidden`, which already refuses it on both paths.

Probed on git 2.53: these three subcommands accept `--output=F` and `--output F` only. Every abbreviation is rejected by git, so the abbreviation class that `command-string-policy-allow-rules-are-compound-unsafe.md` §6 requires for GNU getopt does not apply here, and the probe, not the assumption, is what settles it.

## Why This Works

A text guard sees the word before bash evaluates it. A word that bash rewrites is guarded by asking what it can become, not what it says. For a glob, the answer is bounded by its literal head: if the head cannot begin the flag, no expansion can produce it. A head longer than the flag is either the literal `--output=…`, already refused, or a word that cannot become the flag. A sub-clause covering it had no mutant to kill and was removed.

The second half is layering. A guard that only refuses on the policy path is never reached when tier1 admits first. tier1 and the policy must consult the same predicate, or the one that admits more decides.

## Prevention

- Measure through `create_permission_handler` with the bundled policy, never through one layer's predicate. Both blind spots above were invisible to the predicate's own tests.
- For every flag a guard refuses, add its glob spellings to the refusal tests: one per glob character, plus a bare `-` followed by a glob.
- Mutate term by term, the call sites included. Here nine mutants turned red: each clause, the `git` word filter, the segment split, the `--` stop, the unquoted-`--` requirement, and both call sites. A comment-then-newline case was needed to kill the segment-split mutant: the lexer stops at `#`, the next line is still a command.
- Replay the real pilot corpus, old against new, through the full handler. Here: 35,925 distinct commands, 0 refused → admitted, 6 admitted → refused. All six were a review diff written to a working file with the literal option. The refusal is non-terminal, and reading the same diff on standard output stays admitted. The glob clause changed no real decision.
- Residuals outside this scope are tracked privately.
