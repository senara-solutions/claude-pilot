# A quoted `>` in a commit trailer `<email>` is not a redirection (cpp#236)

## Symptom

Pilot 946d2786 (mika#2578) died on a benign, standard-shape commit:

```
git commit -m "$(printf '%s\n' 'fix: …' '' 'Corps…' '' \
  'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>' \
  'Claude-Session: https://claude.ai/code/session_…')"
```

The refusal is correct — the `$(printf …)` substitution is not chain-safe, so the
command is `bash-git-stage-commit:chain-veto` DENY — but the denial was TERMINAL and
ended the run. Root class: **any** pilot commit whose message carries a
`Name <email>` trailer built through `$(printf …)` with the POSIX literal-apostrophe
idiom `'"'"'`.

## Why (verified at source, fef8b93)

`_denial_is_terminal`'s third check, `_destination_veto_reason(…, for_lethality=True)`,
extracts redirect targets after masking quoted `<`/`>` via `_mask_quoted_redirect_chars`,
which rests on the shared **flat** scanner `_quote_spans`.

bash parses `$(…)` **recursively**, re-scoping the quotes inside the substitution. The
`'"'"'` idiom embeds a `"` inside the outer `"$( … )"`. A flat left-to-right scanner
cannot follow that — and this is not specific to our scanner: Python's `shlex` raises
`No closing quotation` on the exact input. With an odd count of `'"'"'`, `_quote_spans`
desyncs: it reports the trailing region **unterminated** *and* leaves the trailer `>` in
a region it believes is **unquoted** (between spans). On the unterminated signal,
`_mask_quoted_redirect_chars` returns the command **unchanged** (cpp#157 D5's
fail-closed-toward-lethal choice), so the trailer `>` stays visible and leaks into the
destination veto as a **phantom** out-of-worktree redirect target → terminal.

`is_tier3_dangerous_for_lethality` stays False throughout (cpp#205 dropped the generic
`>` from the lethality verb set); the lethality is entirely the phantom-redirect veto.

## Two premises this corrected

1. The cause is neither `$(…)` nor `printf` — it is the `TIER3_PATTERNS` redirect
   pattern `(?<!<)>{1,2}` matching the `>` that closes `<email>`. Short variants without
   the `<email>` trailer were already survivable.
2. "Tokenize with `shlex`, treat `>` as a redirect only in unquoted tokens" does **not**
   work: shlex fails to parse `'"'"'`-inside-`$(…)` identically to `_quote_spans`. Nor
   does "also mask the unterminated trailing region": the trailer `>` lands *between*
   spans (phantom-unquoted), not inside the unterminated one.

## Fix (lethality only, admission byte-identical)

Mask must track substitution context explicitly. A `<`/`>` inside `$(…)` / backtick /
`<(…)` / quotes is never an **outer**-command redirect. A context-stack parser
(TOP / DQ / SQ / SUB-with-paren-depth / BT) succeeds where every flat scanner fails,
because it re-opens quoting on entry to `$( … )`.

- `tier1._mask_lethality_redirect_chars(command)` — blanks every non-TOP-level `<`/`>`,
  keeps real outer redirect operators.
- `permissions._denial_is_terminal` — feeds the two redirect/destination vetoes
  `veto_command = _mask_lethality_redirect_chars(command)` **only** when
  `"$(" in command or "\`" in command or _has_unterminated_quote(command)`; otherwise the
  raw command (byte-identical to today).

Invariants: admission (`is_tier3_dangerous`, `TIER3_PATTERNS`, tier1,
`is_tier3_dangerous_for_lethality`) untouched — the commit stays DENY, only
`_denial_is_terminal` flips. Proven dangers stay terminal via check #1 on the raw text
(`$(rm -rf x)`, `$(eval …)`, `<(curl x)`) and via mkdir/cp/mv destination vetoes
(`mkdir /etc/evil`). Real unquoted redirects stay terminal (`> /etc/passwd`,
`>> ~/.bashrc`, `> "$HOME/…"`, and `echo "$(date)" > /etc/passwd`).

## Lesson

No flat scanner (ours or `shlex`) can attribute a `>` to the outer command across a
`$(…)` boundary; the only sound classifier tracks substitution context. And per cpp#205,
a `>` a classifier cannot even prove is a redirect is the "could not parse/prove" class
that defaults to **survivable**, not the fail-closed-toward-lethal default cpp#157 chose
before the doctrine inverted.
