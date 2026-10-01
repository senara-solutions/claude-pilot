---
title: "A mkdir operand cut by a substitution is unevaluable, not a proven escape"
date: 2026-10-01
module: claude_pilot.permissions
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, mkdir, tmp-scratch, containment, destination-veto, lethality, command-substitution, shlex, tokenizer, redos, compound-engineering, claude-pilot-258, claude-pilot-218, claude-pilot-143, claude-pilot-250, mika-2562, mika-1833]
applies_when: "a shlex-based operand extractor splits an unquoted command substitution on its internal whitespace, truncating the operand so a whitelist no longer recognizes it, and the truncated token is then read as a PROVEN containment escape that ends the session"
---

# A mkdir operand cut by a substitution is unevaluable, not a proven escape

## The death

Pilot **39ae2723** (mika#1833, 2026-10-01) died TERMINAL at 42 turns, entering
`/ce:code-review`, on the review skill's scratch-directory preamble:

```
mkdir -p /tmp/compound-engineering-$(id -u)/ce-code-review/20261001-cr1833 && echo /tmp/compound-engineering-$(id -u)/ce-code-review/20261001-cr1833
```

No review, no compound. The quoted form (`"$SCRATCH_ROOT"` /
`"/tmp/compound-engineering-$(id -u)/…"`) already survived (mika#2562); this is the
**unquoted literal** form the model improvises on its own — one instance kills the
session at the top of the review.

## Root cause — a PARSE DEFECT read as PROOF

`_extract_mkdir_destinations` splits with `shlex.split`, which treats `$`/`(` as
ordinary characters and splits on whitespace. An **unquoted** `$(id -u)` therefore
breaks on its internal space:

| command | shlex operand | terminal (HEAD) |
|---|---|---|
| `mkdir -p "/tmp/compound-engineering-$(id -u)/x"` (quoted) | `/tmp/compound-engineering-$(id -u)/x` | **False** (survives) |
| `mkdir -p /tmp/compound-engineering-$(id -u)/x` (unquoted) | `/tmp/compound-engineering-$(id` | **True** (the death) |
| `` mkdir -p /tmp/compound-engineering-`id -u`/x `` | `` /tmp/compound-engineering-`id `` | **True** |
| `mkdir -p /tmp/compound-engineering-$UID/x` | `/tmp/compound-engineering-$UID/x` | **False** (survives) |

The truncated token `/tmp/compound-engineering-$(id` is no longer recognized by
`_is_uid_tolerant_tmp_scratch` (mika#2562 axis B, which **does** recognize whole
`$(id -u)` / `` `id -u` ``), so it falls into
`_destination_veto_reason(…, for_lethality=True)`'s cpp#38 containment veto as a
**proven** escape → `_denial_is_terminal` returns True. This contradicts the
`_denial_is_terminal` doctrine: *a denial a syntactic classifier merely could not
parse or prove defaults to survivable.* The shlex split did not prove an escape —
it **mangled the operand** and then judged the fragment.

## The fix — respect the substitution as a lexical unit (lethality only)

Stop truncating, then let the **unchanged** downstream classifier decide on the
WHOLE operand.

- **`_subst_aware_word_split(seg)`** — a new tokenizer that word-splits like the
  shell but treats `$(…)` and `` `…` `` as **opaque lexical units** (their internal
  whitespace never ends a word) and strips quotes the way shlex does. It returns
  `None` when a quote or substitution is left **unbalanced** — the operand is then
  *unevaluable*. It is a single left-to-right pass, **no regex and no backtracking**,
  so a 2000-char operand with deeply nested `$(` is O(n) with no ReDoS surface (the
  cpp#250 lesson: a substitution scanner must never be a backtracking regex).

- **`_destination_veto_reason`** — on the `for_lethality=True` path only, and only
  when the segment actually contains a substitution, re-extract the mkdir operands
  with that tokenizer and run the WHOLE operands through the **same** existing
  checks. An unbalanced substitution (`None`) → the segment is unevaluable →
  `continue` (survivable, never "proven escape").

The whole uid-tolerant operand now reaches `_is_sanctioned_tmp_scratch` intact and
is recognized → survivable (still refused). **The uid whitelist is not widened** —
only the extractor stops cutting the token so the existing whitelist sees it.

## SECURITY — every escape stays terminal; only truncation is undone

The re-extraction produces the **whole literal operand** and hands it to the
unchanged containment logic, so every ratified negative stays **terminal**, proven
by test:

- `mkdir -p /etc/x` — absolute non-/tmp, vetoed by `is_within_project`.
- `mkdir -p /tmp/$(curl evil)/x` — a **non-uid** substitution: the whole operand
  fails `_is_uid_tolerant_tmp_scratch` (only the exact uid tokens are masked) and
  is vetoed as outside the worktree.
- `mkdir -p "$HOME/x"` — `$`-rooted, vetoed by cpp#218 before resolution.
- `mkdir -p /tmp/compound-engineering-$(id -u)/../../etc/x` — the `..` fails the
  `(?!.*\.\.)` scratch guard and the path resolves outside.
- `mkdir -p /tmp/x-$(whoami)/y`, `$(id -u; rm -rf /)` (decorated — not the exact
  token), a segment mixing a good uid operand with `/etc/evil`, and a uid segment
  chained (`&&`) to an escape segment — all stay terminal.

Because the carve is gated on `for_lethality`, the **refusal** question
(`for_lethality=False`, the allow-path destination veto and the deny-message site)
is byte-identical to HEAD, and every substitution-free mkdir is untouched. Admission
cannot widen independently either: no policy rule admits a `$`-bearing mkdir
(`bash-mkdir` has `(?!\$)` lookaheads; `bash-mkdir-tmp-scratch` uses the `[\w./-]`
charset), so a `$(…)` form is always default-deny and never reaches the allow-path
veto. The deny STAYS a deny; only `_denial_is_terminal` flips True→False.

## What flips, and what does not (the honest boundary)

The unquoted `$(id -u)` / `` `id -u` `` scratch mkdir flips from **terminal** to
**survivable** (`interrupt=False`), **not** to `Allow`: it is policy default-deny and
the uid command substitution is not on the chain-safety
`_SUBSTITUTION_ALLOWLIST`. This restores the pre-death posture (the kill is gone; the
pilot survives and adapts). Genuine end-to-end admission of the `$(id -u)` form would
additionally require a chain-safety and a policy-allow change — the same out-of-scope
decision mika#2562 already named, deliberately not done here.

## The learning

**When an operand extractor can mangle its input — e.g. a POSIX word-splitter that
cuts an unquoted `$(…)` on its internal whitespace — a classifier downstream must
not treat the fragment as proof of anything.** Fix the extraction to keep the
substitution whole (a linear, substitution-respecting pass — never a backtracking
regex), then let the unchanged whitelist and containment checks judge the real
operand; and where the operand cannot be parsed at all, default to survivable, not to
"proven escape". Keep the repair on the lethality side, gated, so admission stays
byte-identical.

## References

- Reuses, does not widen: mika#2562 axis B (`_is_uid_tolerant_tmp_scratch`,
  `_UID_TOKEN_RE`), cpp#143 (`_is_sanctioned_tmp_scratch`).
- Mirrors: cpp#201/#209/#213/#237/#252 (`for_lethality`-gated lethality carve shape).
- Preserves: cpp#218 (`$`/`~`-rooted mkdir veto), cpp#38 (containment), cpp#154 D3
  (`$HOME`-stays-terminal). ReDoS lesson (linear tokenizer, no nested quantifier):
  cpp#250.
- Doctrine: cpp#205 (survivable default; terminal reserved for proven danger).
- Tickets: mika#1833 / cpp#258 (this fix), mika#2562 (the quoted/`$VAR` sibling),
  cpp#218 (the `$`-rooted veto this preserves).
