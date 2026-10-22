---
title: "A read rule names its read forms, not its subcommands"
date: 2026-10-22
last_updated: 2026-10-22
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
symptoms:
  - "A policy rule meant for git reads admitted its subcommands by prefix, whatever options and arguments followed"
  - "A guard against options that make git run a program lived only on the tier1 path, and only matched part of what git accepts"
root_cause: missing_validation
resolution_type: code_fix
related_components: [policy, chain-safe]
tags: [permissions, tier1, policy, chain-safe, git, read-only, closed-list, parse-options, lexer, parity, mutation-testing, tightening]
applies_when: "adding or reviewing a policy rule that admits a command family as read-only, or a guard that refuses an option by name"
---

# A read rule names its read forms, not its subcommands

## Problem

`bash-git-readonly` admitted ten git subcommands as reads. Its pattern stopped at the subcommand name, and `_bash_allow_is_chain_safe` did not replay tier1's option guards on a policy allow. A subcommand that has a read mode is not a read: most of these also have modes that write a ref, the config or a worktree, run a program, or read outside the repository. The rule is now tightened: it admits only reads.

## What Was Not Obvious

**1. "Read-only subcommand" is the wrong unit.** Of the ten, only `status`, `rev-parse` and `merge-base` are reads whatever their options. `log`, `diff` and `show` are reads except for a handful of options. `branch`, `worktree`, `fetch` and `ls-remote` have a small read mode inside a larger write or transport surface. The admitted unit has to be the form, per subcommand.

**2. Where the read mode is small, list it; where it is the whole command, list the exits.** For `branch`, `worktree`, `fetch` and `ls-remote` the guard is a closed list of admitted options and argument shapes. For `log`, `diff` and `show`, a closed list of their display options would run to hundreds of entries and drift with git, while only two options were found to leave reading; those are refused by name, along with any unquoted glob that can still spell them. An exit is not always an option: `diff` also leaves the repository when given a path outside it, with or without `--`, so such a path is refused too. `status`, `rev-parse` and `merge-base` have no entry: mutation testing showed their entry changed no decision, because git itself rejects the refused options there.

**3. Probe the option parser, do not assume it.** git does not parse every subcommand's options the same way. Each refused option was probed on git 2.53 in throwaway repositories, and the guard matches what git accepts for that option on that subcommand, not only the literal name.

**4. The guard that refuses a program must live on both layers.** tier1 admits git invocations directly, so a guard placed only on the policy path is never reached for what tier1 lets through. `git_option_names_a_program` is consulted by `is_safe_bash_command` and by `_bash_allow_is_chain_safe`, for every git invocation, rule-id independent. This is the same lesson as `a-flag-guard-must-also-refuse-a-glob-that-can-spell-the-flag.md`, met again.

**5. `--` ends the options only where git says so.** Stopping at the first unquoted `--` is right for `log`, `diff` and `show`. On a transport subcommand the guard reads the whole invocation. An independent adversarial review asked for this after the first cut's own tests and mutants were green: green tests and dead mutants measure the guard against its author's model, not against git.

**6. Judge the word bash hands to git, not the text the pattern read.** The closed list refuses a quoted or escaped subcommand word outright: what the pattern matched and what git receives must be the same word.

**7. A closed list only bites where it is consulted.** The segment loop of `_bash_allow_is_chain_safe` accepts a tier1-safe segment before consulting the policy, so a list consulted on the policy path alone decides only the segments tier1 refuses. Its tests run through the handler where that is the case, and against the policy layer alone (tier1 refusing every segment) for the rest, so the rule's own contract is pinned independently of what tier1 admits. tier1 now consults the same list too (item 8).

**8. Consult it once, at the entry every path passes, on the segments bash runs.** tier1 admits git through several paths, each with its own matcher. The first plan put the veto inside two of those matchers. Review showed that was not enough: a matcher is an admission filter, and the pieces it cuts the command into are not guaranteed to be the words git receives. The veto now sits once at the entry of `is_safe_bash_command`, next to `git_option_names_a_program`, and judges every segment from the quote-aware splitter. Every tier1 path that judges a git segment comes after it. The predicate is reused as is: tier1 and the policy consult one list, and neither can drift from the other.

## Solution

In `src/claude_pilot/tier1.py`:

- `git_option_names_a_program(command)`: for every word `git`, the following quote-removed words up to an unquoted `--`, refused when one may spell an option whose value git runs as a program. Wired into `is_safe_bash_command` and `permissions._bash_allow_is_chain_safe`, right after `git_argument_value_hidden`.
- `git_read_overreaches(segment)`: the closed list of read forms (`_GIT_READ_FORMS`), judged on the arguments git receives after the segment's redirects are blanked. Consulted for every segment the policy allows, keyed on the segment's subcommand rather than on the rule id, and mirrored in `_segment_refusal_cause` so the refusal diagnosis names the segment.

In `permissions.yaml`, the rule takes the subcommand as a whole word.

Follow-up, same day: `is_safe_bash_command` refuses a command when `git_read_overreaches` is true for any segment from `_split_compound_command`. tier1 now admits only real git reads, both outside and under containment.

No new admission. Lethality unchanged: neither predicate is consulted by `_denial_is_terminal`.

## Verification

- 55 mutants, one per clause, table entry, call site and the pattern's word boundary, each turned the dedicated tests red. Two controls (an identity mutant, a missing anchor) behaved as expected. Python mutants were applied in memory, not on disk. Three surviving terms were removed rather than kept: two clauses of the path check that no input could reach, and the table entries of the three subcommands that are reads whatever their options.
- Real-corpus parity through the full handler, old against new, contained and uncontained: no command went from refused to admitted. The few that went from admitted to refused were forced ref or worktree writes, not reads.
- Follow-up (tier1): the same parity on 35 925 distinct commands found 0 flips from refused to admitted. It found 11 flips from admitted to refused, identical in and out of containment, all non-terminal, none a read:
  - 2 local branch creations.
  - 4 fetches with an explicit destination under `refs/remotes/`. The substitute is a plain fetch, then `origin/<branch>` or `FETCH_HEAD`.
  - 5 `worktree add` of a witness tree at the base, used to run tests there. This is an ordinary pilot gesture. It was weighed and accepted: creating a working tree writes to the repository's administration and is not a read. The refusal does not end the session.
  
  Red-before was measured term by term by in-memory mutation, starting from the green commit. The call site and the per-segment loop each turn the tests red. One equivalent term, a fallback the splitter can never reach, was removed.

## Prevention

- When a rule admits a command "as a read", write down the read forms, then probe each excluded mode in a throwaway repository before refusing it, and keep only what the probe confirms.
- Test the rule both through the handler and on the policy layer alone: the first shows end-to-end decisions, the second pins the rule's contract when another layer happens to decide first.
- When a guard must hold on every admission path, place it at the one entry they share, and have it judge the segments bash will run. Do not place it inside each path's own matcher.
- Residuals outside this scope are tracked privately.
