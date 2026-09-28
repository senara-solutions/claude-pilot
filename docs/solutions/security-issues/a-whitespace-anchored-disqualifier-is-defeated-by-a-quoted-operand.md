---
title: "A whitespace-anchored disqualifier is defeated by a quoted operand — the quote sits between the space and the metacharacter"
date: 2026-09-28
module: claude_pilot.permissions
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, cp, mv, quoting, lookahead, containment, destination-veto, admission, tightening, escape, fail-closed, negative-control, claude-pilot-154, claude-pilot-176, claude-pilot-201, claude-pilot-209, claude-pilot-211]
applies_when: "writing a disqualifier or containment check that keys on a metacharacter reached across an operand boundary the shell also lets a quote fill"
---

# A whitespace-anchored disqualifier is defeated by a quoted operand

## Context

`claude-pilot` admits a `cp`/`mv` call through the `bash-cp-mv` policy rule, then
`_destination_veto_reason` is a second, cwd/fs-aware layer that vetoes a
destination resolving outside the worktree. The two are meant to be
defense-in-depth: the rule refuses a rooted destination syntactically, the veto
catches what the rule misses.

The rule disqualifies a `$`/`~`/`/`-rooted destination with three
whole-remainder negative lookaheads of the form `(?!.*\s\$)` — "reject if, after
a space, the next thing is `$`." Bare, this works: `cp x $HOME/y` is refused.

## The trap

The lookahead is anchored on **whitespace immediately followed by the
metacharacter**. But an operand boundary in the shell is not only whitespace —
it is whitespace *and then an optional quote the shell will strip*:

```
cp secret "$HOME/exfil"
```

The character after the space is `"`, not `$`. `\s\$` does not match, the
disqualifier does not fire, and the rule ADMITS the call. The same
out-of-worktree destination that is refused bare is admitted quoted — refractive
of the exact hole in
[a-quote-scope-exemption-inherits-the-whole-grammar-escapes-included](a-quote-scope-exemption-inherits-the-whole-grammar-escapes-included.md):
a matcher keyed on a delimiter owns every form of that delimiter, quotes
included.

The second layer failed the same input for a mirrored reason.
`_destination_veto_reason` shlex-strips the quotes, then hands the literal
`$HOME` (no leading `/`) to `is_within_project`, which reads it as a same-named
relative subdir *inside* the worktree and returns veto=None. Both layers,
independently, treated the quoted form as contained.

| command | bare | quoted (HEAD) | quoted (fixed) |
|---|---|---|---|
| `cp x $HOME/y` | deny | deny | deny |
| `cp x "$HOME/y"` | — | **allow, veto=None** | deny + veto |
| `cp x '$HOME/y'` | — | **allow** | deny + veto |
| `cp x "subdir/y"` (contained) | allow | allow | allow (unchanged) |
| `cp x "/abs/y"` | veto | veto | veto (unchanged) |

The sandbox (bwrap `--unshare-net`, tmpfs `/home`) bounds the blast radius, so
this was defense-in-depth, not a live exfiltration. But defense-in-depth only
counts when each layer holds alone — a layer that admits what its neighbor
refuses is an aligned hole, not depth.

## The learning

**A disqualifier keyed on a metacharacter reached across an operand boundary
must account for the quote the shell allows at that boundary — on every layer
that shares the assumption.** Two edits, mirrored:

- the lookaheads accept an optional opening quote between the boundary space and
  the metacharacter: `(?!.*\s["']?\$)` (and `~`, `/`);
- the veto adds a lexical disqualifier for a destination that, after
  quote-stripping, `startswith("$")` or `startswith("~")` — treated as NOT
  contained, consistent with the cpp#154 D3 anti-`~`-respelling doctrine already
  applied on the lethality side.

This is a TIGHTENING: only the quoted `$`/`~`-rooted class flips
admitted→refused. A contained quoted destination (`"subdir/y"`, `"a b"`) stays
admitted — the both-directions negative controls (cpp#176/#178) are what prove
the tightening did not widen. Note the fix deliberately does NOT reuse the full
lexical-disqualifier predicate, whose charset/`..` sub-rules would over-tighten a
currently-admitted contained destination; it reuses only the `$`/`~` leading-root
expression. Leading `/` stays a containment candidate so cpp#176 (absolute path
inside the worktree) survives.

Consequence, intended: a `$HOME`/`~`-rooted `cp`/`mv` refusal is now TERMINAL,
matching the cpp#154 D3 invariant and the redirect side. On HEAD it was
accidentally survivable because containment misread it — no test asserted that
accident.

## The residue (known, out of scope for cpp#211)

The `bash-mkdir` rule carries the identical whitespace-anchored lookahead and the
same quoted-`$`/`~` hole for `mkdir` destinations. cpp#211 is scoped to `cp`/`mv`
by ratification; the `mkdir` sibling is a candidate follow-up tightening — the
one direction the doctrine never debates.

## References

- Sibling: [a-quote-scope-exemption-inherits-the-whole-grammar-escapes-included](a-quote-scope-exemption-inherits-the-whole-grammar-escapes-included.md).
- Arc: [a-syntactic-classifier-cannot-prove-a-target-is-safe-prove-it-outside](../tooling-decisions/a-syntactic-classifier-cannot-prove-a-target-is-safe-prove-it-outside.md).
- Tickets: cpp#211 (this fix), cpp#154 D3, cpp#176 (both-directions), cpp#201/#209 (mktemp lethality carve, preserved).
