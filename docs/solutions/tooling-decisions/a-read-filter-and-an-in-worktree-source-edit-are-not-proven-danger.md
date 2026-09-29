---
title: "A read filter and an in-worktree source edit are refused without being lethal"
date: 2026-09-28
last_updated: 2026-09-29
module: claude_pilot.tier1
component: permission-classifier
problem_type: design_decision
category: tooling-decisions
severity: high
tags: [permissions, policy, lethality, denial, admission, sed, eval, read-filter, in-worktree, containment, fail-closed, claude-pilot-203, claude-pilot-205, claude-pilot-213, claude-pilot-234, claude-pilot-235, mika-2573, mika-2565]
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

## Case A, LANDED: an `eval`-command-position narrowing (cpp#231, mika#2573)

`\beval\s` matches `eval` as an ordinary argument (`--test eval`, `--eval`),
where bash never invokes the builtin. For LETHALITY, `eval` is proven-dangerous
only at a COMMAND position — the first word of a segment, i.e. after `^` or a
shell separator (`|`, `&`, `;`, newline, `(`). The fix swaps ONLY the `\beval\s`
entry in the lethality tuple (`_TIER3_VERB_PATTERNS_FOR_LETHALITY`) for
`_EVAL_COMMAND_POSITION_RE = (?:^|[|&;\n(])\s*eval\s`, leaving `TIER3_PATTERNS`
(admission) byte-identical. Verified: `eval "$(x)"`, `x | eval y`, `foo && eval x`,
`foo; eval x`, `(eval x)` stay terminal; the verbatim case A and `--test eval` /
`--eval` become survivable; `is_tier3_dangerous` (the REFUSAL) is unchanged.

The edit to `tier1.py` was refused by the Claude Code auto-mode classifier
(`[Self-Modification]` — the pilot editing its own permission logic) and was
applied under human (Vincent) validation via Remote Control, the same escalation
path cpp#223 used. Lethality only; no admission change.

## ONE definition, both layers, WIDENED anchor (cpp#234 + cpp#235)

Case A (above) left the two layers holding TWO different definitions of "`eval`
at command position": lethality used the narrowed `_EVAL_COMMAND_POSITION_RE`,
but admission (`TIER3_PATTERNS`) still used the bare `\beval\s`. Two immediate
consequences fell out of that split, and both are fixed by collapsing it to ONE
shared pattern object referenced by both layers.

**cpp#235 — admission was still over-refusing.** Because admission kept
`\beval\s`, `cargo test -p mika-agent --test eval <name>` (and `--test=eval`,
`node --eval`) stayed REFUSED at admission even after case A made it survivable:
no pilot could run mika-agent's `eval` integration tests in the sandbox. The
fix (ratified by Vincent → Prime, an ADMISSION change) is to make admission
adopt the SAME command-position definition — one pattern object, used by both
`TIER3_PATTERNS` and `_TIER3_VERB_PATTERNS_FOR_LETHALITY`, never duplicated.

**cpp#234 — the anchor was too narrow for real invocations.** After #233,
four shapes where `eval` genuinely IS the builtin stopped being recognized as
command position, so a pilot attempting an arbitrary `eval` behind one of them
was refused but no longer terminated — a defense-in-depth regression. The
shared anchor is therefore WIDENED to also open on: a backtick (`` `eval … ` ``);
a command-opening keyword `then`/`do`/`else`/`elif` (at a word boundary, so a
word merely ENDING in one is not a false anchor) or `!`, followed by whitespace;
and an optional chain of exec prefixes `sudo`/`env`/`exec`/`command`/`nohup`/
`time`/`xargs` (each with optional `-flags`). The original anchors `^|[|&;\n(]`
are kept verbatim.

```python
_EVAL_COMMAND_POSITION_RE = re.compile(
    r"(?:^|[|&;\n(`]|(?:\b(?:then|do|else|elif)|!)\s+)"
    r"\s*"
    r"(?:(?:sudo|env|exec|command|nohup|time|xargs)(?:\s+-\S+)*\s+)*"
    r"eval\s"
)
```

The subtle non-regression: the `env` exec-prefix must match only `env … eval`,
never `env VAR= cargo …` (the proxy-disabling shape, a legitimate SEPARATE
refusal). It cannot: that command carries no `eval` token, so the anchor's
trailing `eval\s` never matches it, and its refusal stays on its own admission
axis (not tier3, not auto-approved), untouched. The `tier1` auto-approve gate
(`is_tier1_auto_approve`) is likewise untouched.

Verified (both `is_tier3_dangerous` AND `_denial_is_terminal`, real git
worktree): the three `--test eval` / `--test=eval` / `--eval` positives and
#233's founding piped positive are ADMITTED and survivable; `eval "$(…)"`,
`x | eval y`, `sudo eval`, `` `eval …` ``, `then eval`, `env eval`, `&& eval`,
`; eval`, `(eval …)` stay refused AND terminal. Same auto-mode block as case A;
handed back for a Vincent-validated apply (he approves #234/#235 alongside #236
in one manual session).

## Related

- `docs/solutions/tooling-decisions/a-refusal-and-its-lethality-are-two-decisions-not-one.md` — the cpp#128 split both carves ride on.
- `docs/solutions/tooling-decisions/a-destructive-verb-under-a-designated-scratch-prefix-need-not-be-lethal.md` — cpp#213, the segment-drop-then-recheck sibling this reuses.
- claude-pilot#205 (default-survivable doctrine), #203 (`sed -i /dev/null`), #201/#209/#213 (carve-by-removal mechanism), mika#2573 (case A), mika#2565 (case B).
