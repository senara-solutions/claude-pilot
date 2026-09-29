---
title: "A read filter and an in-worktree source edit are refused without being lethal"
date: 2026-09-28
last_updated: 2026-09-28
module: claude_pilot.tier1
component: permission-classifier
problem_type: design_decision
category: tooling-decisions
severity: high
tags: [permissions, policy, lethality, denial, sed, eval, read-filter, in-worktree, containment, fail-closed, claude-pilot-203, claude-pilot-205, claude-pilot-213, mika-2573, mika-2565]
applies_when: "carving a survivable-deny exemption for a read/filter pipeline or an in-worktree source edit that a syntactic classifier over-refuses"
---

# A read filter and an in-worktree source edit are refused without being lethal

## Context

cpp#205 ratified the doctrine: `_denial_is_terminal` defaults SURVIVABLE, and
TERMINAL is reserved to PROVEN danger. A denial whose cause is pure FORM — a
classifier that could not parse or prove a target safe — is a refusal, not a
reason to end the run. cpp#130/#154/#155/#157/#196/#201/#203/#209/#213 each
carved one over-refusal shape at a time. Two more killed pilots the night of
2026-09-27:

- **mika#2573 (case A, session 8a3eb33b)** — a read/filter pipeline:
  `cargo test -p mika-agent --test eval <name> 2>&1 | sed -n '/running 1 test/,/test result/p' | head -40`.
  Refused AND terminal (`[bash-cargo] (terminal)`).
- **mika#2565 (case B, session 6d61c747)** — an in-worktree source edit:
  `sed -i '5870,5990s/classify_dependabot_verdict(/classify_2519(/' crates/mika-agent/src/evidence/guards.rs && grep -n "…" …/guards.rs`.
  Refused AND terminal (`[bash-grep] (terminal)`).

Both are ordinary read/edit shapes. Neither writes outside the worktree; neither
executes an untrusted verb. The refusal is fine (a bare `sed -i` is not an
allow-listed idiom, so the pilot must fall back to the Edit tool); only the
lethality was wrong.

## Guidance

### 1. Probe the ACTUAL terminal predicate at the source — the obvious cause can be the wrong one

Case B is what it looks like: `TIER3_PATTERNS`' `sed -i` verb entry matches the
flag regardless of target, so `is_tier3_dangerous_for_lethality` is `True` and
the denial is terminal. The fix is an in-worktree containment carve (below).

Case A is NOT what it looks like. The command reads like "a print-only `sed`
made it terminal," but a probe in a real temp git worktree shows the print-only
`sed -n '/running 1 test/,/test result/p'` is `False` on its own — the killer is
`TIER3_PATTERNS`' `\beval\s` entry matching the bareword **`eval`** in
`cargo test --test eval` (the name of the cargo integration-test target), not
the shell `eval` builtin. A print-only-sed carve does not touch it. The lesson:
run `_denial_is_terminal` / `is_tier3_dangerous_for_lethality` on the VERBATIM
command in a real worktree and identify which pattern fires before designing the
fix — a plausible diagnosis of a compound command is not the measured one.

### 2. A print-only `sed` segment is a read filter — blank it before the verb scan

`sed -n '<addr>[,<addr>]p'` (the closed-world `_is_safe_sed_print_only` shape:
`-n` sole flag, one address/range + bare `p`, no `-i`, no `w`/`W` write) reads
and prints. It writes and executes nothing, yet the LITERAL text of its print
script is scanned by the verb patterns, so `… | sed -n '/rm -rf/p'` trips the
very verb it searches for. `_blank_print_only_sed_segments` drops each such
segment (reusing `_is_safe_sed_print_only` verbatim) before the scan, mirroring
cpp#213's segment-drop-then-recheck: survivors keep their full text, so real
danger outside the sed still matches. LETHALITY only — `is_tier3_dangerous` (the
REFUSAL) never calls this.

### 3. An in-worktree `sed -i` source edit is contained — carve it fs-aware, fail-closed

`sed_i_confined_to_worktree` mirrors `rm_confined_to_pilot_scratch` exactly:
split the command, drop each `sed -i` **substitution** segment whose EVERY file
target is RELATIVE and resolves inside the worktree, and re-run the unchanged
`is_tier3_dangerous_for_lethality` on the remainder. Containment uses the same
`is_within_project` resolution the destination veto uses — symlink-aware,
cwd-bounded. Every rejected class stays terminal, fail-closed:

- an ABSOLUTE target (`/etc/x`) — rejected outright (the incident is relative);
- a `$`/`~`-rooted operand (`$HOME/x`, `~/x`) — the same anti-respelling
  disqualifier the redirect/cp-mv/mkdir vetoes apply, because `is_within_project`
  does no shell expansion (cpp#154 D3);
- a `..` traversal or outbound-symlink target — collapsed/resolved out of the
  worktree by `is_within_project`;
- a script that is not a single write-free substitution — a standalone `w`/`W`
  write command, an `s///w` write flag, `-e`/`-f` multi-script, or a `d`/`y`/…
  command all fail the positive `_SED_I_SAFE_SUBST_SCRIPT_RE` match and keep the
  segment terminal;
- a mixed target list or a chained destructive verb — the confined segment is
  dropped, but the unconfined danger survives in the remainder and fires.

Placed in `_denial_is_terminal` (cwd-aware), NOT in `is_tier3_dangerous_for_lethality`
(cwd-free) — so that function's own `sed -i realfile.rs → True` (cpp#203) is
unchanged; the lethality flip is decided one layer up where the worktree is known.

### 4. Flip lethality, never admission — and prove the line did not move

Both carves change exactly one bit: `_denial_is_terminal` returns `False`.
`is_tier3_dangerous` (the refusal), `is_tier1_auto_approve`, and every YAML rule
are byte-identical to HEAD and never consult the new code. Pin this with a
dedicated admission-identity test (`is_tier3_dangerous` still `True`,
`is_tier1_auto_approve` still `False`, policy still `deny`, handler returns a
non-terminal `PermissionResultDeny`) alongside the lethality assertion. Never
touch the tier1 gate `is_tier1_auto_approve`.

### 5. When the classifier blocks the guardrail edit, hand it back — do not route around

The lowercase truth of a survivable-carve is that it narrows a danger predicate,
and the Claude Code auto-mode classifier reads that as "Security Weaken." The
`eval`-command-position narrowing needed for case A (below) was blocked on that
basis. The discipline is: do not re-issue it in pieces, through another tool, or
via encoding — STOP, and hand back the exact patch so a human-validated apply
can finish (the same path cpp#2562/#218 took). The two `sed`-shaped carves in
this change were NOT blocked and landed normally.

## The residual: case A needs an `eval`-command-position narrowing (handed back)

`\beval\s` matches `eval` as an ordinary argument (`--test eval`, `--eval`),
where bash never invokes the builtin. For LETHALITY, `eval` is proven-dangerous
only at a COMMAND position — the first word of a segment, i.e. after `^` or a
shell separator (`|`, `&`, `;`, newline, `(`). `eval "$(x)"`, `x | eval y`,
`foo && eval x` stay terminal; `--test eval` / `--eval` become survivable. The
proposed change swaps only the `\beval\s` entry in the lethality tuple
(`_TIER3_VERB_PATTERNS_FOR_LETHALITY`), leaving `TIER3_PATTERNS` (admission)
byte-identical. It was blocked by the auto-mode classifier and is handed back for
human-validated apply; until it lands, the verbatim case A stays terminal.

## Related

- `docs/solutions/tooling-decisions/a-refusal-and-its-lethality-are-two-decisions-not-one.md` — the cpp#128 split both carves ride on.
- `docs/solutions/tooling-decisions/a-destructive-verb-under-a-designated-scratch-prefix-need-not-be-lethal.md` — cpp#213, the segment-drop-then-recheck sibling this reuses.
- claude-pilot#205 (default-survivable doctrine), #203 (`sed -i /dev/null`), #201/#209/#213 (carve-by-removal mechanism), mika#2573 (case A), mika#2565 (case B).
