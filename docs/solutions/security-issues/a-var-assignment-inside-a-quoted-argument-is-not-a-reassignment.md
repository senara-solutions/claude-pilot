---
title: "A VAR=… token inside a quoted argument is not a reassignment — command-start-aware last-wins for the transitive scratch derivation"
date: 2026-10-01
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, mkdir, tmp-scratch, containment, destination-veto, lethality, transitive-rooting, same-command-tracing, last-wins, command-substitution, quoting, echo, regression, compound-engineering, ce-code-review, claude-pilot-218, claude-pilot-258, claude-pilot-265, mika-2562, mika-1960]
applies_when: "a flat `VAR=value` scan resolves a variable's last-wins value from text that only appears inside a quoted argument (e.g. `echo \"RUN_DIR=$RUN_DIR\"`), so a same-command transitive resolver misses the real assignment"
---

# A `VAR=…` token inside a quoted argument is not a reassignment

## The regression (extends the `re-admitting-the-ce-tmp-scratch` sibling)

PR #223 (mika#2562) re-admitted the ce-* `/tmp` scratch preamble that cpp#218
collaterally denied, and a follow-up (c0b0c08) made a *transitively*
scratch-rooted `mkdir` **survivable** instead of fatal — the canonical
`RUN_DIR="$SCRATCH_ROOT/ce-…/$RUN_ID"; mkdir -p "$RUN_DIR"` derivation, resolved
by `_is_transitive_ce_scratch_mkdir_target` → `_value_roots_at_scratch`.

That carve was correct for the preamble *as the test wrote it* — but the
**official** one-line preamble the `ce-code-review` skill actually emits **ends**
with an `echo` and a logging call that both mention `$RUN_DIR`:

```
SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; mkdir -p "$SCRATCH_ROOT"; chmod 700 "$SCRATCH_ROOT"; RUN_ID="$(date +%Y%m%d-%H%M%S)-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' ')"; RUN_DIR="$SCRATCH_ROOT/ce-code-review/$RUN_ID"; mkdir -p "$RUN_DIR"; chmod 700 "$RUN_DIR"; echo "RUN_DIR=$RUN_DIR"; python3 .../ce-code-review/scripts/run-log.py event --run-dir "$RUN_DIR" --start scope
```

Pilot **819e8f6b** (implement of mika#1960) died TERMINAL at 49 turns entering
`/ce:code-review` on this exact line. Same family as cpp#258 (a non-evaluable
operand read as a proven escape) — here a *string literal* read as a reassignment.

## Cause (reproduced at source `1d7c022`, `MIKA_PILOT_CONTAINED=1`, real temp git worktree)

`tier3_for_lethality` = False; `_redirect_destination_veto_reason` = None. The
killer is `_destination_veto_reason(…, for_lethality=True)` on the
`mkdir -p "$RUN_DIR"` segment:

```
destination '$RUN_DIR' is rooted at an unresolved variable/tilde ($/~) — treated as not contained (cpp#218 / cpp#211 / cpp#154 D3 anti-respelling)
```

The precise gap — **not** the one first hypothesised (the resolver already
follows a `$RUN_ID`-bearing suffix):

- `_is_transitive_ce_scratch_mkdir_target(cmd, "$RUN_DIR")` resolves `$RUN_DIR`'s
  value via `_last_assignment_value` (a **flat** `_ANY_ASSIGNMENT_RE.finditer`
  over the whole command, LAST-WINS).
- The real assignment is `RUN_DIR="$SCRATCH_ROOT/ce-code-review/$RUN_ID"`. But the
  trailing **`echo "RUN_DIR=$RUN_DIR"`** carries the bracketed text
  `RUN_DIR=$RUN_DIR`, and the flat regex (`(?<![\w$])` lookbehind, which a `"`
  satisfies) matches it as a *later* assignment. LAST-WINS therefore resolves
  `RUN_DIR` to the self-reference `$RUN_DIR"` — which roots at nothing, so
  `_value_roots_at_scratch` returns False and the cpp#218 `$`-rooted veto fires
  **terminally**.

Measured: `_last_assignment_value(verbatim, "RUN_DIR") == '$RUN_DIR"'`; drop the
`echo` and it is `'$SCRATCH_ROOT/ce-code-review/$RUN_ID'` and the resolver
returns True. The bug is a **quoting-blind** last-wins scan, not suffix handling.

## The fix — command-start-aware last-wins, LETHALITY ONLY

A `VAR=value` is a real assignment only at a **command-start** position (the
leading assignment-prefix word(s) of a segment); `VAR=` inside a quoted argument
is data. New in `tier1.py`:

- `_last_real_assignment_value(command, var)` — walks `_split_compound_command`
  segments and matches an **anchored** `_LEADING_ASSIGNMENT_RE` only against the
  lstripped start of each segment (and the whitespace-separated prefix words that
  follow). LAST-WINS across segments. `echo "RUN_DIR=$RUN_DIR"` starts with
  `echo`, so its bracketed token is never counted.
- `_suffix_is_contained(value, command, depth)` — a suffix path component is
  contained iff it carries no literal `..` and every bare `$VAR`/`${VAR}` it
  names *that is assigned* is itself contained (bounded). This additionally
  closes an indirect traversal the literal-tail `..` check alone missed
  (`EVIL=../../etc; D="$SR/$EVIL"`).

`_value_roots_at_scratch` and `_is_transitive_ce_scratch_mkdir_target` now follow
the root chain with `_last_real_assignment_value` and gate the suffix with
`_suffix_is_contained`.

**Why admission is byte-identical.** The admission axis
(`_ce_scratch_variable_names` → `_is_ce_scratch_variable_ref` →
`_is_sanctioned_tmp_scratch`) still calls the **unchanged** flat
`_last_assignment_value`. The new resolver is reached only from the
`for_lethality=True` branch (`permissions._destination_veto_reason` line 1727, a
no-op on every admission call). The deny stays; only `_denial_is_terminal` flips
True→False. Verified: 0 diffs on the admission axis against pristine `main` over a
broad sample.

## What stays terminal (each tested)

`RUN_DIR="$HOME/x"`; `D="$SCRATCH_ROOT/../../etc"` (literal `..`);
`mkdir -p "$UNSET/x"` and `D="$UNSET/x"` (unassigned root); `D="$(curl …)"`
(command-sub **root**); a chain whose root is `$HOME`; a reassignment out of
scratch (`SR=/tmp/ok; SR=$HOME/evil`); and the indirect
`EVIL=../../etc; D="$SR/$EVIL"` traversal. A command that merely *mentions*
`VAR=` in an `echo`/argument no longer poisons the resolver, but nothing new is
admitted.

## Deliberate boundary (named)

The mika#2562 contract that an **unassigned** suffix var is survivable is kept
on purpose: bash expands it to empty, so `$SCRATCH_ROOT/ce-…/$RUN_ID` with
`$RUN_ID` unassigned stays under the scratch root and cannot escape. The ticket's
first-pass negative "unassigned suffix var → terminal" was written against a
wrong mental model (that the resolver only accepted literal suffixes); tightening
it would both regress the shipped `test_transitive_run_dir_roots_at_scratch` and
reject a provably safe form, so it was not adopted. Only a literal `..` — direct
or via an assigned suffix var — traverses, and that is rejected.

## References

- Sibling: `docs/solutions/security-issues/re-admitting-the-ce-tmp-scratch-a-uid-only-substitution-whitelist.md`
  (axes A/B) and `.../ce-scratch-admission-must-be-last-wins-not-any-assignment.md` (cpp#224).
- Doctrine: cpp#205 (survivable default; terminal reserved for proven danger);
  cpp#258 (a non-evaluable operand is not a proven escape — same "read a parse
  artefact as an escape" family); cpp#250 (linear, no ReDoS).
- Code: `src/claude_pilot/tier1.py` (`_last_real_assignment_value`,
  `_suffix_is_contained`, `_value_roots_at_scratch`,
  `_is_transitive_ce_scratch_mkdir_target`). Wiring unchanged:
  `permissions._destination_veto_reason` (for_lethality branch). PR: cpp#265.
