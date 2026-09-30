# A `<`/`>` inside a literal-quoted heredoc body is not a redirection (cpp#241)

## Symptom

Two pilots died within three hours on the same shape — #2590 (7cd3ce9a,
17:46:10Z, mika#2590) and #1990 (2bf1c7f3, 20:43:37.463Z) — each on an
interpreter reading a **quoted-delimiter heredoc**:

```
cd crates/mika-agent && python3 - <<'PY'
import re, pathlib
# … a script that regex-edits Rust source:  -> Vec<T>,  None::<…>,  if a > b:
PY
```

The refusal is correct (the heredoc-to-interpreter form is not chain-safe, so the
command is DENY), but the denial was made **TERMINAL** and ended the run. Root
class: **any** heredoc whose delimiter is quoted/escaped and whose body contains a
`<`, `>` or `>>` — which Rust/C++/TS bodies (`-> Vec<T>`, `a > b`, `None::<…>`)
routinely do. `python3 - <<'PY' … PY`, `node - <<'JS' … JS`,
`ruby - <<'RB' … RB` are all affected.

## Why (verified at source, e534db6)

Because the delimiter is **quoted** (`<<'PY'`), bash performs **no expansion** on
the heredoc body: every line up to the terminator is fed **verbatim** to the
interpreter on stdin. A `<`/`>` in that body is therefore pure **data**.

But `_denial_is_terminal`'s redirect/destination vetoes
(`_redirect_destination_veto_reason` and `_destination_veto_reason(…,
for_lethality=True)`) do **not** model the heredoc body. They scan the whole
command string for redirect operators, so a body `>` — e.g. the `>` in
`if a > b:` — is read as an **outer-command redirection** whose target is the
next token (`b:`). That target is not a lexically contained path, so the
destination veto returns non-None and the denial becomes terminal.

Probe (real temp git worktree, before fix):

```
python3 - <<'PY'
if a > b:
    print(1)
PY
```

→ `_denial_is_terminal` **True**; fired predicate:
`_destination_veto_reason(…, for_lethality=True)` =
`"redirect destination 'b:' is not a literal contained path …"`. The *same*
heredoc with no `<`/`>` in the body → survivable. `is_tier3_dangerous_for_lethality`
stays False throughout (cpp#205 dropped the generic `>` from the lethality verb
set); the lethality is entirely the phantom-redirect destination veto.

This is the **cpp#236 family** (a `<`/`>` that is not a real outer redirect), but
the cpp#236 mask (`_mask_lethality_redirect_chars`) models quotes and
command/process substitutions, **not** heredoc bodies.

## Fix (lethality only, admission byte-identical)

Add a heredoc-aware pre-pass to the lethality path that blanks every `<`/`>`
inside the **body** of a heredoc whose delimiter is **quoted or escaped**
(`<<'D'`, `<<"D"`, `<<\D` — bash disables expansion, so the body is inert data).
Reuses bash's own heredoc grammar: openers are found quote-aware on command lines,
bodies run to a line equal to the bare delimiter (leading tabs stripped for
`<<-`), and heredocs are consumed in bash's order.

- `tier1._parse_heredoc_opener` / `_scan_command_line_for_heredocs` — recognize a
  heredoc opener and its delimiter/quoting on a command line, quote-aware.
- `tier1._mask_lethality_heredoc_redirect_chars(command)` — blank `<`/`>` in
  quoted-heredoc body lines; length-preserving; purely lexical `(str) -> str`.
- `tier1._needs_lethality_heredoc_mask(command)` — gate on `"<<" in command`.
- `permissions._denial_is_terminal` — after the raw-command verb check and the
  cpp#236 substitution mask, also apply the heredoc mask to `veto_command` when
  the gate fires; otherwise byte-identical to HEAD.

Fail-closed so no **real** outer redirect is ever exempted:

- The **opener line is never masked**, so a genuine redirect on it
  (`python3 - <<'PY' > /etc/passwd`) stays terminal.
- An **unterminated** heredoc (no later line equals the bare delimiter) returns
  the command unchanged. The `python3 - <<'PY' … PY > /etc/passwd` shape never
  closes (the line `PY > /etc/passwd` is not the bare-`PY` terminator bash
  requires), so it is returned raw and its `> /etc/passwd` stays terminal.
- An **unquoted** heredoc body (`<<PY`) is left raw — bash expands it, so its
  `$(curl …)`/backticks are real; that shape stays terminal. `curl … | python3`
  is untouched.

Invariants: admission (`is_tier3_dangerous`, `TIER3_PATTERNS`, tier1,
`is_tier3_dangerous_for_lethality`) untouched — the command stays DENY, only
`_denial_is_terminal` flips. A destructive verb that merely appears as inert body
text (`rm -rf` inside the script) is handled exactly as on HEAD: the verb check
runs on the raw command **before** this mask, which only touches `<`/`>`.

## Lesson

A quoted heredoc body is `stdin` **data**, not shell syntax; any classifier that
scans the flat command string will read its `<`/`>` as redirections. The sound
fix models bash's heredoc grammar (delimiter quoting decides expansion; the body
span runs to the bare-delimiter terminator) and masks only within it — and, per
cpp#205, a `>` a classifier cannot prove is a real redirect is the
"could-not-prove" class that defaults to **survivable**, not fail-closed-toward-lethal.
The same masking direction as cpp#236, applied to the one context cpp#236's
substitution parser did not cover.
