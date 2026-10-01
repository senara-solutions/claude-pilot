---
title: "The prescribed .pilot-scratch/ root is a derived-scratch root, not a terminal denial"
date: 2026-10-02
last_updated: 2026-10-02
module: claude_pilot.tier1
component: permission-classifier
problem_type: design_decision
category: security-issues
severity: medium
tags: [permissions, policy, lethality, denial, mkdir, chmod, redirect, scratch, pilot-scratch, pwd, worktree-relative, derived-scratch, last-wins, reads-only, fail-closed, symlink-aware, claude-pilot-38, claude-pilot-201, claude-pilot-213, claude-pilot-265, claude-pilot-266, claude-pilot-270, claude-pilot-272, mika-2623]
applies_when: "a pilot writes to a variable-carried target under the repo-prescribed worktree scratch root and the derived-scratch resolver only knows /tmp and mktemp roots"
---

# The prescribed .pilot-scratch/ root is a derived-scratch root, not a terminal denial

## Context

cpp#265/#266/#270 built a derived-scratch resolver for the lethality axis: a
`mkdir`/`chmod`/redirect target carried by a variable the SAME command keeps as a
live scratch value is a working directory by construction, so it is a survivable
deny, not grounds to end the session. The resolver's root recognizers knew the
SYSTEM roots — `/tmp/…` (cpp#266) and `$(mktemp -d)` (cpp#270/#201). They did not
know the root the repo instructions actually PRESCRIBE for disposable work:
`.pilot-scratch/` inside the worktree (the same directory cpp#213's `rm` sink
already confines to).

Pilot c722b251 (mika#2623) built a disposable git repro EXACTLY there — under
`.pilot-scratch/`, no `/tmp`, no `mktemp`, no `rm -rf` — and died at line 2:

```
D=.pilot-scratch/git-probe
mkdir -p "$D"        # ← terminal
```

The literal `mkdir -p .pilot-scratch/git-probe` is survivable (it passes the
worktree-containment check). The moment the same target passes through a
variable, the `$`-rooted mkdir veto (cpp#218) and the `$`-rooted redirect veto
(cpp#154 D3) fire TERMINAL — the derived-scratch resolver would have saved it, but
its root recognizers did not list `.pilot-scratch/` or `$PWD/.pilot-scratch/`.

## Guidance

### 1. Teach the SAME resolver a new root — do not fork a parallel one

The fix is one new root recognizer joining `/tmp` and mktemp in the existing
`_value_roots_at_scratch`, not a second resolver. `_is_pilot_scratch_rel(value)`
recognizes a worktree-relative `.pilot-scratch` or `.pilot-scratch/<path>` with no
`..`. Once the root is recognized, the EXISTING reads-only reassignment rule
(`_var_has_nonread_occurrence` via `_live_scratch_source`) and the transitive
derivation (`_last_real_assignment_value`) apply UNCHANGED, so every negative the
resolver already enforced for `/tmp`/mktemp — a reassignment out of scratch in any
form, a `..` tail, a non-scratch sibling — is inherited for free.

### 2. `$PWD` is the worktree root, but only `.pilot-scratch` under it is scratch

The prescribed idiom composes the root through `$PWD`
(`R="$PWD/.pilot-scratch/repo"`, and transitively `R="$PWD/$D/repo"` where
`D=.pilot-scratch/…`). Treat a leading `$PWD/` / `${PWD}/` as the pilot's cwd — a
fixed root bash always sets, needing no same-command assignment — and require the
TAIL after it to ITSELF root at `.pilot-scratch`. So `$PWD/.pilot-scratch/x` and
`$PWD/$D/x` are scratch, while `$PWD/foo` (in-worktree but NOT under the scratch
root — the ticket scopes to `.pilot-scratch`) and `$PWD/../x` (a `..` tail,
out of the worktree) are not. Fail closed on a reassigned `PWD`: if the command
assigns `PWD`, skip the fast-path and resolve it as an ordinary (here
non-scratch) variable, so `PWD=/etc; "$PWD/.pilot-scratch/x"` stays terminal.

### 3. A worktree-relative root needs fs-aware containment; a system root does not

`/tmp` and mktemp are SYSTEM locations recognized LEXICALLY (cpp#143 doctrine:
never resolve to GRANT). `.pilot-scratch` is WORKTREE-RELATIVE, so its
containment is the cpp#38/#213 question `is_within_project` / cpp#213's rm sink
already answer: is `<cwd>/.pilot-scratch` really under the worktree, or an
outbound symlink? Reuse `is_within_pilot_scratch` — the same symlink-aware,
fail-closed `<cwd>/.pilot-scratch` notion — at the lethality sink: thread the
`cwd` through `_value_roots_at_scratch`, and when it is present hold the
`.pilot-scratch` base case to that containment. An outbound-symlink
`.pilot-scratch` and an unresolvable cwd both stay terminal. With `cwd=None`
(direct unit calls) the recognizer stays purely lexical, so the `/tmp`/mktemp
behavior and the existing unit tests are byte-identical. This is the one place the
`.pilot-scratch` carve diverges from the system-root carves, and it diverges
toward MORE containment, never less.

### 4. The redirect sink needs the var+tail shape the mkdir sink lacks

The transitive mkdir carve (`_is_transitive_ce_scratch_mkdir_target`) matches a
BARE `$VAR` only. A redirect commonly carries a suffix (`> "$D/a.txt"`), so wire
the redirect sink to a twin (`_is_transitive_ce_scratch_redirect_target`) that
reuses `_MKTEMP_SCRATCH_TARGET_RE` (`$VAR`/`${VAR}` + optional `..`-free tail,
optional surrounding quote) and the SAME `_value_roots_at_scratch` +
`_last_real_assignment_value`. This routes `/tmp`, mktemp and `.pilot-scratch`
through one resolver at the redirect sink too — an alignment with cpp#265, not a
widening of admission.

### 5. Flip lethality, never admission — and prove the line did not move

The exemption changes exactly one bit: `_denial_is_terminal` returns `False`.
Everything new is reached ONLY under `for_lethality` — the two transitive
predicates and `_value_roots_at_scratch` are called only from
`permissions._destination_veto_reason`'s `for_lethality`-gated clauses. So
`is_tier1_auto_approve`, `is_tier3_dangerous`, every YAML rule, egress, and
`_destination_veto_reason(for_lethality=False)` are byte-identical to HEAD. A
survivable deny is still a deny: the command stays refused and the pilot routes
around it. Pin this with a dedicated admission-identity test and an empirical
broad-sample diff against the served code (0 diffs here, 28 commands).

## References

- Code: `src/claude_pilot/tier1.py` (`_is_pilot_scratch_rel`,
  `_PILOT_SCRATCH_REL_RE`, `_PWD_PREFIX_RE`; `_value_roots_at_scratch` extended
  with the `.pilot-scratch` base case + `$PWD/` prefix + optional `cwd`
  fs-containment via `is_within_pilot_scratch`;
  `_is_transitive_ce_scratch_redirect_target`, the redirect twin of
  `_is_transitive_ce_scratch_mkdir_target`), `src/claude_pilot/permissions.py`
  (the `bash-mkdir` and `bash-redirect` `for_lethality` clauses now pass `cwd`).
- Siblings: cpp#213 (`.pilot-scratch` rm prefix carve — source of
  `is_within_pilot_scratch`), cpp#201 (mktemp redirect SOURCE carve),
  cpp#265/#266 (transitive scratch derivation, LAST-WINS), cpp#270/#268 (derived
  rm/mkdir on a variable + the INVERTED reads-only reassignment rule — inherited
  unchanged). Doctrine cpp#38/#213 (worktree-relative containment, symlink-aware),
  cpp#143 (never resolve to GRANT a SYSTEM root), cpp#205 (default survivable),
  cpp#250 (ReDoS).
- Plan: `docs/plans/2026-10-02-001-fix-272-pilot-scratch-var-derived-lethality-plan.md`.
- Ticket: cpp#272 / mika#2623 (pilot c722b251). n=2 of the "disposable git repro"
  class (after 93bac846, mika#2626 / cpp#268).
