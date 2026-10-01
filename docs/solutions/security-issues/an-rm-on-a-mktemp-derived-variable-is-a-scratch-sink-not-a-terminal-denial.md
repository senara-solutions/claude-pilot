---
title: "An rm on a $(mktemp -d)-derived variable is a scratch SINK, not a terminal denial"
date: 2026-10-01
last_updated: 2026-10-01
module: claude_pilot.tier1
component: permission-classifier
problem_type: design_decision
category: security-issues
severity: medium
tags: [permissions, policy, lethality, denial, rm, mktemp, mkdir, chmod, scratch, last-wins, fail-closed, reassignment-forms, claude-pilot-201, claude-pilot-213, claude-pilot-265, claude-pilot-266, claude-pilot-268, mika-2626]
applies_when: "carving a survivable-deny exemption for a destructive verb whose target is a variable provably assigned from a fresh-scratch system utility"
---

# An rm on a $(mktemp -d)-derived variable is a scratch SINK, not a terminal denial

## Context

cpp#201 taught the lethality axis one half of the mktemp idiom: a redirect whose
target is rooted at a variable the SAME command assigns from `$(mktemp …)` is a
fresh `/tmp` working file by construction, so it is not grounds to end the
session (`_is_mktemp_scratch_redirect_target`). That carve covered the SOURCE
(`T=$(mktemp -d); cat >"$T/log"`) but never the SINK: when a pilot is done with
the scratch dir it deletes it, and `rm -rf "$T"` was still a proven-danger verb
that killed the run.

groom 93bac846 (mika#2626) died TERMINAL at turn 12 reproducing a git defect in a
throwaway repo. The command is a `;`-chain, multi-line, with a `cd /tmp` out of
the worktree — so the refusal is **legitimate** and must stay. Its last line is
the killer:

```
cd /tmp 2>/dev/null; B=$(mktemp -d); C=$(mktemp -d); git -C "$B" init … ;
… ; rm -rf "$B" "$C"
```

`_denial_is_terminal` line by line: only `rm -rf "$B" "$C"` is terminal; the 11
other lines (git init/clone/push to `file://$B`, update-ref, ls-remote, fetch,
force-with-lease push) are survivable. `B` and `C` are assigned, in the same
command, from `$(mktemp -d)` — a fresh directory under `$TMPDIR`/`/tmp`. Axis A
knew neither the `$(mktemp -d)` source nor the `rm -rf` sink, so it ended the
session over a cleanup it had already sanctioned writing into.

## Guidance

### 1. The SINK is the exact twin of cpp#201's SOURCE and cpp#213's prefix rm

Build it as a structural twin, not a new mechanism. `rm_targets_mktemp_scratch`
is `rm_confined_to_pilot_scratch` (cpp#213) with one operand predicate swapped:
split the command, drop each `rm`/`rmdir` segment whose **every** operand is a
live mktemp-scratch variable, and re-run the unchanged
`is_tier3_dangerous_for_lethality` on what remains. The remainder re-check is
what keeps every mixed and chained shape terminal with no per-shape logic: a
mixed operand list (`rm -rf "$X" /etc`), a chained destructive verb
(`… && git reset --hard`), a second unconfined `rm`, a `..` tail — each leaves a
still-dangerous remainder (or is never carved) and stays fatal, fail-closed.

### 2. Flip lethality, never admission — and prove the line did not move

The exemption changes exactly one bit: `_denial_is_terminal` returns `False`.
`is_tier3_dangerous` (the refusal classifier), `is_tier1_auto_approve`, every
YAML rule, and even `is_tier3_dangerous_for_lethality` itself are byte-identical
to HEAD — none of them consults the new code; it is reached ONLY from the
`_denial_is_terminal` rm-lethality `and`-chain. A survivable deny is still a
deny: the command stays refused and the pilot routes around it instead of dying.
Pin this with a dedicated admission-identity test and an empirical broad-sample
diff against the served code (0 diffs here). The redirect/destination vetoes
below the carve still run on the FULL command, so a carved `rm` that ALSO
redirects out of the worktree is re-armed there.

### 3. The source must be LAST-WINS, because a reassignment is the real risk

If `mktemp` fails, `B=""` gives an inert `rm -rf ""` — harmless. The genuine
danger is a reassignment between the derivation and the sink:
`X=$(mktemp -d); X=/; rm -rf "$X"`. The membership check cpp#201 uses
(`_mktemp_scratch_variable_names`, "assigned from mktemp ANYWHERE") does not see
that reassignment and would wrongly carve it. So resolve the variable's **last**
command-START assignment, the cpp#265/#266 discipline: a `VAR=…` token inside a
quoted argument (`echo "X=/etc"`) is not a reassignment (walk
`_split_compound_command` segments and count only leading assignment-prefix
words), and a real reassignment to any non-mktemp value — a literal, another
variable, or a non-mktemp command substitution (`X=$(curl …)`) — makes the last
value non-scratch and keeps the command terminal.

### 4. One shared source recognition keeps the mktemp subst whole

cpp#265's `_last_real_assignment_value` is the right last-wins walk, but its
value charset (`[^\s;|&()<>]*`) truncates a `$(…)` value at the `(`, so it
cannot tell a `$(mktemp -d)` assignment from a `$(curl …)` one — both read back
as a bare `$`. The source recognition (`_last_establishing_assignment`) uses the
command-start assignment-prefix matcher (`_LEADING_ASSIGN_OP_RE`), which keeps
the mktemp substitution whole in its own `mk` branch while every other value
falls to the dq/sq/bare branches. It returns the var's LAST establishing
`NAME=value` as `("mktemp", …)` or `("value", …)` in one linear pass, so
`_var_is_live_mktemp_scratch` decides mktemp-membership directly and the cpp#266
transitive resolver gets the raw value. No nested quantifier (cpp#250 — no
ReDoS); a 50-occurrence chain resolves in a few ms.

### 6. The reassignment guard is INVERTED to reads-only — shared with cpp#266

The first two cuts ENUMERATED write forms (first only a bare `NAME=`; then
`export`/`readonly`/`local`/`declare`/`typeset`/`+=`/`read`/`for`/`unset`), and
the MPC gate kept proving the next uncovered form: `{ B=/etc; }` (brace group),
`IFS= read -r B` (a prefix assignment hiding the `read`), `let B=1` / `((B=1))`
(arithmetic), and `: ${B:=/etc}` (default-ASSIGN parameter expansion) all left
the sink wrongly survivable. The SAME forms SERVED the cpp#266 mkdir/chmod sink
(`SCRATCH_ROOT="/tmp/…"; : ${SCRATCH_ROOT:=/etc}; mkdir -p "$SCRATCH_ROOT"`),
because both sinks share one last-wins resolver.

Enumerating writes is whack-a-mole. INVERT it to fail-closed, once, in the shared
`_live_scratch_source`, and route both resolvers through it
(`_var_is_live_mktemp_scratch` for the mktemp-`rm` path;
`_last_real_assignment_value` → `_value_roots_at_scratch` →
`_is_transitive_ce_scratch_mkdir_target` for the transitive-scratch path):

1. Find the var's LAST scratch-ESTABLISHING command-start assignment (the
   positive source — `$(mktemp -d)` for #268, a scratch-rooting value for #266).
2. Scan the command region AFTER it up to the sink. The variable name may appear
   ONLY AS A READ — a bare parameter expansion `$B` / `${B}` / `"$B"` using a
   NON-assigning operator (`:-` `:+` `:?` `#` `##` `%` `%%` `/` `^` `,`
   `:off:len`). ANY OTHER occurrence — an assignment in any form (`B=`, keyword
   `export B=`, append `B+=`, a brace-group `{ B=…; }`), a default-ASSIGN
   `${B:=…}`/`${B=…}`, `read`/`IFS= read`/`mapfile`/`readarray`/`getopts B`,
   `let B=…`/`((B=…))`/`$((B=…))`, `printf -v B`, `unset B`, `for B in …`, a
   dynamic `"$NAME=…"` indirection, `eval`, a `source`/`.` that sources a script
   into the current shell (gate n°3 — a sourced script reassigns the var opaquely,
   exactly like `eval "$CMD"`; `./script` is an exec in a child, not a source), or
   the bare name used as a command-position token — makes the sink TERMINAL.

The name is matched at a word / `${` / `$` boundary, so `$BAR` is not `$B` and a
`C` inside a flag `-C` is not an occurrence of `$C`; a bare name inside a quoted
LITERAL that is not an expansion (a `read -p "type B"` prompt, `echo "B=/etc"`)
is not an occurrence either. A subshell `( … )` assignment does not propagate and
is skipped, so `B=$(mktemp -d); (B=/etc); rm -rf "$B"` stays survivable; a bare
name that is an attribute-only argument of a declaration keyword (`export -n B`,
`readonly B`) keeps its value and stays a read. The preserved positives (the
93bac846 verbatim, `export X=$(mktemp -d)`, the `declare`-form ce-code-review
preamble, `"$X/sub"` with a safe tail) all satisfy reads-only and remain
survivable.

Lethality-only on both paths. The admission axis (`_ce_scratch_variable_names` /
the flat `_last_assignment_value`) is untouched; for the #266 mkdir sink the flat
axis-A `_is_sanctioned_tmp_scratch` keeps its exact admission behavior and only
DEFERS the `$VAR` case to the inverted transitive carve on the lethality path
(`for_lethality=True`), so admission stays byte-identical to HEAD.

### 5. Reuse the cpp#201 target shape so a traversal never launders out

The operand predicate reuses `_MKTEMP_SCRATCH_TARGET_RE` (anchored both ends, no
`..` anywhere in the tail), so `rm -rf "$X/../.."` — a traversal out of the
mktemp dir even though `$X` is legitimate — never matches and stays terminal,
exactly as the redirect carve rejects `"$T/../etc"`. A bare `$VAR` with a safe,
`..`-free tail (`"$X/sub"`) stays under the scratch dir and is carved.

## References

- Code: `src/claude_pilot/tier1.py`
  (`rm_targets_mktemp_scratch`, `_rm_operand_is_mktemp_scratch`,
  `_var_is_live_mktemp_scratch`; the SHARED `_live_scratch_source` —
  `_last_establishing_assignment` (positive source, `_LEADING_ASSIGN_OP_RE` /
  `_DECL_KEYWORD_PREFIX_RE`) + `_var_reassigned_nonread_after` /
  `_var_has_nonread_occurrence` (the INVERTED reads-only scanner) — also consumed
  by the cpp#265/#266 `_last_real_assignment_value`),
  `src/claude_pilot/permissions.py` (`_denial_is_terminal` and-chain;
  `_is_sanctioned_tmp_scratch` axis-A deferred to the inverted transitive carve
  under `for_lethality`).
- Siblings: cpp#201 (mktemp redirect SOURCE carve), cpp#213 (`.pilot-scratch` rm
  prefix carve — this carve's structural twin),
  cpp#265/#266 (transitive scratch derivation, LAST-WINS over command-start
  assignments — the reassignment-form hole was SHARED and closed in the same
  scanner here). Doctrine cpp#205 (default survivable); cpp#250 (ReDoS lesson).
- Plan: `docs/plans/2026-10-01-007-fix-268-mktemp-derived-rm-lethality-plan.md`.
- Ticket: cpp#268 / mika#2626 (groom 93bac846). Priority low (n=1).
