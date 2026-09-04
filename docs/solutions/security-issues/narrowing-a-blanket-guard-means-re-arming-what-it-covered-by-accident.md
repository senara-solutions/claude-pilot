---
title: "Narrowing a blanket guard means re-arming what it was covering by accident — and resolve to withhold, never to grant"
date: 2026-09-05
module: claude_pilot.tier1
component: permission-classifier
problem_type: security_issue
category: security-issues
severity: critical
tags: [permissions, policy, bash, redirect, lethality, session-fatality, worktree-containment, control-plane, symlink, lexical-matching, path-resolve, blanket-pattern, negative-control, claude-pilot-38, claude-pilot-42, claude-pilot-128, claude-pilot-130, claude-pilot-143, claude-pilot-154, claude-pilot-155]
applies_when: "narrowing an over-broad pattern in a security gate, or writing any containment check that resolves a path"
---

# Narrowing a blanket guard means re-arming what it was covering by accident

## Context

claude-pilot sandboxes a headless LLM pilot that can be prompt-injected. Two
decisions are kept separate (see
[a-refusal-and-its-lethality-are-two-decisions-not-one](../tooling-decisions/a-refusal-and-its-lethality-are-two-decisions-not-one.md)):
`is_tier3_dangerous` decides the **refusal**, and
`is_tier3_dangerous_for_lethality` — consulted only by
`permissions._denial_is_terminal` — decides whether that refusal **ends the run**.

`TIER3_PATTERNS` (`src/claude_pilot/tier1.py`) carries one entry that is not a
verb at all:

```python
re.compile(r"(?<!<)>{1,2}(?!\(|&[\d-])"),   # > or >>
```

Any output redirect. cpp#130 had already carved `/dev/null` out of it for
lethality. cpp#154 carved out the rest of the *contained* targets, because three
pilot sessions on one ticket died in a single day (2026-09-04) on refusals whose
cause was pure **form** — a command chain the allow-list guard cannot honour —
while the command only wrote a working file. The last died in `/ce:work` after
155 turns and 8 pushed commits, with `gh pr create` one call away.

## The two lessons

### 1. A blanket pattern is load-bearing for hazards nobody wrote down

The `>` entry existed to stop arbitrary writes. But because it matched *every*
redirect, it was also — silently, and with no test naming it — the only thing
making these terminal:

| shape | the hazard it actually covers |
|---|---|
| `echo x > .git/hooks/pre-commit` | control plane (cpp#42): executes on next checkout |
| `echo x > .claude/settings.json` | control plane: the agent's own instructions |
| `echo x > esc/passwd` (`esc` -> outside) | worktree escape by symlink (cpp#38) |

None of these had a test. Every pre-existing control-plane test used
`git show … > …`, which `_segment_write_kind` classifies and which therefore
routes through `_destination_veto_reason` — a *different* code path that the
narrowing could not touch. So the entire class was structurally invisible to the
suite: the tests that looked like they covered it could not have failed.

**The rule.** Before narrowing a broad guard, enumerate every hazard it currently
catches — not every hazard it was *written* for. Ask of each: after the
narrowing, what still catches this? A hazard whose only remaining answer is "the
pattern I am about to narrow" must be re-armed explicitly, in the same change.

The tell that this is happening: the guard is *generic* (a syntax class like `>`,
a wildcard, a catch-all `else`) while the hazards are *specific*. Generic guards
accumulate unnamed dependents.

### 2. Resolving a path to GRANT an exemption is unsafe; resolving to WITHHOLD one is not

This one has a scar. cpp#143 added a `/tmp` scratch exemption for `mkdir` and its
first version resolved the destination with `Path.resolve()` before testing `/tmp`
membership. That **broke the cpp#38 symlink-escape tests**: a worktree symlink
crafted as `esc -> ../../../tmp` resolved into `/tmp` and got exempted, although
the pilot had never spelled `/tmp` anywhere in the command. The fix was to match
the **literal operand text**, and the reasoning is preserved in the source
(`permissions.py:922-968`).

cpp#154 inherited that rule and it looked like a contradiction: the containment
re-arm above *needs* to resolve, because `esc/passwd` and `notes.txt` are
lexically identical. They are not in contradiction, because the direction differs:

```
resolve -> then GRANT an exemption      UNSAFE
    the attacker controls the input, so they control what it resolves TO,
    and a favourable resolution buys them the exemption.

resolve -> then WITHHOLD an exemption    SAFE
    the worst case is a command that stays refused / stays terminal.
    An attacker cannot profit from making their own command more restricted.
```

So the shape that satisfies both: keep the **exemption** test purely lexical and
input-free (no `resolve`, no `stat`, no `cwd` — the signature stays
`(command: str) -> bool`), and put the resolving check in a **separate function
on the withholding side only**:

```python
# permissions._denial_is_terminal
if is_tier3_dangerous_for_lethality(command):      # lexical, cwd-free, grants
    return True
if _redirect_destination_veto_reason(command, cwd) is not None:  # resolves, withholds
    return True
return _destination_veto_reason(command, cwd) is not None
```

### 3. Corollary — re-arm where the blast radius is smallest

The "clean" fix was to teach `_segment_write_kind` about redirects, so the
existing `_destination_veto_reason` would see them. That was rejected and
deferred (claude-pilot#155): `_segment_write_kind` is *also* consulted on the
**allowed** path, where a redirect write-kind would veto `cat > /tmp/x <<'EOF'`
and regress a working allow rule. The narrower re-arm is reachable only from
`_denial_is_terminal`, so no allowed command can be affected by it at all.

When re-arming, prefer the call site with the fewest consumers, even if a shared
helper looks tidier. Tidiness that widens blast radius is not tidiness.

## How this was caught

Five independent reviewers, run in parallel over the diff, **all five** landed on
the control-plane hole. None of the pipeline's own gates did: `ruff`, `mypy` and
1011 green tests all passed on the version that had it, because the missing test
was missing on `main` too. A hole that predates your diff will not show up as a
regression — only as a question nobody asked.

## Prevention

**Pin every guard with a negative control that goes red when the guard is
removed.** Not "the suite is green" — one deliberate red per guard. cpp#154 ships
four, and each one names the exact edit that produces it:

| remove this | goes red |
|---|---|
| the containment strip (the feature itself) | 6 tests — the 3 real deaths + 3 paired controls |
| the control-plane / escape re-arm | 3 tests |
| the leading-`$` rejection (`$HOME/x` == `~/x`) | 2 tests |
| `[ \t]*` -> `\s*` between operator and target | 1 test |

That last one is worth its own line as a lexical-gate hazard: `\s*` matches
newlines, so a line-final `>` took the **next line's first token** as its target.
`"echo done >\nbash -c 'id'"` stripped to `"echo done   -c 'id'"` and lost the
`bash -c` match. **Blanking a redirect must never blank a verb** — in a
whitespace-tolerant pattern over a command string, spell horizontal whitespace
explicitly.

And when a pre-existing test asserts the very behaviour your ticket exists to
change, that is a **specification supersession, not a regression** — but say so
out loud rather than quietly editing it. Here `assert …("mika ask > /tmp/exfil")
is True` was a cpp#130 assertion that cpp#154 inverts by design. It was escalated
to the architect before the edit, ratified, migrated (not rewritten in place)
into the new suite where the changed verdict is visible, and the ratification
recorded in the PR. The escalation is not ceremony: the question "what *else*
should have flipped and did not?" is what surfaces holes like the control-plane
one.
