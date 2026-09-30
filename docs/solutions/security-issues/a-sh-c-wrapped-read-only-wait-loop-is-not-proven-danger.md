---
title: "A `sh -c`-wrapped read-only wait-loop is not proven danger — the wrapper's lethality is the script's"
date: 2026-09-30
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: high
tags: [permissions, policy, bash, lethality, sh-c, wrapper, wait-loop, read-only, containment, cpp205, admission, arithmetic-expansion, command-substitution, claude-pilot-237, claude-pilot-205, claude-pilot-213, mika-2105]
applies_when: "a lethality verb-ban fires on a shell WRAPPER (`sh -c`/`bash -c`) whose wrapped script is provably read-only, so the denial is killed instead of merely refused"
---

# A `sh -c`-wrapped read-only wait-loop is not proven danger

## The death

Pilot #2105 (e1a6c78b) died **TERMINAL** at 2026-09-29T15:13:20.327Z on a pure
read-only wait-loop that polls a scratch file:

```
sh -c 'n=0; while [ $n -lt 55 ]; do if [ -f .pilot-scratch/measures.txt ]; then cat .pilot-scratch/measures.txt; exit 0; fi; sleep 10; n=$((n+1)); done; du -sm target; tail -1 .pilot-scratch/cold0.log'
```

Every op is read-only (`[ -f … ]`, `cat`, `du -sm target`, `tail -1`, `sleep`,
arithmetic `n=$((n+1))`, `exit`); every file target is relative under the worktree
(`.pilot-scratch/…`, `target`). No destructive verb, no out-of-worktree write, no
network, no dangerous `eval`/substitution.

## The predicate that made it terminal (proven at source, HEAD c136814)

`_denial_is_terminal`'s FIRST gate is
`is_tier3_dangerous_for_lethality(command)`, and that returns **True** because the
`\bsh\s+-c\b` entry of `TIER3_PATTERNS` (`tier1.py:201`) — carried into the
cpp#205 lethality verb set `_TIER3_VERB_PATTERNS_FOR_LETHALITY` — matches
`sh -c` at span 0..5 of the stripped command. Nothing else contributes:
`rm_confined`/`sed_confined` are False, and both destination vetoes
(`_redirect_destination_veto_reason`, `_destination_veto_reason(…,
for_lethality=True)`) return None. The **sole** cause of lethality is the `sh -c`
verb.

The candidate hypotheses were refuted at source, not assumed: the arithmetic
`$((n+1))` is **not** read as a `$(` command substitution (it is single-quoted and
never reaches any redirect/substitution mask); `du`/`cat`/`tail` are not treated
as writes; no `while`/`done`/`;` token and no redirect contributes. The `sh -c`
wrapper alone is the killer.

## Why the wrapper is different from `rm -rf`

`rm -rf` is in `TIER3_PATTERNS` because a syntactic classifier cannot prove the
target of a recursive delete safe — the **verb** is proven-dangerous regardless of
what it points at (cpp#205's founding reasoning). `sh -c`/`bash -c` are in that
tuple for a different reason: a wrapper can **smuggle** any command. But a
wrapper is not intrinsically destructive — **its lethality is exactly the
lethality of the script it wraps.** When that script is a read-only wait-loop,
there is no proven danger, so under cpp#205 (terminal reserved to proven danger)
the denial must be survivable, not fatal.

## The fix — a lethality-only recognizer (sibling of cpp#213 / mika#2565)

`is_readonly_waitloop_script(command)` (`tier1.py`, **new**, purely lexical, no
`cwd`/filesystem) recognizes the safe class and is added to the tier3 lethality
gate exactly like the `rm`/`.pilot-scratch` (cpp#213) and `sed -i` (mika#2565)
carves:

```python
if (
    is_tier3_dangerous_for_lethality(command)
    and not rm_confined_to_pilot_scratch(command, cwd)
    and not sed_i_confined_to_worktree(command, cwd)
    and not is_readonly_waitloop_script(command)   # cpp#237
):
    return True
```

It unwraps a **single** `sh -c '<script>'` / `bash -c "<script>"` (nothing after
the closing quote, else fail-closed), then requires:

1. **No command substitution** — reject `` ` ``, `$(cmd)`, funsub `${ …}`, `$'…'`.
   Inert arithmetic `$((…))` **with no nested `$`** is blanked to `0` *before* the
   `$(` check, so a counter (`n=$((n+1))`) is admitted while a substitution
   smuggled into arithmetic (`$(( $(cmd) ))`) survives the blank and is rejected.
2. **No pipe / background / subshell / redirect metacharacter** (`| & < > ( )`).
3. **A real loop** — `while`/`until`/`for` + `do` + `done`.
4. Every `;`/newline statement (split **quote-aware**) is a read-only simple
   command — leading word in a closed allowlist (`[`/`test`, `cat`, `head`,
   `tail`, `du`, `ls`, `wc`, `sleep`, `exit`, `:`, `true`, `false`, `echo`,
   `printf`, `grep`, `stat`, `dirname`, `basename`), a `for NAME in <list>` head,
   or a `NAME=value` assignment — whose every file operand is **worktree-relative**
   (rejects absolute, `..`, `~`, dangerous env vars `$HOME`/`$OLDPWD`/`$PWD`/…).

When the carve applies, the command falls **through** to the unchanged redirect/
destination vetoes below, which return None for a recognized loop (no `>`, no
out-of-worktree target by construction). The carve is a **no-op** on every
`for_lethality=False` call, so admission is byte-identical.

## SECURITY — fail-closed, admission never widened

The command **stays DENIED** and never executes — `is_readonly_waitloop_script`
is consulted ONLY by `_denial_is_terminal`, never by `is_tier3_dangerous`,
`is_tier1_auto_approve`, any YAML rule, or `is_tier3_dangerous_for_lethality`
itself. Proven byte-identical at source for the verbatim command:
`is_tier3_dangerous`=True, `is_tier1_auto_approve`=False, `policy.decision`=deny.
Only `_denial_is_terminal` flips True→False.

Every dangerous shape is rejected by the recognizer, so the `sh -c` verb keeps
the denial **terminal** (probed at source with the wired gate):

| body of the `sh -c` wait-loop | recognized | stays terminal |
|---|---|---|
| `curl`/`wget` (network) | no | **yes** |
| `rm`/`rm -rf` (destructive) | no | **yes** |
| `cat x > /etc/passwd` / `> "$HOME/y"` (out-of-worktree write) | no | **yes** |
| `eval "$CMD"` | no | **yes** |
| `echo $(rm -rf /)` (command substitution) | no | **yes** |
| `x=$(( $(cat n) + 1 ))` (subst inside arithmetic) | no | **yes** |
| `cat x` piped to `sh` (pipe to shell) | no | **yes** |
| `rm -rf /etc` after the loop, still in `sh -c` | no | **yes** |

The **egress axis is untouched** (`HTTPS_PROXY= … curl` stays terminal). The
tier1 gate is never touched.

## The learning

**A lethality verb-ban that fires on a shell WRAPPER (`sh -c`/`bash -c`) is
over-broad: the wrapper's danger is the danger of the script it wraps, not the
wrapper itself.** Carve it the cpp#205 way — a purely-lexical, fail-closed
recognizer of the provably-safe wrapped class (here a read-only wait-loop with
worktree-relative targets), consulted ONLY on the `for_lethality` path so the deny
stays byte-identical and only its *terminality* flips. Prove the killer predicate
at source before writing the recognizer (the wrapper, **not** the arithmetic or a
loop token), and prove every dangerous body still terminal, both directions.

## References

- Doctrine: cpp#205 (survivable default, terminal reserved to proven danger;
  `_TIER3_VERB_PATTERNS_FOR_LETHALITY` = `TIER3_PATTERNS` minus the trailing
  generic-redirect catch-all).
- Mirrors: cpp#213 (`rm_confined_to_pilot_scratch`), mika#2565
  (`sed_i_confined_to_worktree`), cpp#209 (`for_lethality`-gated carve shape).
- Reuses the read-only-loop framing of cpp#92/#151
  (`_is_sanctioned_readonly_for_loop`, the admission-side for-loop recognizer).
- Preserves: cpp#154 D3 (`$HOME`-stays-terminal), cpp#38 (containment), the egress
  axis.
- Tickets: mika#2105 (the death, e1a6c78b), cpp#237 (this fix), cpp#205 (parent
  doctrine). Guardrail-edit escalation: cpp#223/#231.
