---
title: "A quote-scope exemption inherits the whole grammar — escapes included, and that is where it leaks"
date: 2026-09-05
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, redirect, lethality, session-fatality, quoting, posix, escape, lexical-matching, fail-closed, negative-control, anti-vacuity, claude-pilot-130, claude-pilot-154, claude-pilot-157]
applies_when: "adding a quote-aware or scope-aware exemption to a security classifier, or writing a hand-rolled lexer that decides whether a gate applies"
---

# A quote-scope exemption inherits the whole grammar — escapes included

## Context

`claude-pilot` keeps two decisions apart: `is_tier3_dangerous` decides the
**refusal**, `is_tier3_dangerous_for_lethality` decides whether that refusal
**ends the run** (see
[a-refusal-and-its-lethality-are-two-decisions-not-one](../tooling-decisions/a-refusal-and-its-lethality-are-two-decisions-not-one.md)).

cpp#157: the generic `>` entry of `TIER3_PATTERNS` is quote-blind, so it read the
`>` in `sed 's/=.*/=<set>/'` — ordinary text to bash, the replacement half of a
substitution — as a redirection. That one segment carried the lethality alone,
and it killed the pilot of mika#2179. The fix masks `<` and `>` inside quoted
regions before the patterns run.

## The trap

Writing the scanner, quotes are the obvious layer: `'` opens, `'` closes, `"`
opens, `\"` inside does not close. All of that was handled. What was not handled
is the layer that GOVERNS quotes — the backslash **outside** them.

```
echo \' > /etc/passwd \'
```

Bash reads `\'` as a literal apostrophe. It opens nothing. The `>` is a real
redirect to `/etc/passwd`. The scanner, seeing two bare `'`, opened a region at
the first, closed it at the second, and masked the redirect between them:

| command | main | mask, no escape handling | mask + escape handling |
|---|---|---|---|
| `echo \' > /etc/passwd \'` | True | **False** | True |
| `echo \" > /etc/passwd \"` | True | **False** | True |

The refusal was never in question — that command stays denied and no byte is
written. What leaked was the **lethality**: a genuine write outside the worktree
became survivable, the exact class the ticket's own AC2 forbids. The fix
introduced the hole; `main` did not have it.

## The learning

**A scope-based exemption is a claim about a grammar, and you own every layer of
that grammar down to the one that decides where your scope begins.** Quote scope
in POSIX is not decided by quote characters. It is decided by quote characters
*that the escape layer has not already consumed*. Stop one layer short and the
exemption fires on a scope the shell never had.

Two properties keep the residue survivable, and both are worth copying:

1. **Pick the fail-closed direction from what the function DECIDES, not from what
   its neighbours do.** The two pre-existing quote scanners in this module treat
   an unterminated quote's remainder as *inside* the quote, because they decide
   an **allowance** and their conservatism is "refuse". This one decides a
   **lethality**, so its conservatism is "do not exempt" — an unterminated quote
   returns the command unchanged, i.e. still lethal. Same principle, opposite
   direction, and a reviewer who assumes symmetry will call one of them a bug.
2. **Consume the escaped character; never mask it.** `\>` is a literal `>` to
   bash, so masking it would be *more* correct lexically — and would widen the
   exemption. Leaving it visible to the pattern keeps a class lethal that maybe
   need not be. That is the right side to be wrong on.

## How it was caught, and the cheaper way to catch it

Not by a test — by an adversarial probe run against the finished code, asking one
question: *can I construct a command where the mask fires on a scope bash does
not have?* Five minutes, ten candidate strings, one hit.

**A scope exemption deserves that probe before it merges, every time.** Unit
tests written by the author of a lexer test the grammar the author had in mind;
they cannot test the layer the author forgot. The probe attacks the claim
instead of the implementation.

## Measure the control before you assert it

The regression test for the finding above first asserted `echo a \> b` stays
lethal. It does not — and not because of this mask: cpp#154 already strips a
*contained* relative target, so that command measures `False` on `main`. The
assertion would have passed for the wrong reason and pinned the wrong mechanism.
Re-aimed at `/etc/passwd`, measured `True` before and after.

That is the **third** premise this one ticket lost to measurement — after a
diagnosis of "lethality by chain aggregation" that the grooming checkpoint
refuted, and two negative-control examples in the body that cpp#154 had already
made non-lethal the day before. A fourth premise, the plan's assumption that the
module's quote scanners agree on every boundary, was refuted before a line was
written: `_split_compound_command` and `contains_unquoted_metacharacter` already
disagree on `main` about `echo "a\\"`.

**The pattern across all four is one habit: a control was chosen from the code as
remembered, not as measured.** Every negative control in a security test needs
its baseline verdict measured on the actual base commit — not derived from the
patch notes, not inherited from yesterday's plan. A control that is already the
colour you expect proves nothing, and it is indistinguishable from a control that
works until someone runs it.

## Where the debt is

The module now carries three independent POSIX quote scanners. They are not
merged here — two sit on the ALLOW path, and a p1 lethality fix does not widen
its surface onto that path. `TestQuoteScannerBoundaryParity` pins where each one
places a boundary today, both known divergences named in the test body, so a
future change to any one of them cannot shift a boundary silently. Extraction of
a shared `_quote_spans()` is filed as follow-up.

## References

- `src/claude_pilot/tier1.py` — `_mask_quoted_redirect_chars`, and the two
  scanners it deliberately does not merge with.
- `src/claude_pilot/permissions.py` — `_redirect_destination_veto_reason`, which
  takes the same mask so one notion of "where the redirects are" exists.
- `tests/test_tier1.py` — `TestTier3QuotedRedirectCharLethality`,
  `TestQuoteScannerBoundaryParity`.
- cpp#157 (this fix), cpp#154, cpp#130, cpp#151, cpp#143.
