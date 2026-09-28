---
title: "Re-admitting the ce-* /tmp scratch — a uid-only substitution whitelist"
date: 2026-09-28
module: claude_pilot.permissions
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, mkdir, chmod, tmp-scratch, containment, destination-veto, admission, same-command-tracing, command-substitution, whitelist, regression, compound-engineering, claude-pilot-143, claude-pilot-201, claude-pilot-218, mika-2562]
applies_when: "a containment tightening keyed on a metacharacter also denies a legitimate idiom whose safety is provable lexically from a fixed, whitelisted token set"
---

# Re-admitting the ce-* /tmp scratch — a uid-only substitution whitelist

## The regression

cpp#218 closed the quoted-`$HOME` `mkdir` exfil hole by adding a
`$`/`~`-rooted-destination veto to `_destination_veto_reason` (the `mkdir`
sibling of cpp#211). Correct — but it also denied the compound-engineering
`ce-*` scratch preamble that opens **every** `/mika` pipeline step (mika#2562):

```
SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"
(umask 077; mkdir -p "$SCRATCH_ROOT") || exit 1
chmod 700 "$SCRATCH_ROOT" || exit 1
```

Because the veto is an *unconditional* `interrupt=True` at its honoring site
(the cpp#128 containment-breach exception), the refusal was not merely a refusal
— it was **terminal**. Every ce-* step killed the pilot.

Probe facts confirmed at source on the base (`a49d3c3`), real temp git worktree:

| command | policy | veto | terminal |
|---|---|---|---|
| `mkdir -p "$SCRATCH_ROOT"` | allow (`bash-mkdir`) | cpp#218 | **True** (the killer) |
| `mkdir -p /tmp/compound-engineering-1000` | allow (`bash-mkdir-tmp-scratch`) | None | False |
| `mkdir -p "/tmp/compound-engineering-$(id -u)"` | allow (`bash-mkdir`) | cpp#38 | True |
| `chmod 700 "$SCRATCH_ROOT"` | deny (default) | None | False (already survivable) |
| `mkdir -p "$HOME/x"` | allow (`bash-mkdir`) | cpp#218 | **True** (must stay) |

## The fix — a bounded extension of the cpp#143 /tmp-scratch sanction

cpp#143 already sanctions a *literal* `/tmp` scratch mkdir
(`_is_sanctioned_tmp_scratch`, `_TMP_SCRATCH_MKDIR_RE = ^/tmp/(?!.*\.\.)[\w./-]+$`).
Two things put the ce-* scratch outside it; the fix extends the recognition on
each, both tightly bounded, mirroring cpp#201's mktemp same-command tracing:

- **Axis B — a uid token in the /tmp path.** Relax the recognition to tolerate
  *only* a benign uid token: exactly `$(id -u)`, `` `id -u` ``, `$UID`, `${UID}`,
  `$EUID`, `${EUID}`. Implemented by *masking* those exact tokens and requiring
  the remainder to be an ordinary cpp#143 `/tmp` scratch — so any **other**
  substitution or variable survives the mask and fails the `[\w./-]` charset.
- **Axis A — same-command variable tracing.** A bare `$VAR`/`${VAR}` mkdir
  target whose value the **same command string** assigns to a uid-tolerant
  `/tmp` scratch literal is resolved to that literal. Reuses the cpp#201
  mechanism verbatim (`_mktemp_scratch_variable_names` →
  `_ce_scratch_variable_names`; `_is_mktemp_scratch_redirect_target` →
  `_is_ce_scratch_variable_ref`). Lexical only — never resolve to grant (cpp#143).

## SECURITY — the uid whitelist is a literal token set, never arbitrary subst

The dangerous over-reach would be tolerating *arbitrary* `$(...)` or `$VAR` in a
`/tmp` path: the shell executes the substitution, so `mkdir "/tmp/x-$(rm -rf ~)"`
would run `rm -rf ~`. The whitelist is therefore an **exact literal match** of the
uid tokens only. Everything else stays vetoed, proven by test:
`/tmp/x-$(whoami)`, `/tmp/x-$(rm -rf /)`, `/tmp/x-$(id -u; rm -rf /)` (decorated —
not the exact token), `/tmp/$OTHER`, `/tmp/x-$UIDFOO` (longer name), `$(id -g)`
(gid, not uid) are all **not** admitted. The uid-token regex is anchored so a
decorated substitution never matches the token and its metacharacters fall
through to the charset check, which rejects them.

Every ratified negative is preserved (both `_destination_veto_reason` and the
full handler): `mkdir "$HOME/x"` / `"~/x"` stay **terminal** (cpp#218 intact); a
`$VAR` assigned same-command to a non-`/tmp` value (`X="/etc/evil"`) stays
refused; a `$SCRATCH_ROOT` with **no** same-command assignment is unresolvable
and stays refused; a traversal tail on the reference (`"$SCRATCH_ROOT/../etc"`)
or in the assigned value (`S="/tmp/../etc"`) stays refused (the cpp#143
`(?!.*\.\.)` guard); `rm -rf` outside `.pilot-scratch/` stays terminal.

## What flips, and what does not (the honest boundary)

The change is confined to `_destination_veto_reason`, which sits **below** two
gates the fix deliberately does not touch:

- The **self-resolving uid-parameter literal** forms
  (`mkdir -p "/tmp/compound-engineering-$UID"`, `${UID}`, `$EUID`) are
  **genuinely admitted** (handler `Allow`): they are policy `allow` (`bash-mkdir`),
  chain-safe (a parameter expansion, not a command substitution), and now un-vetoed.
- The **compound preamble** and the **`$(id -u)`/`` `id -u` `` command-substitution**
  forms flip from **terminal to survivable** (`interrupt=False`), not to `Allow`:
  the compound is policy default-deny (a leading assignment is not `^mkdir`), and
  the `$(` form is independently vetoed by `_bash_allow_is_chain_safe` (the uid
  substitution is not on the closed-world `_SUBSTITUTION_ALLOWLIST`). This restores
  the **pre-cpp#218** posture (the kill is gone; the pilot survives and adapts).

Genuine end-to-end admission of the `$(id -u)` compound would additionally
require adding `$(id -u)` to `_SUBSTITUTION_ALLOWLIST` (a chain-safety change) and
a policy-`allow` path for the assignment-prefixed compound — both **outside**
this ticket's bounded scope, both separate evidence-gated decisions.

## chmod

`chmod` is not classified by `_segment_write_kind`, so it never flows through
`_destination_veto_reason`; it is policy default-**deny** and already
**non-terminal** (it was never part of the cpp#218 kill). Admitting the
*variable* form `chmod 700 "$SCRATCH_ROOT"` cannot be done by lifting a veto —
it would require a **new deny→allow override path** in the handler (a broad new
admission mechanism, and a chain-safety-bypass risk). Per the ticket's own
guardrail ("if admitting chmod cleanly is not feasible without a broad change,
stop and report — do not broaden chmod"), chmod is left unchanged: it stays a
survivable refusal.

## The learning

**When a containment tightening also denies a legitimate idiom, re-admit only
the sub-language whose safety you can prove lexically — a fixed, whitelisted
token set — and mask-then-recheck so anything outside the set fails the original
charset.** Extend the veto layer alone; do not silently broaden the policy-allow
or chain-safety gates that sit above it. Where the idiom's real shape needs those
gates too, say so and leave them to a separate decision rather than widening them
by reflex.

## Correction — the canonical preamble needs TRANSITIVE rooting (lethality only)

The first cut admitted the standalone `$SCRATCH_ROOT` form but the MPC gate showed the
CANONICAL preamble was still **fatal**: it builds a second variable
`RUN_DIR="$SCRATCH_ROOT/ce-code-review/$RUN_ID"` and does `mkdir -p "$RUN_DIR"`. `$RUN_DIR`'s
value is `$<recognized-scratch-var>/<suffix>`, not a `/tmp` literal, so axis A did not
recognize it and cpp#218's veto still fired terminally.

Measured at the source, the **pre-#218** posture of that exact compound was a **survivable
deny** (an assignment-prefixed compound is policy default-deny, non-terminal; the dir was
never created by that command). So the regression was purely the *terminality*. The
correction restores it, **lethality only**: a `mkdir` whose destination variable roots
(transitively, via same-command **last-wins** assignments) at a recognized scratch —
`_is_transitive_ce_scratch_mkdir_target`, consulted ONLY from the `for_lethality` veto path
— flips terminal→survivable. The deny STAYS a deny; admission is **byte-identical** (the
carve is a no-op on every `for_lethality=False` call). It also accepts the preamble's
`${TMPDIR:-/tmp}/…` fallback root. Resolution is **last-wins**, so a reassignment out of
scratch (`SR=/tmp/ok; SR=$HOME/evil; RUN=$SR/x; mkdir "$RUN"`) stays fatal; a `..` in any
suffix, an unassigned var, and a non-uid substitution stay fatal too.

**Known out-of-scope caveat (cpp#224).** The DIRECT form `X=/tmp/ok; X=$HOME/evil; mkdir "$X"`
is admitted+survivable at HEAD because the pre-existing **axis-A admission** is NOT last-wins
(it recognizes `X` from the first `/tmp/ok` assignment). Making that fatal is a separate
admission-narrowing (a tightening), tracked in cpp#224; it is deliberately not done here so
this stays lethality-only / admission-byte-identical.

## References

- Extends: cpp#143 (`/tmp` scratch sanction, `_is_sanctioned_tmp_scratch`).
- Mirrors: cpp#201 (mktemp same-command tracing, `_is_mktemp_scratch_redirect_target`); cpp#209 (`for_lethality`-gated carve shape).
- Preserves: cpp#218 (quoted `$`/`~`-rooted mkdir veto), cpp#38 (containment), cpp#154 D3 (`$HOME`-stays-terminal).
- Follow-up: cpp#224 (make axis-A admission last-wins — a tightening).
- Tickets: mika#2562 (this fix), cpp#218 (the regression source), cpp#211 (parent class).
