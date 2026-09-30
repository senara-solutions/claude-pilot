---
title: "A read-only process substitution is not proven danger — the `<(…)`'s lethality is its interior command's"
date: 2026-09-30
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, lethality, process-substitution, procsub, git-show, read-only, containment, cpp205, admission, redos, dev-null, claude-pilot-252, claude-pilot-205, claude-pilot-213, claude-pilot-237, mika-2252]
applies_when: "a lethality verb-ban fires on a process substitution `<(…)` whose interior command is provably read-only, so the denial is killed instead of merely refused"
---

# A read-only process substitution is not proven danger

## The death

Pilot #2252 (5cfd4bc8) died **TERMINAL** at 2026-09-30T13:52:26.824Z on a pure
read-only comparison that diffs a git blob against a restored worktree file:

```
diff <(git show HEAD:crates/mika-agent/src/tools/pr_merge_with_gate.rs) .pilot-scratch/pr_merge_with_gate.rs.orig >/dev/null 2>&1; echo "--- vérification que la restauration est complète ---"; cargo t
```

`<(git show HEAD:<path>)` reads a git blob; `diff` compares it to a worktree file;
the output is discarded (`>/dev/null 2>&1`); then an `echo` and `cargo t`. No
destructive verb, no out-of-worktree write (`/dev/null` is not an exfiltration),
no network — `<(git show …)` only reads.

## The predicate that made it terminal (proven at source, HEAD 216033d)

`_denial_is_terminal`'s FIRST gate is `is_tier3_dangerous_for_lethality(command)`,
and that returns **True** because the `<\(` entry of `TIER3_PATTERNS`
(`tier1.py:229`) — carried into the cpp#205 lethality verb set
`_TIER3_VERB_PATTERNS_FOR_LETHALITY` (`= TIER3_PATTERNS[:-1]`) — matches the `<(`
of the process substitution. Nothing else contributes: `rm_confined`/`sed_confined`/
`is_readonly_waitloop_script` are False, and both destination vetoes
(`_redirect_destination_veto_reason`, `_destination_veto_reason(…,
for_lethality=True)`) return None. The **sole** cause of lethality is the `<(`
process substitution.

The candidate hypotheses were refuted at source, not assumed: the `>/dev/null` is
**stripped** by `_STDOUT_DEVNULL_RE` (cpp#130) before the pattern check, so the
redirect is **not** the killer (both vetoes return None); the `bash-grep`/`diff`
rule interaction never fires because the verb pattern trips *before* any veto. The
`<(` alone is the killer.

## Why the process substitution is different from `rm -rf`

`rm -rf` is in `TIER3_PATTERNS` because a syntactic classifier cannot prove the
target of a recursive delete safe — the **verb** is proven-dangerous regardless of
what it points at (cpp#205's founding reasoning). `<(`/`>(` are in that tuple for a
different reason: a process substitution can **smuggle** any command in its
interior. But a process substitution is not intrinsically destructive — **its
lethality is exactly the lethality of the command inside it.** When every
substitution is a read-only *input* `<( CMD … )` whose interior `CMD` is in a closed
read list, there is no proven danger, so under cpp#205 (terminal reserved to proven
danger) the denial must be survivable, not fatal.

## The MPC-ratified gate (2026-09-30)

Admitted interiors inside `<( )` = a **CLOSED list of READ commands**: `git show`,
`git diff`, `cat`, `printf`. (`git diff` is IN; `echo` is NOT — the ticket draft
listed git-show/cat/printf/echo, MPC replaced `echo` with `git diff`.) Every
`>( … )` OUTPUT substitution, every `<( … )` interior outside the list
(`<(curl …)`, `<(bash …)`, `<(sh -c …)`, `<(eval …)`, `<(rm …)`, `<(python3 …)`),
and every command substitution (`` ` ``/`$(`) stays terminal. Plus a bounded-time
(< 50 ms) recognizer on a pathological input (the cpp#250 ReDoS lesson).

## The fix — a lethality-only recognizer (sibling of cpp#213 / mika#2565 / cpp#237)

`readonly_procsub_survivable(command)` (`tier1.py`, **new**, purely lexical, no
`cwd`/filesystem) recognizes the safe class and is added to the tier3 lethality
gate exactly like the `rm`/`.pilot-scratch` (cpp#213), `sed -i` (mika#2565), and
wait-loop (cpp#237) carves:

```python
if (
    is_tier3_dangerous_for_lethality(command)
    and not rm_confined_to_pilot_scratch(command, cwd)
    and not sed_i_confined_to_worktree(command, cwd)
    and not is_readonly_waitloop_script(command)
    and not readonly_procsub_survivable(command)   # cpp#252
):
    return True
```

Mechanism mirrors cpp#213/#2565: a single quote-aware left-to-right scan
(`_readonly_procsub_masked`) blanks every **admitted** `<( CMD … )` span, and the
remainder is re-checked with the **unchanged** `is_tier3_dangerous_for_lethality`,
so any OTHER proven-danger cause — a chained destructive verb
(`diff <(cat a) b && rm -rf /`), a second unadmitted substitution — still returns
True there and stays terminal. The interior is validated by a single
negated-char-class `fullmatch` (`_PROCSUB_READ_INTERIOR_RE`): a closed-list leading
word followed only by argument characters that cannot chain, redirect, substitute,
or open a nested substitution (`;`, `|`, `&`, `<`, `>`, `(`, `)`, `$`, backtick,
newline all excluded). One star, no nesting — **linear, no catastrophic
backtracking** (cpp#250).

When the carve applies, the command falls **through** to the unchanged redirect/
destination vetoes below. `>/dev/null` (and any worktree-relative target) passes
them (cpp#130) and stays survivable. A **real out-of-worktree redirect**
(`diff <(git show HEAD:x) a > /etc/passwd`, `> "$HOME/y"`) survives the recognizer's
re-check (cpp#205 dropped the bare-`>` catch-all from the lethality verb set) but is
**re-armed** by `_destination_veto_reason` on the FULL command, so it stays terminal.
The carve is a **no-op** on every `for_lethality=False` call, so admission is
byte-identical.

## SECURITY — fail-closed, admission never widened

The command **stays DENIED** and never executes — `readonly_procsub_survivable` is
consulted ONLY by `_denial_is_terminal`, never by `is_tier3_dangerous`,
`is_tier1_auto_approve`, any YAML rule, or `is_tier3_dangerous_for_lethality`
itself. Proven byte-identical at source for the verbatim command:
`is_tier3_dangerous`=True, `is_tier1_auto_approve`=False, `policy.decision`=deny.
Only `_denial_is_terminal` flips True→False.

Every dangerous shape is rejected, so the `<(` verb keeps the denial **terminal**
(probed at source with the wired gate):

| shape | recognized | stays terminal |
|---|---|---|
| `<(curl …)` / `<(wget …)` (network) | no | **yes** |
| `<(bash …)` / `<(sh -c …)` / `<(python3 …)` (arbitrary exec) | no | **yes** |
| `<(eval …)` | no | **yes** |
| `<(rm -rf …)` (destructive interior) | no | **yes** |
| `>(tee out)` (OUTPUT process substitution) | no | **yes** |
| `<(echo …)` (echo NOT admitted, MPC dropped it) | no | **yes** |
| `diff <(cat a) b && rm -rf /` (chained destructive verb) | no | **yes** |
| `<(git show HEAD:x; rm -rf /)` (danger inside interior) | no | **yes** |
| `<($(evil))` / `<(cat \`evil\`)` (command substitution) | no | **yes** |
| `diff <(git show HEAD:x) a > /etc/passwd` / `> "$HOME/y"` (real out-of-worktree redirect) | yes (procsub safe) | **yes** (re-armed by destination veto) |

The **egress axis is untouched**. The tier1 gate `is_tier1_auto_approve` is never
touched. `>/dev/null` is recognized as a non-write without opening a write door.

## The learning

**A lethality verb-ban that fires on a process substitution `<(…)`/`>(…)` is
over-broad: the substitution's danger is the danger of its interior command, not the
`<(` operator.** Carve it the cpp#205 way — a purely-lexical, fail-closed,
**linear** recognizer of the provably-safe class (here `<( CMD … )` whose interior
leading word is in a closed READ list), consulted ONLY on the `for_lethality` path
so the deny stays byte-identical and only its *terminality* flips. Prove the killer
predicate at source before writing the recognizer (the `<(`, **not** the
`>/dev/null` or the `diff`/`bash-grep` rule), keep recognition linear (no
nested-quantifier regex — cpp#250), and prove every dangerous interior/output-form
still terminal, both directions. Blank the admitted substitutions and re-run the
unchanged lethality classifier so a chained danger or a real out-of-worktree
redirect is caught downstream, not by the carve itself.

## References

- Doctrine: cpp#205 (survivable default, terminal reserved to proven danger;
  `_TIER3_VERB_PATTERNS_FOR_LETHALITY` = `TIER3_PATTERNS` minus the trailing
  generic-redirect catch-all).
- Mirrors: cpp#213 (`rm_confined_to_pilot_scratch`), mika#2565
  (`sed_i_confined_to_worktree`), cpp#237 (`is_readonly_waitloop_script`), cpp#209
  (`for_lethality`-gated carve shape).
- Reuses: cpp#236 (substitution-aware redirect mask), cpp#130 (`/dev/null` is a
  non-write via `_STDOUT_DEVNULL_RE`), cpp#157 (quoted `<`/`>` are not operators).
- ReDoS lesson (no nested-quantifier regex, linear recognition): cpp#250.
- Preserves: cpp#154 D3 (`$HOME`-stays-terminal), cpp#38 (containment), the egress
  axis.
- Tickets: mika#2252 (the death, 5cfd4bc8), cpp#252 (this fix), cpp#205 (parent
  doctrine). Guardrail-edit escalation: cpp#223/#231.
