---
title: "Quoted regions are read as bash reads them"
date: 2026-10-22
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
symptoms:
  - "The shared quote scanner opened a quoted region where bash opens no quote"
  - "In comments, after heredoc openers and in expansions inside double quotes, the scanner's regions did not follow bash's reading"
root_cause: missing_validation
resolution_type: code_fix
related_components: [policy, chain-safe, splitter, lethality]
tags: [permissions, tier1, policy, chain-safe, quote-scanner, comment, heredoc, parameter-expansion, parity, mutation-testing, tightening, fail-closed]
applies_when: "adding or reviewing any guard that reads `_quote_spans` regions or `_split_compound_command` segments, or changing the quote scanner"
---

# Quoted regions are read as bash reads them

## Problem

Quoted regions must follow bash's reading: bash opens none in a comment or after a heredoc opener, and rereads in its own way the quotes of a `${…}` expansion inside double quotes. `_quote_spans` opens a region at every unescaped quote character outside a region. The rule is now tightened: a command whose regions open where bash opens no quote is refused on both admission paths, by `split_is_ambiguous` (see `a-splitter-cut-bash-does-not-make-is-refused-not-guessed.md`), before any segment is read.

## What Was Not Obvious

**1. Correcting the scanner is the wrong fix, for a second reason.** As for the splitter, the scanner feeds the lethality walk (through the splitter and `_mask_quoted_redirect_chars`), so changing its regions moves the lethality of existing refusals. It also feeds `contains_unquoted_metacharacter`: a scanner that stopped opening regions in a comment would stop flagging what it finds there, which removes refusals and creates admissions. The scanner stays as it is; `_quote_opens_where_bash_reads_none` names its wrong regions and both paths refuse them.

**2. After a heredoc opener, refuse any quote; do not delimit the body.** Reading the body as bash does means a second parser: the end of the opener's line, the terminator line, `<<-`, several openers queued on one line, an empty delimiter. Each mistake in it is an under-detection. "A quoted region opens after an opener" costs nothing measurable: over the real corpus, neither the broad term nor the exact one changes a decision. The policy path already refuses every heredoc but the sanctioned one, which stays ahead of the check.

**3. A region that ends with its comment does not count.** Refusing every quote in a comment broke a ratified admission: a comment that carries an apostrophe and ends the command. Only a region that opens in the comment and runs past its end is refused. Regions are sorted and disjoint, so only the last one opening before the end of the comment can run past it: one bisection, linear cost on a hostile input.

**4. Read the start of a word as bash does, escapes included.** A `#` after an escaped blank, tab or parenthesis is mid-word for bash, and the predicate now reads it so. An empty heredoc delimiter still opens a heredoc. An independent review found both after the first commit was green and its mutants were red.

**5. Reading more comments than bash must only refuse more.** Reading a comment means skipping to the end of its line, and a skip is not a refusal. A `#` glued to a `)` cannot be told apart from a subshell without a parser, so a supposed comment that contains `<<` with a quote after it is refused as an opener would be. The second review pass found this one.

**6. Inside double quotes, a cut region is not the only divergence.** A `${…}` whose closing brace lies past the end of the double-quoted region shows itself by the depth left open. A single quote inside a closed expansion does not cut anything, yet it shifts which later quote characters pair up. A mutant removing that condition survived until a test with a closed expansion was added. Outside double quotes, bash reads the quotes of an expansion as the scanner does, or more widely, so no term is needed there.

## Solution

`_quote_opens_where_bash_reads_none(command)` in `src/claude_pilot/tier1.py`, consulted by `split_is_ambiguous`:

- a comment (a `#` at the start of a word, not after an escape, outside a region, up to the end of its line) in which a region opens and runs past its end, or which contains `<<` with a region after it;
- a heredoc opener (`<<`, not `<<<`, any delimiter including an empty one), outside a region and a comment, with a region opening after it;
- inside a double-quoted region, a `${…}` expansion holding a single quote, or still open when the region ends.

The refusal is not terminal. No new admission: replayed through `create_permission_handler` over 35,957 distinct pilot commands, `ff98d71` against the final implementation, contained and uncontained, no decision and no lethality changed. The new term fires on 307 of them, all already refused with the same lethality. Each condition was neutralised alone (30 in-memory mutants) and turned the tests red. Through the handler, each of the three forms turns red; the conditions that stay green there (the start of the command, the here-string, the word-start characters other than blanks) cover forms other guards already refuse, and are pinned by direct tests.

## Not Fixed Here

Residuals outside this scope are tracked privately. The opt-in `per_spawn` evaluator does not consult this predicate.

## Prevention

Before trusting a quote boundary from `_quote_spans`, ask whether bash opens a quote there at all, not only where it closes one. When the predicate reads a construct more widely than bash does, check that the wider reading only refuses: a skip is not a refusal.
