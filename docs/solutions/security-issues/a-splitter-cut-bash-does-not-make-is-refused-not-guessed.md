---
title: "A splitter cut bash does not make is refused, not guessed"
date: 2026-10-22
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
symptoms:
  - "The compound splitter shared by tier1 and the policy cut a command where bash does not end the statement"
  - "Text after such a cut was judged as a statement of its own instead of as part of the command it belongs to"
root_cause: missing_validation
resolution_type: code_fix
related_components: [policy, chain-safe, lethality]
tags: [permissions, tier1, policy, chain-safe, splitter, line-continuation, carriage-return, escaped-separator, parity, mutation-testing, tightening, fail-closed]
applies_when: "adding or reviewing any classifier guard that reads `_split_compound_command` segments, or changing the splitter itself"
---

# A splitter cut bash does not make is refused, not guessed

## Problem

`_split_compound_command` cuts on a bare carriage return, on a line continuation and on an escaped separator (`\;`, `\|`, `\&`). Bash ends no statement at any of them. Every admission guard of tier1 and of the policy chain check reads those segments, so text that bash hands to one command was judged as if it were a command of its own. The rule is now tightened: a command whose split may differ from bash's is refused on both admission paths before any segment is read.

## What Was Not Obvious

**1. Making the splitter bash-exact is the wrong fix.** The denial lethality (`_denial_is_terminal`, the destination veto) walks the same segments. Changing where the splitter cuts moved the lethality of existing refusals both ways: a refusal that was rightly terminal became survivable, and one that was survivable became terminal. Not cutting on an escaped separator also admitted commands the old split refused, because their guards had only ever seen the old segments. The splitter stays as it is. `split_is_ambiguous` names the commands it cuts differently, and both paths refuse them. Admissions get the guarantee; the lethality walk is untouched.

**2. Joining a continuation is not what the guards read.** Bash deletes a backslash-newline, unquoted and inside double quotes, and rebuilds one word from the two lines. The guards read the raw text, where `shlex` keeps the newline inside the token, so the word a guard judges is not the word bash runs. Joining would need every guard to see the rebuilt word. Refusing costs nothing measurable: of the 27 real commands with a continuation, none lost its decision.

**3. A cut that moves nothing is not ambiguous.** A refusal of every escaped separator and every continuation broke two ratified admissions: `find … -exec … {} \;` (cpp#33) and the cpp#151 read-only `for` loop, whose pipeline continues with `\` then a line opening with `|`. In both, only blanks follow the cut up to a real separator or the end. The splitter's statement boundary is bash's, and no text changes segment. The predicate refuses a cut only when text of the same statement follows it. A redirection such as `&>` counts as text, not as the end of the statement.

**4. Both call sites, before every whole-command shape.** `is_safe_bash_command` checks first, ahead of the ce-work preamble, the contained read-only compound and the review scope probe. `_bash_allow_is_chain_safe` checks right after the heredoc block, ahead of the sanctioned shapes that return `True` without a segment loop. The sanctioned heredoc stays before it: it is a whole-command proof that never consults the split, and its body is data bash does not join.

**5. A deleted continuation can also build an operator.** Right after `<` or `>`, the character that follows a continuation joins it into one operator, so "only blanks, then a separator" is not enough there; the predicate refuses that position whatever follows. An independent review found it after the first cut's tests and mutants were green.

**6. A divergence other guards already refuse earns no term.** Each term of the predicate has a handler-level test that turns red when it alone is removed; a form already refused on both paths by another guard is left out rather than kept "for safety", the same rule `a-read-rule-names-its-read-forms-not-its-subcommands.md` applied to its own dead entries.

## Solution

`split_is_ambiguous(command)` in `src/claude_pilot/tier1.py`, with `_quote_spans` for quoting and the same atomic backslash pairing:

- a carriage return, anywhere;
- outside quotes, a line continuation or an escaped separator followed by text of the same statement (`_text_follows_in_statement`), or right after `<` or `>`;
- inside double quotes, a line continuation.

Called at the top of `is_safe_bash_command` and in `permissions._bash_allow_is_chain_safe`. The refusal is not terminal. No new admission: replayed through `create_permission_handler` over 35,925 distinct pilot commands, old against new, contained and uncontained, no decision and no lethality changed. Each term was neutralised alone (20 in-memory mutants) and turned `tests/test_split_matches_bash.py` red.

## Not Fixed Here

Residuals outside this scope are tracked privately. The opt-in `per_spawn` evaluator does not consult this predicate.

## Prevention

Before a guard trusts a segment, ask whether bash ends the statement where the splitter did. When a shared helper is wrong for admission but other consumers depend on its output, refuse the inputs it gets wrong at the admission entry points, and leave the helper alone.
