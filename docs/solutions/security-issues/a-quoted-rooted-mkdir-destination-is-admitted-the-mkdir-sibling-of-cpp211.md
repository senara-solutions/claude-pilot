---
title: "A quoted $/~-rooted mkdir destination is admitted — the mkdir sibling of cpp#211"
date: 2026-09-28
module: claude_pilot.permissions
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, mkdir, quoting, lookahead, containment, destination-veto, admission, tightening, escape, fail-closed, negative-control, claude-pilot-154, claude-pilot-176, claude-pilot-211, claude-pilot-218]
applies_when: "a disqualifier or containment check keys on a metacharacter reached across an operand boundary the shell also lets a quote fill — and the write verb takes its destination as the FIRST operand"
---

# A quoted $/~-rooted mkdir destination is admitted

This is the `mkdir` instance of the class first fixed for `cp`/`mv` in
[a-whitespace-anchored-disqualifier-is-defeated-by-a-quoted-operand](a-whitespace-anchored-disqualifier-is-defeated-by-a-quoted-operand.md)
(cpp#211). That doc named the residue explicitly: the `bash-mkdir` rule carried
the identical whitespace-anchored lookahead and the same quoted-`$`/`~` hole.
cpp#218 closes it. Read cpp#211 first — the reasoning is the same and is not
repeated here.

## The bug

The `bash-mkdir` policy rule disqualifies a `$`/`~`/`/`-rooted destination with
whole-remainder negative lookaheads (`(?!.*\s\$)` …) anchored on **whitespace
immediately followed by the metacharacter**. A quoted operand puts the opening
quote after the space, so `\s\$` never matches:

```
mkdir $HOME/x       -> denied   (leading anchor (?!\$) fires)
mkdir "$HOME/x"     -> ADMITTED (bug: quote sits between space and $)
mkdir '$HOME/x'     -> ADMITTED (bug)
mkdir "~/x"         -> ADMITTED (bug)
```

A `mkdir` outside the worktree is a containment escape (bounded by the bwrap
sandbox, so defense-in-depth, not live).

## The one difference from cpp#211: the destination is the FIRST operand

For `cp`/`mv` the destination is a LATER operand — it always has a space before
it — so cpp#211's quote-insensitive whole-remainder lookahead
(`(?!.*\s["']?\$)`) fully closes the policy layer. For `mkdir` the destination
is the FIRST (and often only) operand. A single-operand `mkdir "$HOME/x"` has no
internal space for a whole-remainder lookahead to anchor on, and the LEADING
anchor `(?!\$)` is quote-blind. So the whole-remainder edit alone closes only
the multi-operand form (`mkdir a "$HOME/x"`); the single-operand form is closed
by the second layer.

## The fix — mirror cpp#211, both layers

1. **`bash-mkdir` YAML rule:** make each whole-remainder disqualifier
   quote-insensitive (`(?!.*\s["']?\$)`, `~`, `/`), identically to cpp#211. This
   denies a rooted second-or-later operand at the policy layer.
2. **`_destination_veto_reason`:** `mkdir` destinations already flow through this
   veto (`_segment_write_kind == "bash-mkdir"`, `_extract_mkdir_destinations`
   shlex-strips quotes). Transpose cpp#211's `$`/`~`-leading-root disqualifier to
   `kind == "bash-mkdir"` — the SAME expression (`dest.startswith("$")` /
   `startswith("~")`), reused verbatim, NOT the full lexical predicate whose
   charset/`..` rules would over-tighten a contained quoted destination. This is
   the layer that closes the single-operand `mkdir "$HOME/x"`, for both the
   REFUSAL and the LETHALITY question — so a `$HOME`/`~`-rooted `mkdir` is
   TERMINAL, matching the cpp#154 D3 invariant.

| command | HEAD | fixed |
|---|---|---|
| `mkdir $HOME/x` | deny | deny |
| `mkdir "$HOME/x"` | **allow** | deny (veto) |
| `mkdir a "$HOME/x"` | **allow** | deny (policy + veto) |
| `mkdir "~/x"` | **allow** | deny (veto) |
| `mkdir "subdir/x"` | allow | allow (unchanged) |
| `mkdir "./build"` | allow | allow (unchanged) |
| `mkdir "/abs/x"` | deny (veto) | deny (veto, unchanged) |

## The learning

**A disqualifier keyed on a metacharacter reached across an operand boundary
must account for the quote the shell allows there — and where the destination is
the FIRST operand, the leading position is a WRITE target, so the runtime
containment veto (not just the whole-remainder lookahead) is what actually
closes it.** cpp#211 could leave `cp`/`mv`'s leading anchor alone because that
operand is a source (a read); `mkdir`'s first operand is a destination, so the
veto's `$`/`~`-leading-root disqualifier does the load-bearing work.

TIGHTENING only: exactly the quoted `$`/`~`-rooted `mkdir` class flips
admitted→denied. The both-directions negative controls (cpp#176/#178) prove the
contained quoted forms (`"subdir/x"`, `"./build"`, `"a b"`) stay admitted, an
absolute-in-worktree path stays a containment candidate, and the cpp#143 `/tmp`
scratch carve-out is untouched. `tier1.py` `is_tier1_auto_approve` is out of
scope.

## References

- Parent: [a-whitespace-anchored-disqualifier-is-defeated-by-a-quoted-operand](a-whitespace-anchored-disqualifier-is-defeated-by-a-quoted-operand.md) (cpp#211, the `cp`/`mv` fix that named this residue).
- Arc: [a-syntactic-classifier-cannot-prove-a-target-is-safe-prove-it-outside](../tooling-decisions/a-syntactic-classifier-cannot-prove-a-target-is-safe-prove-it-outside.md).
- Tickets: cpp#218 (this fix), cpp#211 (parent), cpp#154 D3, cpp#176 (both-directions), cpp#143 (/tmp scratch, preserved).
