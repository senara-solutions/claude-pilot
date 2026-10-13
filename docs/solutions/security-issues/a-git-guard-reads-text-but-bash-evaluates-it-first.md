---
title: "A git guard reads text, but bash evaluates it first — and the policy re-admits what tier1 refuses"
date: 2026-10-12
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
symptoms:
  - "A git command refused when its value is spelled literally is admitted when the same value arrives through an expansion, a hidden assignment, or quote splicing"
  - "Closing the hole in tier1 alone changes no end-to-end decision: the policy layer re-admits the refused command"
root_cause: missing_validation
resolution_type: code_fix
related_components: [policy, chain-safety]
tags: [permissions, tier1, policy, chain-safe, git, push, expansion, quoting, lexer, parity, mutation-testing, admission, tightening, fail-closed]
applies_when: "adding or reviewing any classifier guard that decides on a git (or any) argument by reading the command text"
---

# A git guard reads text, but bash evaluates it first

## Problem

Every git guard in the classifier (`_MAIN_MASTER_RE`, `_FORCE_FLAG_RE`, `_GIT_DENIED_GLOBAL_FLAG_RE`, `_GIT_BRANCH_MUTANT_TOKENS`, `TIER3_PATTERNS`) reads the raw command string. Bash does not hand that string to git: it expands parameters, applies operator expansions, substitutes special parameters, removes quotes and backslashes, and globs. So the value a guard refuses as a literal (a push to `main`, a force flag, `--output=…`, a branch mutant) can reach git without ever appearing in the text the guard read. This is the same family as cpp#157 (a quote-scope exemption inherits the whole grammar) and cpp#211 (a whitespace-anchored disqualifier is defeated by a quoted operand): the classifier models a string, the shell executes a program.

## What was not obvious

**1. Fixing tier1 alone is a no-op end to end.** When tier1 refuses a Bash command, the policy layer gets its turn. `bash-git-push` (`^git\s+push(\s|$)`) and `bash-git-readonly` are prefix rules, and `_bash_allow_is_chain_safe` only re-checks a policy-allowed segment with `is_tier3_dangerous`, which also reads raw text. Measured through the real `create_permission_handler`: every form tier1 newly refused was still ALLOWED by the handler until the same predicate became a veto in `_bash_allow_is_chain_safe`. Always measure a classifier fix through the handler, never through `is_safe_bash_command` alone.

**2. The veto must run after the allowlisted `$(…)` redaction.** On the policy path, sanctioned substitutions (`$(git branch --show-current)`, `$(git merge-base HEAD origin/main)`) are redacted to the literal `_SUB_` before chain-safety runs. A veto placed before that redaction sees a command substitution in a git argument and refuses four real pilot reads from the corpus. A test pins the order (`git diff --name-only $(git merge-base HEAD origin/main)` stays allowed).

**3. A plain `$NAME` is only safe because the hidden-assignment channels are closed.** `git diff $BASE` is a frequent legitimate read (the mika#1686 F1 shape assigns `BASE` in the clear). It stays admitted in READ subcommands only. That is sound only if nothing in the same command can give `NAME` a value the text does not show: `printf -v` (quoted or escaped `printf` still runs the builtin), an assignment operator anywhere inside a `${…}` body (nested `${Z:-${X:=…}}` and array `${A[0]:=…}` included), and `$_` (the previous command's last argument). Those are refused command-wide. Plain names in write subcommands are refused outright.

**4. Normalise what git resolves, not what the text says.** git DWIMs a push destination: `heads/main` and `refs/heads/main` both update `refs/heads/main`. A destination check that strips only `refs/heads/` misses `HEAD:heads/main`. Three independent reviewers found it, and it was confirmed against a scratch bare repo. Compare the destination after stripping `refs/` and then `heads/`.

**5. Real-corpus parity catches over-refusal the tests cannot.** Replaying the change over 24,976 unique Bash commands from pilot transcripts (old vs new, real handler, bundled policy, contained and uncontained) surfaced two legitimate idioms the first cut refused:
- `git stash push -u -m "<tag>-$$"`, the uniqueness idiom used by 3 pilots. `$$` is the PID: digits the pilot does not choose. It is the one special parameter tolerated.
- `@{u}`, `@{upstream}` and `^{tree}`: 13 commands across 10 pilots, mistaken for brace expansion. Bash only expands `{…}` that contain `,` or `..`.

After both corrections the replay showed 0 decision changes. Of the new refusals, none occurs in the corpus.

**6. Mutate term by term and delete what survives.** Each clause and each call site was neutralised alone and had to turn the dedicated tests red. Adding the "quoted word whose value is a guarded flag" clause upstream made two earlier guards redundant (force on the push target; re-running the raw guards on quote-removed text in `is_safe_git_command`). Their mutants survived, and the guards were removed rather than kept "for safety", which also returned two functions to their pre-change form. One survivor is an equivalent mutant: an unlexable segment without `${` is an unterminated quote, which `_split_compound_command` always extends to the end of the command, so it is always the last segment.

## Solution

`git_argument_value_hidden(command)` in `src/claude_pilot/tier1.py`: one predicate, consulted at the top of `is_safe_bash_command` (after the anchored ce-work preamble, before the read-only compound path) and as a veto in `permissions._bash_allow_is_chain_safe`. It only ever refuses, and the refusal stays non-terminal (lethality unchanged; `git push origin main` stays terminal through `TIER3_PATTERNS`). A small lexer (`_lex_shell_words`) records, per word, the quote-removed text, the expansions outside single quotes, and unquoted glob or brace-expansion characters. It fails closed when it cannot follow the input.

Clauses: (a) hidden assignment anywhere in the command; (b) any expansion in a git argument other than a plain `$NAME`/`${NAME}`; (c) any expansion in a write subcommand, or after a global option whose arity the finder does not model; (d) push target judged on the quote-removed words (glob, `--all`/`--mirror`, `+` refspec, destination resolving to main/master); (e) a quoted or escaped spelling of a guarded flag, a brace expansion, or a leading glob before `--`.

## Not fixed here

The policy layer also admits **literal** git options that tier1 refuses (`--upload-pack=`, `--output=`, `branch -m`, `worktree remove --force`), because `bash-git-readonly` is a prefix match and chain-safety never replays tier1's flag guards. That is a different class (nothing is hidden from the text) and needs its own ticket. Residuals: environment variables inherited from the dispatcher and read through a plain `$NAME` in a read subcommand, and the opt-in `per_spawn` evaluator, which does not consult this predicate.

## Prevention

When a guard keys on a value, ask what bash will have done to the text before the program sees it, and test that guard through `create_permission_handler` with the bundled policy, not through the tier1 predicate alone.
