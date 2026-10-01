# A quoted operator char is not an operator for `TIER3_PATTERNS` — admission (cpp#236)

## Symptom

A Mika pilot searching for a plan callout — whose shape starts with `> - **Plan:**` — is
refused once per turn and burns its whole budget:

```
grep -rln '> - **Plan:**' crates/*/src/
grep -inE '^[[:space:]]*(>[[:space:]]*)+' docs/plans/x-plan.md
grep -n '> x' docs/a.md
```

Replayed on the deployed module (`MIKA_PILOT_CONTAINED=1`, cwd = worktree):

| command | policy | chain_safe | is_tier3_dangerous |
|---|---|---|---|
| `grep -n 'x' docs/a.md` | allow `bash-grep` | True | False |
| `grep -n '> x' docs/a.md` | allow `bash-grep` | **False** | **True** |
| `grep -n 'a>b' docs/a.md` | allow `bash-grep` | **False** | **True** |

Verbatim occurrences: 57ad9d76 (mika#2194), e9cb7b6b (mika#2606, in flight at 116/151
turns) — the latter was tasked precisely with the `>`-quoted plan-header population it
could not even measure.

## Why (verified at source, HEAD 901b7e3)

The generic redirect entry of `TIER3_PATTERNS` is quote-blind:

```
(?<!<)>{1,2}(?!\(|&[\d-])   matches the `>` inside   grep -n '> x' docs/a.md
```

`is_tier3_dangerous` returns True, and the chain-safety gate
`permissions._bash_allow_is_chain_safe` has the clause
`pd.decision == "allow" and not is_tier3_dangerous(seg)`. The quoted `>` makes that clause
False, so the policy `allow` (`bash-grep`) is flipped to a deny. The `>` that bash reads as
ordinary text — because it is inside a single/double-quoted token — is counted as a shell
redirect.

This is the ADMISSION half of cpp#236. The LETHALITY half shipped in #238
(`is_tier3_dangerous_for_lethality` + `_mask_lethality_redirect_chars`): those refusals are
already `(non-terminal)`. Admission still bites.

## Fix (admission, Prime-ratified 2026-10-01)

Mask (blank, length-preserving) every `<`/`>`/`|`/`&` that a shlex-grade single-pass
scanner finds inside a TOP-level single- or double-quoted token, then run the UNCHANGED
`TIER3_PATTERNS` on the masked string.

- `tier1._mask_quoted_operator_chars_for_admission(command) -> str` — a context-stack
  parser (TOP / DQ / SQ / SUB-with-paren-depth / BT), the ADMISSION analog of the
  LETHALITY path's `_mask_lethality_redirect_chars`. It differs in two ways:
  1. it masks four operator chars, not only `<`/`>` (the generalization: a quoted `<`,
     `|`, or `&` is no more an operator than a quoted `>` — the `'"'"'` idiom is handled
     naturally by SQ/DQ tracking);
  2. it NEVER masks inside a `$(…)` command substitution or a `` `…` `` backtick region —
     it tracks them (via an `sub_bt_depth` counter) only so the quote state after the
     substitution stays correct. `$(…)`/backtick are out of tier3 since mika#946, so their
     handling stays byte-identical.
- `tier1.is_tier3_dangerous` — runs the masker before the existing `_FD_DEVNULL_RE` strip
  and the `TIER3_PATTERNS` search. Nothing else changes.

Linear (one pass, O(1) per char), no regex → not a ReDoS surface (cpp#250). FAIL-CLOSED:
an unbalanced quote/substitution (stack non-empty at end) returns the command UNCHANGED, so
a dangling-quote command stays flagged — the safe direction for an admission classifier.

Invariants: the change flips `is_tier3_dangerous` for the quoted-operator case ONLY. A
broad sample of currently-allowed and currently-refused commands with no top-level quoted
operator is byte-identical (measured: of 60 sampled, only the 4 quoted-`>`/`<(` cases
flip). The tier1 gate `is_tier1_auto_approve` and the egress axis are untouched; lethality
(`is_tier3_dangerous_for_lethality`, `_denial_is_terminal`) is not consulted and does not
regress (a quoted `>` was already non-terminal via #238). Real operators OUTSIDE quotes stay
refused: `grep x f > /etc/y`, `grep '>' f >> ~/.bashrc`, `git commit -m "x" > /etc/passwd`,
`echo "$(cat /etc/shadow)" > x`, `echo x >> ~/.bashrc`.

## Lesson

Deciding "is this operator char inside a quoted token" is a separate question from "is this
char a dangerous operator" (the tier3 check), and the first must be answered by a real
shlex-grade tokenizer, never by a quote-matching regex. The LETHALITY path already learned
this (cpp#236 #238); the ADMISSION path is the same classifier applied to the
`TIER3_PATTERNS` input. The `$(…)`/backtick boundary is where a flat scanner desyncs on the
`'"'"'` idiom, so the context-stack parser is load-bearing even when — as here — it refuses
to descend into the substitution.
