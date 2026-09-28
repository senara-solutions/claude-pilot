---
title: "The ce-* scratch admission axis must be last-wins, not any-assignment"
date: 2026-09-28
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, mkdir, tmp-scratch, containment, destination-veto, admission, same-command-tracing, last-wins, reassignment, whitelist, tightening, compound-engineering, claude-pilot-143, claude-pilot-218, claude-pilot-223, claude-pilot-224, mika-2562]
applies_when: "a recognition predicate keyed on a same-command variable assignment scans for ANY matching assignment instead of resolving the variable's effective (last) value"
---

# The ce-* scratch admission axis must be last-wins, not any-assignment

## The defect (pre-existing, found during cpp#223)

mika#2562 landed axis A: a bare `$VAR`/`${VAR}` `mkdir` target whose value the
*same command string* assigns to a uid-tolerant `/tmp` scratch literal is
resolved to that literal and admitted (`_ce_scratch_variable_names` →
`_is_ce_scratch_variable_ref` → `permissions._is_sanctioned_tmp_scratch`). The
recognition scan was **any-assignment**: a variable was recognized if *any*
same-command assignment rooted at a `/tmp` scratch. It was NOT last-wins.

So a variable reassigned OUT of scratch was still recognized from its earlier
scratch assignment, even though its EFFECTIVE (last) value is what the shell
uses. Probe facts confirmed at source on the base (`9f6ea0c`, real temp git
worktree, `_denial_is_terminal` + `_destination_veto_reason(..., for_lethality=False)`):

| command | admitted (veto=None) | terminal | correct? |
|---|---|---|---|
| `X=/tmp/ok; X=$HOME/evil; mkdir -p "$X"` | **True** | **False** (survivable) | **NO** — effective value is `$HOME/evil`, an exfil target |
| `X=/tmp/ok; X=/tmp/still-ok; mkdir "$X"` | True | False | yes — last value is scratch |
| `SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; mkdir -p "$SCRATCH_ROOT"` | True | False | yes |

The first row is the hole: the pilot writes to `$HOME/evil` under an admission
the classifier granted only because a *shadowed* earlier value was scratch.

## The fix — make axis-A recognition last-wins (a TIGHTENING)

`tier1._ce_scratch_variable_names` no longer adds a variable on "any assignment
roots at /tmp". It takes the candidate variables (those with at least one `/tmp`
assignment, via `_CE_SCRATCH_ASSIGN_RE`) and, for each, re-resolves the LAST
same-command assignment across ANY value with `_last_assignment_value` (landed by
cpp#223, already last-wins for the lethality carve), then requires THAT value to
be a uid-tolerant scratch (`_is_uid_tolerant_tmp_scratch`):

```python
for m in _CE_SCRATCH_ASSIGN_RE.finditer(command):
    var = m.group("var")
    last = _last_assignment_value(command, var)
    if last is not None and _is_uid_tolerant_tmp_scratch(last):
        names.add(var)
```

`_is_ce_scratch_variable_ref` is unchanged — it still reads
`var in _ce_scratch_variable_names(command)`; the semantics are now centralized
in the names helper. Effect: `X=/tmp/ok; X=$HOME/evil; mkdir "$X"` → last value
`$HOME/evil` → NOT recognized → the cpp#218 `$`-rooted `mkdir` veto fires and, as
for every unrecognized `$`-rooted destination, terminally. **Refused + terminal.**

## What this closes and what it must not over-tighten

This CLOSES an admission (a tightening): no new admission, no new bearing. The
kept positives are unaffected because they are single-assignment — the last
value *is* the only value:

- `SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"; mkdir -p "$SCRATCH_ROOT"` — admitted.
- standalone literal `mkdir -p "/tmp/compound-engineering-$(id -u)"` — admitted.
- `S="/tmp/ce-$UID"; mkdir "$S"` — admitted.

Last-wins must not over-tighten a scratch→scratch reassignment:
`X=/tmp/ok; X=/tmp/still-ok; mkdir "$X"` → last value IS scratch → still
admitted. And the symmetric `X=$HOME/evil; X=/tmp/ok` → last value scratch →
newly recognized (correct: the effective value is the safe one).

## The lethality path is untouched

`_is_transitive_ce_scratch_mkdir_target` and its `for_lethality`-gated carve
(cpp#223) are already last-wins (`_value_roots_at_scratch` walks the LAST
assignment transitively) and correct. They are not touched. The canonical ce-*
preamble stays survivable end-to-end: `mkdir "$SCRATCH_ROOT"` stays admitted +
non-terminal, and `mkdir "$RUN_DIR"` (the transitive `$SCRATCH_ROOT/…/$RUN_ID`
form) stays refused-but-**survivable** via the lethality carve. Nothing regresses
to fatal. The tier1 gate `is_tier1_auto_approve` is not touched.

## SECURITY — why any-assignment was the wrong quantifier

The admission axis exists to prove a destination safe *lexically* without running
the shell. A variable's runtime value is its LAST assignment; an earlier
assignment is dead. Scanning for "any assignment roots at scratch" proves a
property of a value the shell will never use, so it can admit a live value the
axis never inspected. The correct quantifier over same-command assignments is
therefore **last-wins**, matching what the shell resolves and matching the
lethality carve cpp#223 already used. Any-assignment is only ever safe as an
*over*-refusal (recognizing more, to deny more) — never as an admission input.

## The learning

**A recognition predicate that admits based on a variable's same-command
assignment must resolve the variable's EFFECTIVE (last-wins) value, never "any
matching assignment".** Any-assignment is sound only for tightening a refusal; on
an admission path it grants safety to a shadowed, dead value while the live value
goes uninspected. When one part of a subsystem (here the cpp#223 lethality carve)
is already last-wins and a sibling admission axis is not, that asymmetry is the
bug — align the admission axis to the same last-wins resolver.
