"""Tier 1 auto-approval filter. Port of src/tier1.ts.

Returns True if a tool request is safe to auto-approve without relaying to the
external agent. Security principle: deny-list first, conservative default.
When in doubt, return False (relay decides).

Note: Bash shell commands do NOT get path-containment checks (unlike
Write/Edit). Static analysis of shell redirect/copy targets is impractical;
only commands with no write side effects are safe-listed.

Quote-aware metacharacter scanning (mika#946, mika#944): backtick, ``$(`` and
``$'`` (ANSI-C quoting) rejection uses ``contains_unquoted_metacharacter()`` —
a character-state-machine that mirrors the Rust
``contains_unquoted_metacharacter`` in
``crates/mika-agent/src/server/permission_pre_classifier.rs``. Both sides
follow POSIX single-quote semantics (backslash is literal inside ``'...'``).
See the F5 sentinel comment in the Rust module for the cross-language coupling
contract.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


# ── Exec-si-contenu attestation (Vincent-ratified 2026-08-04) ────────────────
#
# The pilot subprocess sets `MIKA_PILOT_CONTAINED=1` when — and ONLY when — it
# runs under mika's dispatch-lib.sh Phase 2b bwrap wrapper (fs+kernel+env+net
# cut ALL active, with hostname-allowlist egress relay). See
# `mika/skills/bundled/_shared/dispatch-lib.sh::_run_pilot_sandboxed` for the
# emitter side. The attestation is:
#
#   * Not forgeable from inside the sandbox — bwrap uses `--clearenv` +
#     explicit `--setenv MIKA_PILOT_CONTAINED "1"`. Nothing from the host env
#     survives into the sandbox unless bwrap injects it; MIKA_PILOT_CONTAINED
#     is injected by dispatch-lib SOLELY in Phase 2b full mode.
#   * Not settable outside dispatch-lib — production pilots always launch via
#     dispatch-lib; dev / test invocations that skip dispatch-lib get the
#     Phase 2a fallback (fs cut only, no MIKA_PILOT_CONTAINED, no safe-exec).
#
# Under the attestation, the invariant "Exec autorisé SSI contenu" allows
# `<safe-exec>` primitives (node, python3) as leaf-effect tier1 commands —
# their arbitrary side-effects are bounded by the sandbox. Hors containment,
# these stay denied (invariant enforced).
def _is_pilot_contained() -> bool:
    """True iff the process runs under dispatch-lib.sh Phase 2b containment.

    Read at classify-time (per-decision), NOT at import — so a helper
    invoked outside the sandbox (e.g. unit tests, dev shells) sees False
    naturally. Env-var read is cheap; no caching required.
    """
    return os.environ.get("MIKA_PILOT_CONTAINED") == "1"


# DOCTRINE: LLM-classifier permission decision (mika#1733 AC2, mika#1193)
#
# Applies per senara-solutions/mika @
# crates/mika-agent/docs/permission-decision-protocol-2026-07-06.md §AC2:
#
#   "This agent structurally cannot do X" applies to pre-classifier engine
#   gates only, NEVER to LLM classifier decisions.
#
# THIS IS THE TIER-1 CLASSIFIER ENTRY POINT. Decisions here are POLICY
# (allowlist-based fast-path for read-only tools + safe-command shapes),
# NOT structural gates. Agents downstream (mika-dev, mika-qa) MUST NOT
# frame tier-1 denials as "structurally cannot" or "structural denial" —
# those framings are reserved for the pre-classifier structural gates in
# mika-agent (`validate_dispatch_readiness`, `is_unauthorized_webhook_dispatch`
# — see mika@crates/mika-agent/src/skills/executor.rs and
# mika@crates/mika-agent/src/webhook_dispatch.rs for canonical shape).
#
# Retirement reference: mika#1193 moved the `permission-policy` skill's
# classifier tiers from mika-agent into claude-pilot-py; this function
# is one of the three landing sites (tier1/tier2/tier3) named in cpp#83
# (this ticket) as needing the AC2 anchor.
def is_tier1_auto_approve(tool_name: str, tool_input: dict[str, Any], cwd: str) -> bool:
    if tool_name in ("Read", "Glob", "Grep"):
        return True

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return False
        return is_safe_bash_command(command)

    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        if not isinstance(file_path, str) or not file_path:
            return False
        return is_within_project(file_path, cwd)

    if tool_name == "Skill":
        skill = tool_input.get("skill", "")
        if not isinstance(skill, str):
            return False
        return skill.strip() in TIER1_SAFE_SKILLS

    return False


# ── Pipeline slash commands (Skill tool) ────────────────────────────────────

TIER1_SAFE_SKILLS: frozenset[str] = frozenset({
    # /mika pipeline entrypoint
    "mika",
    # CE workflow commands (short form)
    "ce:plan",
    "ce:work",
    "ce:review",
    "ce:compound",
    "ce:brainstorm",
    # CE workflow commands (fully-qualified form)
    "compound-engineering:ce-plan",
    "compound-engineering:ce-work",
    "compound-engineering:ce-review",
    "compound-engineering:ce-compound",
    "compound-engineering:ce-brainstorm",
    # CE utility commands
    "compound-engineering:resolve_todo_parallel",
    # Doc audit
    "mika-doc-audit",
})


# ── Deny-list ────────────────────────────────────────────────────────────────
#
# TIER3 is a "deny these even though tier1 would otherwise pass them" list,
# NOT the safety boundary. The allow-list (SAFE_SHELL_COMMANDS + per-command
# sub-feature guards) is the safety boundary. TIER3 catches known-dangerous
# patterns in commands that would otherwise pass tier1's allow-list.
# If a TIER3 entry is the SOLE protection against a tier1-allowed command's
# sub-feature (e.g., relying on `rm -rf` substring to block
# `awk 'BEGIN{system("rm -rf ~")}'`), the allow-list is misshapen — fix the
# allow-list, not the denylist. cpp#27 was an instance: awk + sed were dropped
# from SAFE_SHELL_COMMANDS because their sub-feature exec routes can't be
# exhaustively guarded.

# Strip universal stderr/stdout silencing (`2>/dev/null`, `1>/dev/null`) before
# running the TIER3_PATTERNS regex check. `is_tier3_dangerous` denies any `>`
# redirect via the generic `(?<!<)>{1,2}(?!\(|&[\d-])` pattern below, which
# false-positives on the universally-safe fd-to-/dev/null silencing idiom. The
# strip pre-pass leaves the safe pattern invisible to the dangerous-pattern
# check while preserving denial for `>file`, `>>file`, and other redirect
# targets that could overwrite arbitrary destinations.
#
# Surfaced by mika#1327 dev-pilot dispatch 2026-05-28: `ls /path/ 2>/dev/null`
# was Tier-1-denied → cpp#20 default-deny → interrupt=True halt.
# Anchor the trailing edge with a negative lookahead instead of `\b` -- `\b`
# fires between `l` (word) and `/` (non-word) so `2>/dev/null/etc/passwd`
# would strip to `/etc/passwd` and slip past the redirect-to-file check.
# The negative lookahead `(?![/\w.])` rejects additional path/word/dot
# characters, blocking `/dev/nullified`, `/dev/null.txt`, and path-suffix
# attacks while permitting whitespace, end-of-string, or shell separators
# (`;`, `&`, `|`, `)`, `>`, `<`).
_FD_DEVNULL_RE = re.compile(r"\b\d+>/dev/null(?![/\w.])")


TIER3_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"rm\s+(-\w*r\w*f|-\w*f\w*r)\b"),           # rm -rf, rm -fr, rm -rfi
    re.compile(r"git\s+push\s+.*--force\b"),                # git push --force
    re.compile(r"git\s+push\s+.*-\w*f\b"),                  # git push -f
    re.compile(r"git\s+push\s+\S+\s+(main|master)\b"),      # git push origin main/master
    re.compile(r"git\s+reset\s+--hard\b"),                  # git reset --hard
    re.compile(r"git\s+branch\s+.*-\w*D\b"),                # git branch -D
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    re.compile(r"\bcargo\s+publish\b"),
    re.compile(r"\bsed\s+(-\w*i|-i\w*)\b"),                 # sed -i
    re.compile(r"\bgh\s+label\s+(delete|edit)\b"),
    re.compile(r"\bbash\s+-c\b"),
    re.compile(r"\bsh\s+-c\b"),
    re.compile(r"\beval\s"),
    # NOTE: the blanket `\bxargs\b` deny was REMOVED here (cpp#40), for the same
    # reason the `find … -exec` blanket deny was (cpp#33): it was the SOLE guard
    # for xargs' inner command, which the header doctrine forbids. xargs safety
    # now lives in the allow-list layer: `_is_safe_xargs_command()` admits
    # `xargs [flags] <cmd>` only when `<cmd>` is in the SAME closed-world
    # FIND_EXEC_SAFE_COMMANDS read-only allowlist `find -exec` uses. `xargs sh -c`
    # / `xargs bash -c` stay independently caught by the `sh -c`/`bash -c`
    # patterns just above (defense in depth); `xargs sudo`/`xargs rm` deny because
    # they are not in the allowlist.
    # NOTE: the blanket `find … -(exec|execdir|delete)` deny was REMOVED here
    # (cpp#33). It was the SOLE protection for find's exec sub-feature, which
    # the TIER3 header doctrine above forbids — a denylist entry must never be
    # the only guard for a safe-listed command's sub-feature. find-exec safety
    # now lives in the allow-list layer: `_is_safe_find_command()` admits
    # `-exec/-execdir/-ok/-okdir <cmd>` only when every `<cmd>` is in the
    # closed-world FIND_EXEC_SAFE_COMMANDS read-only allowlist, and denies
    # `-delete` and any command-substitution. `sh -c`/`bash -c` wrappers inside
    # `-exec` stay independently caught by the patterns just above (defense in
    # depth).
    # NOTE: $( and backtick patterns removed — replaced by quote-aware
    # contains_unquoted_metacharacter() check in is_safe_bash_command().
    # See mika#946 (resolution of mika#938 F5 sentinel divergence).
    re.compile(r"<\("),                                     # <(...)
    re.compile(r">\("),                                     # >(...)
    re.compile(r"(?<!<)>{1,2}(?!\(|&[\d-])"),               # > or >> (not process sub, not fd-manipulation)
)


# DOCTRINE: LLM-classifier permission decision (mika#1733 AC2, mika#1193)
#
# Applies per senara-solutions/mika @
# crates/mika-agent/docs/permission-decision-protocol-2026-07-06.md §AC2:
#
#   "This agent structurally cannot do X" applies to pre-classifier engine
#   gates only, NEVER to LLM classifier decisions.
#
# THIS IS THE TIER-3 CLASSIFIER ENTRY POINT — the danger-pattern denylist
# consulted by `is_safe_bash_command` after tier1 allowlist matching. Decisions
# here are POLICY (regex denylist), NOT structural gates. Agents downstream
# MUST NOT frame tier-3 denials as "structurally cannot" — same discipline
# as tier-1 above. Companion pre-classifier structural gates in mika-agent:
# `validate_dispatch_readiness`, `is_unauthorized_webhook_dispatch` (see the
# tier-1 anchor above for the retirement reference — mika#1193).
def is_tier3_dangerous(command: str) -> bool:
    # Strip universal fd-to-/dev/null silencing before the dangerous-pattern
    # check (see _FD_DEVNULL_RE comment). The strip is invisible to all other
    # patterns; only the bare-`>` redirect pattern is affected.
    stripped = _FD_DEVNULL_RE.sub("", command)
    return any(p.search(stripped) for p in TIER3_PATTERNS)


# cpp#130: a plain STDOUT redirect whose target is the inert /dev/null sink
# (`>/dev/null`, `>>/dev/null`, `1>/dev/null`, with or without a space before the
# path) trips the bare-`>` TIER3_PATTERNS entry. `_FD_DEVNULL_RE` above only
# strips an fd-NUMBERED redirect (`\d+>/dev/null`), so the bare `>` form still
# classes tier3-dangerous. That is CORRECT for the REFUSAL — a `>` redirect is
# not an allow-listed idiom, so is_tier3_dangerous keeps returning True and the
# command stays denied — but WRONG for LETHALITY: `/dev/null` writes nowhere, so
# ending the run over it is the two-character life-or-death gap cpp#130 names
# (`grep … >/dev/null` dies while `… 2>&1 | tail` survives). This regex strips a
# stdout/append redirect whose target is exactly /dev/null, mirroring
# `_FD_DEVNULL_RE`'s trailing-boundary lookahead so `/dev/null/../etc/passwd`,
# `/dev/nullified`, and `/dev/null.txt` do NOT strip and stay fatal.
_STDOUT_DEVNULL_RE = re.compile(r"\d*>{1,2}\s*/dev/null(?![/\w.])")


# ── Contained redirect targets are not on their own session-fatal (cpp#154) ───
#
# cpp#130 (just above) removed ONE redirect target from the lethality class: the
# inert `/dev/null` sink. Its own docstring left the rest as debt — "widening the
# exemption to in-worktree targets is left to the destination veto
# (`permissions._destination_veto_reason`)". cpp#154 measured that the
# destination veto CANNOT carry it: `_segment_write_kind` classifies only
# `cp`/`mv`, `mkdir` and `git show >`, so a redirect is invisible to it —
# `echo hi > /etc/passwd` returns `_destination_veto_reason = None`. The
# widening therefore lands HERE, where cpp#130 left it.
#
# What it buys, measured: three claude-pilot sessions on mika#2158 died in one
# day (2026-09-04) on a denial whose CAUSE was pure FORM — a chain
# `_bash_allow_is_chain_safe` cannot honour — while the command only wrote a
# working file. Callbacks `193e368c` (~47 min) and `ce63ad41` (~1 h) on
# `mkdir -p … && for n in …; do gh issue view … > …/$n.md 2>…/$n.err …; done`;
# `0c3ba346` on `cat > /tmp/probe_test.rs <<'EOF' … python3 - <<'PY' …` — 155
# turns, 8 commits pushed, PR never opened. The refusal is CORRECT in all three
# (the chain really is unsafe); only its lethality was not.
#
# The mechanism is LEXICAL, never resolved on disk — the same load-bearing
# choice `_is_sanctioned_tmp_scratch` documents at `permissions.py:922-968`: an
# earlier version of THAT fix called `Path.resolve()` and broke the cpp#38
# symlink-escape tests, because a worktree symlink crafted to resolve into
# `/tmp` got exempted although the pilot never spelled `/tmp`. Matching the
# LITERAL operand text closes that. Nothing here touches the filesystem, and
# `is_tier3_dangerous_for_lethality` keeps its `(command: str) -> bool`
# signature — no `cwd` is threaded in.
#
# EXTRACTED forms (they name a file; the target is validated):
#   `>`  `>>`  `N>`  `N>>`   — with or without a space before the target
#   `&>` `&>>`               — combined stdout+stderr; these DO write a file
# IGNORED forms (they name no file; leaving them in place keeps the generic `>`
# pattern matching, so lethality holds — fail-closed by construction):
#   `>&M` `N>&M`             — fd duplication (`2>&1`), operand is a descriptor
#   `>&-`                    — fd close
#   `>(` `<(`                — process substitution, already covered by its own
#                              `>\(` / `<\(` entries in TIER3_PATTERNS
#
# `[ \t]*` and NOT `\s*` between the operator and the target is load-bearing: a
# real redirect operand always sits on the same line, and `\s*` would let a
# line-final `>` swallow the FIRST TOKEN OF THE NEXT LINE as its target — so
# `"echo done >\nbash -c 'id'"` would strip to `"echo done   -c 'id'"` and lose
# the `bash -c` match. Blanking a redirect must never blank a verb.
_REDIRECT_RE = re.compile(
    r"(?P<op>(?:&|\d*)>{1,2})"
    r"(?:(?P<ignored>&[\d-]|\()|[ \t]*(?P<target>[^\s;&|<>()]*))"
)

# Same charset as cpp#143's `_TMP_SCRATCH_MKDIR_RE` (`[\w./-]`) PLUS `$`, `{`,
# `}` for parameter expansion and `@` (ordinary in a filename). The `$` is not
# decoration: the two `mkdir` deaths redirect to `/tmp/2158bodies/$n.md`, so a
# charset that excluded `$` would let AC3 fail while claiming to fix the ticket.
# The residue is named and bounded — a MID-PATH `$n` could expand at runtime to
# `../../x` and the lexical test would not see it — but the command is NEVER
# EXECUTED (we are deciding the lethality of an already-pronounced refusal, so
# no byte is written), and no probing oracle opens, because this class is
# refused by its FORM: no spelling of the destination flips a chain-unsafe
# denial into an allow.
#
# A LEADING `$` is a different matter and is rejected outright below: `$HOME/x`,
# `${HOME}/.bashrc` and `$OLDPWD/y` name the same destinations as `~/x`, and
# admitting them would make the `~` disqualifier one respelling away from
# useless. A `$` that is not the head of a parameter name — `$(whoami)`, a bare
# or trailing `$` — is rejected for the same reason: the target text names
# nothing this predicate can reason about, so it fails closed.
_CONTAINED_REDIRECT_TARGET_RE = re.compile(r"^[\w./$@{}-]+$")
_BARE_DOLLAR_RE = re.compile(r"\$(?![A-Za-z_{])")


def _is_contained_redirect_target(dest: str) -> bool:
    """Whether a LITERAL redirect target text is contained: in-worktree (relative)
    or under ``/tmp`` (cpp#154).

    Purely lexical on the text as written — no ``Path.resolve()``, no ``stat``,
    no ``cwd``. ``/dev/null`` is covered upstream by ``_STDOUT_DEVNULL_RE`` and
    is deliberately NOT duplicated here (an absolute path outside ``/tmp/``
    returns False).
    """
    if not dest:
        return False
    if ".." in dest:
        return False
    if dest.startswith("~") or dest.startswith("$"):
        return False
    if _CONTAINED_REDIRECT_TARGET_RE.match(dest) is None:
        return False
    if _BARE_DOLLAR_RE.search(dest) is not None:
        return False
    if dest.startswith("/"):
        return dest.startswith("/tmp/")
    return True


def _redirect_targets(command: str) -> list[str] | None:
    """Every file target the command redirects to, in order; ``None`` if any
    redirect's target cannot be extracted (cpp#154).

    Fail-closed: ``None`` means the caller must treat the command as
    un-contained — i.e. exactly ``main``'s behaviour, lethal. Descriptor
    duplication (``2>&1``), fd close (``>&-``) and process substitution
    (``>(``) name no file and are skipped, not failures.

    NOT quote-aware, by construction. A quoted or escaped target
    (``> "/tmp/a b.md"``, ``> a\\ b.txt``) is returned with its quote characters
    attached; ``_is_contained_redirect_target``'s charset then rejects it, so
    the redirect is not stripped and the command stays lethal. The two helpers
    are coupled on purpose — the fail-closed direction is the charset's, not
    this function's — and a test pins the coupling so a future charset widening
    cannot silently exempt a quoted target.
    """
    targets: list[str] = []
    for m in _REDIRECT_RE.finditer(command):
        if m.group("ignored") is not None:
            continue
        target = m.group("target")
        if not target:
            return None
        targets.append(target)
    return targets


def _strip_contained_redirects(command: str) -> str:
    """Blank out each redirect whose target is contained; leave every other
    redirect in place so the generic ``>`` pattern keeps matching (cpp#154)."""
    if _redirect_targets(command) is None:
        return command

    def _replace(m: re.Match[str]) -> str:
        if m.group("ignored") is not None:
            return m.group(0)
        target = m.group("target")
        if target and _is_contained_redirect_target(target):
            return " "
        return m.group(0)

    return _REDIRECT_RE.sub(_replace, command)


# ── A `<`/`>` INSIDE QUOTES is not a redirect operator (cpp#157) ─────────────
#
# The generic `>` entry of `TIER3_PATTERNS` (`:203`) is quote-blind, so it counts
# as a redirection a `>` that bash reads as ordinary text — the replacement half
# of `sed 's/=.*/=<set>/'`. Measured: that ONE segment carries the lethality on
# its own (`is_tier3_dangerous_for_lethality` on the segment alone = True; on the
# incident chain DEPRIVED of it = False). The form-level refusal it rides on
# therefore became TERMINAL and killed the pilot of mika#2179 — the fourth pilot
# death on denial lethality in 48 h (2026-09-05).
#
# TWO CHARACTERS, NOT THE QUOTED REGION. The mask blanks `<` and `>` inside a
# quoted region and NOTHING else. Blanking the whole region would be shorter to
# write and far wider: `echo 'rm -rf /'` would stop matching `rm -rf` — a verdict
# change on a class this ticket does not touch and which has nothing to do with
# redirects. Every other `TIER3_PATTERNS` entry keeps seeing exactly the text it
# sees on `main`. The corollary is the control that distinguishes the two
# variants, and it is pinned: `echo 'rm -rf /'` stays lethal.
#
# UNTERMINATED QUOTE → RETURN THE COMMAND UNCHANGED, i.e. `main`'s verdict, i.e.
# lethal. This sense of conservatism is the INVERSE of the two scanners below
# (`_split_compound_command`, `contains_unquoted_metacharacter`), which treat the
# remainder as INSIDE the quote — and the inversion is deliberate. Those two
# decide an ALLOWANCE, so their fail-closed direction is "refuse"; this one
# decides a LETHALITY, so its fail-closed direction is "do not exempt". Same
# principle, opposite-facing questions.
#
# THIRD QUOTE SCANNER, knowingly. Merging the three is out of scope for a p1
# lethality fix — two of them sit on the ALLOW path and each carries its own
# documented conservatism (above). The debt is pinned instead by
# `TestQuoteScannerBoundaryParity`, a CHARACTERIZATION test: it records where
# each of the three scanners places a quoted region today, INCLUDING the one
# boundary on which the two pre-existing scanners already disagree on `main`
# (`echo "a\\"` — `_split_compound_command` only treats `\X` as an escape pair
# when `X` is `"`, so it swallows the closing quote and leaves the region open,
# while `contains_unquoted_metacharacter` skips `\X` atomically and closes it).
# This mask follows the atomic form, i.e. POSIX and the second scanner. Extracting
# a shared `_quote_spans()` is filed as follow-up, not done here.
#
# Length is preserved (one space per masked character), so no downstream regex
# index shifts and no two tokens can be glued together.
def _mask_quoted_redirect_chars(command: str) -> str:
    r"""Blank every ``<`` and ``>`` that falls inside a single- or double-quoted
    region; leave the rest of the command byte-for-byte intact (cpp#157).

    Purely lexical — no ``cwd``, no filesystem, ``(str) -> str`` — as
    ``permissions._is_sanctioned_tmp_scratch`` (`:922-968`) requires of everything
    on this path. Quote semantics mirror ``contains_unquoted_metacharacter``:

    - Outside quotes, ``'`` and ``"`` open a region.
    - Inside ``"..."``, ``\X`` is an escape pair consumed atomically (so ``\"``
      does not close the region); a bare ``"`` closes it.
    - Inside ``'...'``, backslash is literal — only ``'`` closes.
    - An unterminated quote returns the command UNCHANGED (fail-closed toward
      lethal; see the header comment for why this direction is inverted).
    """
    n = len(command)
    i = 0
    quote_state: str | None = None  # None / "'" / '"'
    out = list(command)

    while i < n:
        ch = command[i]
        if quote_state is None:
            if ch in ("'", '"'):
                quote_state = ch
            i += 1
            continue
        if quote_state == '"' and ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == quote_state:
            quote_state = None
            i += 1
            continue
        if ch in ("<", ">"):
            out[i] = " "
        i += 1

    if quote_state is not None:
        return command
    return "".join(out)


def is_tier3_dangerous_for_lethality(command: str) -> bool:
    """`is_tier3_dangerous`, but a redirect whose target writes nowhere
    (`/dev/null`, cpp#130) or writes a CONTAINED working file (under `/tmp` or
    relative to the worktree, cpp#154) is not on its own session-fatal.

    Consulted ONLY by ``permissions._denial_is_terminal`` — the LETHALITY
    decision cpp#129 split from the refusal. The refusal path keeps calling the
    unnarrowed ``is_tier3_dangerous``, so a `>/dev/null` redirect is still
    REFUSED; this only makes that refusal non-terminal, so the model gets a
    ``tool_result`` error it can adapt (reach for `2>&1 | tail` or a native tool)
    instead of having the run killed.

    A genuinely dangerous command remains fatal even when it also redirects to a
    stripped target: the strips remove only the redirect, so `rm -rf x >/dev/null`
    and `rm -rf x > /tmp/log` still match the `rm -rf` pattern. A target that is
    NOT contained is never stripped, so `> /etc/passwd` (absolute, outside /tmp),
    `> ../x` and `> /tmp/../etc/x` (`..`), and `> ~/x` (`~`) all stay fatal.

    cpp#154 supersedes cpp#130's parting sentence, which left the in-worktree
    widening "to the destination veto (`permissions._destination_veto_reason`)".
    That veto CANNOT carry it: `_segment_write_kind` classifies only `cp`/`mv`,
    `mkdir` and `git show >`, so a bare redirect never reaches it —
    `echo hi > /etc/passwd` measures `_destination_veto_reason = None`. The
    widening therefore lands here, in the same function, applied AFTER the
    /dev/null strip so cpp#130's trailing-boundary edge cases (`/dev/null.txt`,
    `/dev/nullified`, `/dev/null/../etc/passwd`) keep their own behaviour.

    cpp#157 adds a third narrowing, and it runs INNERMOST: a `<` or `>` sitting
    inside a quoted region is ordinary text to bash, not a redirect operator, so
    it is masked before any pattern runs. `sed 's/=.*/=<set>/'` is no longer on
    its own session-fatal — while staying REFUSED, unchanged, since
    `is_tier3_dangerous` is not touched.

    ORDER IS LOAD-BEARING, now across three narrowings: quoted `<`/`>` masked
    FIRST (cpp#157), /dev/null second (cpp#130), contained targets third
    (cpp#154). The mask must come first because the two later strips extract
    redirect TARGETS, and a quoted `>` fabricates a phantom one: on `main`,
    `_redirect_targets("echo 'a>b'")` yields `["b'"]`, which
    `_is_contained_redirect_target`'s charset rejects only by the accident of the
    trailing quote. Masking first removes the phantom target outright instead of
    relying on that accident. The relative order of cpp#130 and cpp#154 is
    unchanged, so their edge cases (`/dev/nullified`, `/dev/null.txt`,
    `/dev/null/../etc/passwd`) keep their own behaviour. All three live in this
    one function, and `_denial_is_terminal` is the single consumer — cpp#151 B0
    collapsed three separate lethality computations into one precisely so two
    notions of "fatal" could not drift apart.
    """
    return is_tier3_dangerous(
        _strip_contained_redirects(
            _STDOUT_DEVNULL_RE.sub(" ", _mask_quoted_redirect_chars(command))
        )
    )


# ── Model-facing prevention hint (mika#1409) ─────────────────────────────────
#
# Prevention-only half of mika#1409 (Approach #2). The headless pilot model has
# no preflight visibility into the deny-list above, so it reaches for forbidden
# shell idioms (`find … -exec`, cross-worktree `md5sum`, `sed -i`) when an
# auto-approved native tool serves the same goal. Every such reach costs a turn
# and a refusal; the hint is there to make them rarer.
#
# This constant is injected into the SDK system prompt by agent.py. It lives
# HERE, next to the patterns it describes (TIER3_PATTERNS, FIND_EXEC_SAFE_COMMANDS,
# SAFE_SHELL_COMMANDS, is_within_project), so the documentation cannot drift
# from the enforcement. n=2 evidence: claude-pilot logs 6f97dc72 (find -exec
# crashed the mika#1381 groom) and 548191b8 (cross-worktree md5sum crashed the
# mika#1255 AC verification).
#
# Honest-closure note (UPDATED, cpp#128): this hint only ever reduced the RATE
# of denied reaches. The session-fatality class it could not close — a novel
# denied pattern crashing the run — was closed by cpp#128, which revised
# cpp#20 joint 2's contract to distinguish adaptation from fabrication exactly
# as mika#1410 asked. A denied reach now returns to the model as a tool_result
# error it can adapt to; only a destination veto or a tier3-dangerous command
# still ends the run (`permissions._denial_is_terminal`). The hint stays useful:
# a reach that never happens costs no turn at all.
#
# Scope note (cpp#59): this constant grew beyond denied-Bash patterns. It is the
# single model-facing prevention-hint payload appended to the system prompt, and
# now also carries a "no-ops in headless mode" section for harness/runtime tools
# (ScheduleWakeup) that claude-pilot's permission layer CANNOT intercept — the
# SDK/CLI runtime handles them internally, bypassing can_use_tool entirely, so a
# tier1/policy deny is structurally inert. The system-prompt hint is the only
# channel that reaches the model for that class. The name is kept (referenced by
# CLAUDE.md + tests) despite the broadened scope. Same honest-closure boundary:
# prompt-only reduces the RATE of the stochastic ScheduleWakeup trap (n=1 of 139
# sessions, mika#1652), it does not close the class; the disallowed_tools guard in
# agent.py is best-effort defense-in-depth on top.
DENIED_BASH_PATTERNS_HINT: str = """\
## Bash commands the policy DENIES — use the native tool instead

The permission policy DENIES the Bash patterns below. A denied call costs you a
turn and comes back as an error you must work around. Some of them — `sed -i`,
`eval`, `bash -c`, `sh -c`, and anything writing outside this worktree —
additionally END this session immediately, with no retry and no recovery. A
shell redirect (`>`, `>>`) is DENIED but recoverable when its target is a
working file inside this worktree or under `/tmp`; it still ENDS the session
when the target is anywhere else, on the agent control plane (`.git/`,
`.claude/`, `.github/workflows/`, `.mika/`, `skills/bundled/`), or escapes the
worktree through a symlink (cpp#154). Never reach for any of them; use the
auto-approved native tool, which accomplishes the same goal:

- `find … -exec`/`-execdir`/`-ok`/`-okdir` with a NON-read-only inner command
  (e.g. `find … -exec rm`, `find … -exec sh -c …`, `find … -exec sudo …`), and
  `find … -delete` (denied as filesystem-mutating / RCE-class, regardless of
  path). Read-only inner commands (`grep`, `cat`, `head`, `tail`, `ls`, `stat`,
  `wc`, `echo`, …) ARE auto-approved, so
  `find . -name "*.rs" -exec grep -l "struct" {} \\;` runs without halting. Still
  prefer the **Grep** tool to search file contents and the **Glob** tool to find
  files by name — they never risk a denial — but a read-only `find … -exec` no
  longer crashes the session.
- Hashing or inspecting a file with a non-safe-listed command (e.g. `md5sum`,
  `sha256sum`) → use the **Read** tool to read the file directly. Only a small
  allow-list of read-only shell tools is auto-approved; others like `md5sum`
  are denied on ANY path. Read works on any absolute path, inside or outside
  the current worktree — so prefer it for cross-worktree file comparison.
- In-place edits via `sed -i` → use the **Edit** tool.
- Writing files via shell redirect (`>`, `>>`) → use the **Write** tool.
- `xargs` with a NON-read-only inner command (`xargs rm`, `xargs sh -c …`,
  `xargs bash -c …`, `xargs sudo …`) → use the dedicated native tool. A read-only
  inner command (`grep`, `cat`, `head`, `tail`, `ls`, `stat`, `wc`, `echo`, …) IS
  auto-approved, so `find … | xargs grep -l "pattern"` runs without halting. Still
  prefer **Grep**/**Glob** for searching, but a read-only `xargs` no longer crashes
  the session.
- `eval`, `bash -c`, `sh -c` → use the dedicated native tool
  (Grep/Glob/Read/Edit/Write) for the underlying goal.

Prefer Read, Write, Edit, Grep, and Glob over their shell equivalents: they are
auto-approved and never halt the session.

## Tools that are no-ops in headless mode — never call them

You are running headlessly via the Claude Agent SDK. There is NO interactive
harness watching for wake events, so the tools below silently do nothing and
strand your session:

- `ScheduleWakeup` → schedules a future wake the INTERACTIVE harness would fire.
  In headless mode nothing fires it: the call returns "wakeup scheduled", your
  turn ends, and your prompted continuation NEVER runs — the session just ends
  with the work unfinished. Never call it. If you dispatched an `Agent`/subagent
  (e.g. Explore) and want to "wait" for its result, you do NOT need to: the
  subagent runs synchronously and its result is already available to you in the
  next turn. Just continue your work in-turn — read the result and proceed."""


# ── Safe Bash command checking ───────────────────────────────────────────────


def _split_compound_command(command: str) -> list[str]:
    """Quote-aware split on shell operators AND raw newlines.

    Splits on ``&&``, ``||``, ``;``, ``|``, and ``\\n`` only when they appear
    OUTSIDE of single- or double-quoted regions. Quote handling mirrors POSIX
    semantics used by ``contains_unquoted_metacharacter`` in this module:

    - Inside ``"..."``, ``\\"`` is an escape pair (skipped atomically); other
      backslash sequences pass through as-is so ``"a\\|b"`` does not close the
      quote on ``\\``.
    - Inside ``'...'``, backslash is literal — only a closing ``'`` ends the
      quoted region.
    - Unterminated quotes: remaining bytes are treated as inside the quote
      (conservative — falls through to the LLM relay on malformed input).

    ``\\n`` is included because bash treats a bare newline as a command
    separator equivalent to ``;``. Without splitting on ``\\n``, a payload like
    ``git status\\nrm -rf /`` would be evaluated as one segment, miss the
    rm-rf regex on the second line, and auto-approve via the safe-git prefix.

    Pre-fix: split was a single quote-blind regex that matched ``|`` inside
    grep regex alternations (``grep "a\\|b\\|c"``), shredding the segment list
    into nonsense substrings. Every "segment" then failed the safe-list checks,
    tier1 rejected the entire research grep, and the downstream chain-safety
    check halted the pilot with `policy-deny [bash-grep]` even though the
    research command was inherently safe (read-only grep + cargo doc).
    Observed wedging mika#96 and mika#623 dispatch on 2026-06-14.
    """
    segments: list[str] = []
    n = len(command)
    i = 0
    seg_start = 0
    quote_state: str | None = None  # None / "'" / '"'

    while i < n:
        ch = command[i]

        if quote_state is None:
            if ch in ("'", '"'):
                quote_state = ch
                i += 1
                continue
            if ch in (";", "\n", "\r"):
                # `\r` treated as `\n`: some pipelines (Windows-authored payloads,
                # copy-pasted heredocs) carry CR terminators. Bash on Unix ignores
                # bare `\r` between tokens, but the classifier fails-closed here —
                # splitting on `\r` prevents an obfuscation vector where a
                # payload uses CR to hide a second statement from a `\n`-only
                # splitter (coherence refute cpp#103 2026-08-06).
                segments.append(command[seg_start:i].strip())
                i += 1
                seg_start = i
                continue
            if ch == "&" and i + 1 < n and command[i + 1] == "&":
                segments.append(command[seg_start:i].strip())
                i += 2
                seg_start = i
                continue
            if ch == "&":
                # Single `&` = background operator (statement separator). Bash
                # runs the LHS in the background and continues with the next
                # statement — same semantic as `;` for classifier purposes.
                # BUT: `&` also appears in fd-redirect syntax `2>&1` / `>&2`.
                # Preceded by `>` → part of a redirect, NOT a separator.
                # Preceded by `<` → part of process-substitution `<(...)` /
                # `<&N` — also not a separator. Fix cpp#103 (coherence refute
                # 2026-08-06): previously single `&` fell through to `i += 1`
                # and `foo & rm -rf /` never split, so the rm sub scattered
                # outside the deny check.
                prev = command[i - 1] if i > 0 else ""
                if prev in (">", "<"):
                    i += 1
                    continue
                segments.append(command[seg_start:i].strip())
                i += 1
                seg_start = i
                continue
            if ch == "|":
                if i + 1 < n and command[i + 1] == "|":
                    segments.append(command[seg_start:i].strip())
                    i += 2
                    seg_start = i
                else:
                    segments.append(command[seg_start:i].strip())
                    i += 1
                    seg_start = i
                continue
            i += 1
            continue

        # inside a quote
        if quote_state == '"':
            if ch == "\\" and i + 1 < n and command[i + 1] == '"':
                i += 2
                continue
            if ch == '"':
                quote_state = None
            i += 1
            continue

        # quote_state == "'"
        if ch == "'":
            quote_state = None
        i += 1

    tail = command[seg_start:].strip()
    if tail:
        segments.append(tail)
    return [s for s in segments if s]


def contains_unquoted_metacharacter(command: str) -> bool:
    """Return True if *command* contains a backtick, ``$(`` or ``$'`` that bash
    would expand — i.e. anywhere EXCEPT inside single quotes.

    Bash performs command substitution inside double quotes; only single quotes
    suppress it. So the name is historical: the function flags substitution
    markers in unquoted AND double-quoted regions, treating only single-quoted
    regions as inert. Quote handling follows POSIX semantics:

    - Outside quotes, a bare backtick, ``$(`` or ``$'`` returns True.
    - Inside ``"..."`` regions, a bare backtick or ``$(`` returns True (cpp#41
      closed the double-quoted gap — bash expands both there). ``$'`` is NOT
      flagged inside double quotes: ANSI-C ``$'...'`` quoting is only recognized
      outside quotes, so inside a double-quoted region ``$'`` is literal.
    - Inside ``"..."`` regions, ``\\X`` is an escape pair (skipped atomically),
      so a backslash-suppressed ``\\$(``/``\\```` is NOT flagged and ``\\"`` does
      not close the region.
    - Inside ``'...'`` regions, backslash is literal — ``'foo\\\\'`` closes at
      the second ``'`` and any backtick that follows is unquoted.
    - Unterminated quotes: the scanner treats all remaining bytes as inside the
      quote (conservative — falls through to the LLM relay on malformed input).

    NOTE: the Rust mirror ``contains_unquoted_metacharacter`` in
    ``crates/mika-agent/src/server/permission_pre_classifier.rs`` (mika repo) does
    NOT yet detect double-quoted substitution. This Python side intentionally
    diverges (hardened) until the paired-audit ticket mirrors the cpp#41 fix.

    See mika#944 (ANSI-C quoting bypass), mika#946 (mika#938 F5 sentinel),
    cpp#41 (double-quoted substitution gap).
    """
    n = len(command)
    i = 0
    quote_state: str | None = None  # None / "'" / '"'

    while i < n:
        ch = command[i]
        if quote_state is not None:
            # Inside a quoted region — handle escape (double-quoted only) first.
            if quote_state == '"' and ch == '\\' and i + 1 < n:
                # Skip the `\X` pair. In bash, `\` inside double quotes suppresses
                # `$`/backtick, so `"\$(x)"` / "\`x\`" are literal — skipping the
                # pair correctly prevents flagging a SUPPRESSED substitution. `\"`
                # likewise does not close the region (handled by skipping here).
                i += 2
                continue
            if ch == quote_state:
                quote_state = None
                i += 1
                continue
            # cpp#41: bash performs command substitution inside DOUBLE quotes —
            # only SINGLE quotes suppress it. The pre-cpp#41 scanner treated a
            # double-quoted region as inert and missed `$(`/backtick, so
            # `grep "$(id)"` auto-approved and bash ran `id`. Scan double-quoted
            # regions for the two markers bash STILL expands there: `$(` and
            # backtick. `$'` is deliberately NOT flagged inside double quotes —
            # ANSI-C `$'...'` quoting is only recognized OUTSIDE quotes; inside a
            # double-quoted region `$'` is a literal dollar + apostrophe (no
            # expansion), so flagging it would be a false positive (mika#944's
            # `$'` guard correctly lives in the UNQUOTED branch only). Single-
            # quoted regions stay fully inert (bash literal semantics).
            if quote_state == '"':
                if ch == "`":
                    return True
                if ch == "$" and i + 1 < n and command[i + 1] == "(":
                    return True
            i += 1
            continue

        # Unquoted region — open a quote or check for metacharacters.
        if ch == "'" or ch == '"':
            quote_state = ch
            i += 1
            continue
        if ch == "`":
            return True
        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            return True
        # $' (ANSI-C quoting — escapes like \xNN expand at execution time)
        # mika#944: mirrors the Rust scanner's $' check.
        if ch == "$" and i + 1 < n and command[i + 1] == "'":
            return True
        i += 1

    return False


def is_safe_bash_command(command: str) -> bool:
    # Exec-si-contenu whole-command exception: the ce-work Setup preamble is
    # a legitimate multi-line compound (for-loop + if + $()) that stalls the
    # standard classifier BUT is bounded by containment when
    # MIKA_PILOT_CONTAINED=1. Match BEFORE the metachar/tier3 guards — the
    # anchored regex + charset constraints inside the shape are the safety
    # boundary here, not the generic guards. Fails CLOSED if attestation
    # absent or shape drifts (see `is_ce_work_preamble_when_contained` doc).
    if is_ce_work_preamble_when_contained(command):
        return True

    # cpp#103: read-only git compounds under containment attestation. Must
    # match BEFORE the metachar guard — `&&`/`|`/`2>/dev/null` are legitimate
    # glue in ce-work branch-check compounds (SKILL.md §Setup Environment).
    # Fail-closed on any unrecognized sub-command shape.
    if is_git_readonly_compound_when_contained(command):
        return True

    if contains_unquoted_metacharacter(command):
        return False
    if is_tier3_dangerous(command):
        return False

    sub_commands = _split_compound_command(command)
    if not sub_commands:
        return False

    return all(_is_safe_sub_command(sub) for sub in sub_commands)


def _is_safe_sub_command(sub: str) -> bool:
    return (
        is_safe_git_command(sub)
        or is_safe_build_command(sub)
        or is_safe_make_command(sub)
        or is_safe_shell_command(sub)
        or is_safe_gh_command(sub)
        or is_safe_mika_dispatch(sub)
        or is_safe_exec_when_contained(sub)
    )


# ── Safe-exec primitives (Exec-si-contenu, Vincent-ratified 2026-08-04) ──────
#
# Under the containment attestation (`MIKA_PILOT_CONTAINED=1`, set by mika's
# dispatch-lib.sh Phase 2b bwrap wrapper — fs+net+kernel cut with
# hostname-allowlist egress), these interpreter primitives become leaf-effect
# tier1 commands. Their arbitrary side effects are bounded by the sandbox:
# fs writes land in tmpfs or the branch worktree, net calls go through the
# egress relay allowlist, kernel namespaces isolate the process. Hors
# containment, they remain denied (invariant enforced).
#
# Founding case: the compound-engineering ce-work plugin's Setup preamble
# runs `node "$SKILL_DIR/scripts/context.mjs"` — a legitimate Node script the
# LLM invokes to emit workflow context. Pre-containment, this required either
# a fragile per-shape allowlist (cpp#100-class enumeration) or a plugin
# source patch (workspace-brittle). Post-containment, it becomes a direct
# tier1 pass — the effect IS bounded.
#
# Scope:
#   * `node <script>` — with args, redirect chains upstream-classified.
#   * `python3 <script>` — same shape as node.
#   * Chain safety (compound `;`/`||`/`&&`) is handled by the upstream
#     `_split_compound_command` + all-subs-safe loop — each sub still needs
#     to pass a tier1 predicate. safe-exec here is one such predicate.
#
# Not covered here (intentionally):
#   * `python -c 'code'` — arbitrary inline code deserves a separate rule
#     if needed. The founding case uses `python3 <script>` shape.
#   * `node -e 'code'` — same rationale.
#   * `bash <script>` — sub-shell has its own compound checker path.
#
# The is_tier3_dangerous + contains_unquoted_metacharacter checks upstream
# still apply — even under containment, a `node "$(rm -rf /)"` shape trips
# the metacharacter guard before reaching this predicate.

_NODE_EXEC_RE = re.compile(r"^\s*node\s+\S")
_PYTHON3_EXEC_RE = re.compile(r"^\s*python3\s+\S")


def is_safe_exec_when_contained(sub: str) -> bool:
    """Allow `node <script>` / `python3 <script>` iff pilot is contained.

    The `MIKA_PILOT_CONTAINED=1` env is set by dispatch-lib SOLELY when the
    Phase 2b full containment shape is active. Absent it (dev shells,
    Phase 2a fallback with net open, direct classifier tests) → False.
    """
    if not _is_pilot_contained():
        return False
    if _NODE_EXEC_RE.match(sub):
        return True
    if _PYTHON3_EXEC_RE.match(sub):
        return True
    return False


# ── ce-work Setup preamble compound (Exec-si-contenu specific case) ──────────
#
# The compound-engineering plugin's `ce-work` skill defines a Setup section
# that the pilot's Claude Code invokes at every `/ce:work` (see
# `~/.claude/plugins/cache/every-marketplace/compound-engineering/3.21.0/
# skills/ce-work/SKILL.md::Setup`). Shape (single Bash string, multi-line):
#
#     SKILL_DIR="<absolute path>";
#     NODE="$(for c in node nodejs; do
#         command -v "$c" >/dev/null 2>&1 && "$c" -e '' >/dev/null 2>&1 &&
#         { echo "$c"; break; };
#     done)";
#     if [ -n "$NODE" ]; then
#     "$NODE" "$SKILL_DIR/scripts/context.mjs" || echo "<literal>";
#     else
#     echo "<literal>";
#     fi
#
# The compound uses `$(...)` command substitution + a `for` loop + `if`
# statement — hits `contains_unquoted_metacharacter` upstream and never
# reaches per-sub classification. Pre-containment: legitimate deny (the
# effect could touch anything). Post-containment (`MIKA_PILOT_CONTAINED=1`):
# the effect is bounded by bwrap — fs writes land in tmpfs/worktree, net
# calls go through the egress allowlist. Auto-approving the whole compound
# is safe.
#
# The founding blocker: this preamble stalled EVERY dev-pilot dispatch for
# days before Exec-si-contenu was ratified (2026-08-04). It's the concrete
# canary of the invariant.
#
# The regex anchors the entire compound with charset constraints on:
#   * SKILL_DIR path: `[^"]+` (no `"` — nothing quoted around it)
#   * Script path within SKILL_DIR: `[^"]+`
#   * echo literals: `[^"]+`
# All other content is literal-matched. The rule fails CLOSED — variant
# preambles (different plugin, different Setup) do not match. If the
# compound-engineering plugin changes the Setup shape, this rule stops
# firing and the compound reverts to the standard-deny path.

_CE_WORK_PREAMBLE_RE = re.compile(
    r'^SKILL_DIR="[^"]+";\s*'
    r'NODE="\$\(for c in node nodejs; do '
    r'command -v "\$c" >/dev/null 2>&1 && '
    r'"\$c" -e \'\' >/dev/null 2>&1 && '
    r'\{ echo "\$c"; break; \}; '
    r'done\)";\s*'
    r'if \[ -n "\$NODE" \]; then\s*'
    r'"\$NODE" "\$SKILL_DIR/scripts/[^"]+" \|\| echo "[^"]+";\s*'
    r'else\s*'
    r'echo "[^"]+";\s*'
    r'fi\s*$',
    re.DOTALL,
)


def is_ce_work_preamble_when_contained(command: str) -> bool:
    """Match the compound-engineering ce-work Setup preamble under containment.

    Returns True IFF the command is the exact ce-work Setup shape AND the
    pilot subprocess is contained (`MIKA_PILOT_CONTAINED=1`). Anywhere else
    (dev shells, Phase 2a fallback, variant preambles) → False.

    Called from `is_safe_bash_command` BEFORE the metachar guard, so the
    `$(...)` command substitution inside the anchored shape doesn't trip
    the standard-deny path. The anchored regex + charset constraints on
    the three variable-content zones (SKILL_DIR path, script path, echo
    literal) mean no attacker-controlled substring can carry chain
    metachars or additional side-effects.
    """
    if not _is_pilot_contained():
        return False
    return bool(_CE_WORK_PREAMBLE_RE.match(command))


# ── Safe git commands ────────────────────────────────────────────────────────

SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "branch", "show", "commit",
    "push", "checkout", "worktree", "rev-parse", "remote",
    "fetch", "pull", "add", "stash", "tag", "merge",
    "rebase", "cherry-pick", "symbolic-ref",
    "ls-files", "describe", "shortlog", "blame",
    # `merge-base` is read-only: prints the best common ancestor commit SHA on
    # stdout, has no filesystem or ref-mutation side effects. Same safety class
    # as `rev-parse` / `describe` / `shortlog` already in this set.
    # Groom-phase pilots need it to detect base-drift before diff (`git merge-base
    # main HEAD` then `git diff --name-only $BASE HEAD`). Added after 18-incident
    # policy:deny class observed 2026-07-26 → 2026-07-27 blocked dev-groom /
    # dev-pilot on mika#1852/#1849/#1401/#1403 (compound-bash tier1/tier2 gap).
    "merge-base",
})

_GIT_CMD_RE = re.compile(r"^\s*git\s+(\S+)")
_FORCE_FLAG_RE = re.compile(r"--force\b|-\w*f\b")
_MAIN_MASTER_RE = re.compile(r"\b(main|master)\b")
_BRANCH_D_RE = re.compile(r"-\w*D\b")

# Global git-flag deny list (applies to ALL git commands, contained or not).
# These flags turn git into an arbitrary-exec / arbitrary-write channel via
# config injection (`-c core.pager='sh -c ...'`), cwd escape (`-C /etc`),
# output redirection (`--output=/etc/passwd`), or ref-transport hijack
# (`--upload-pack=<attacker-cmd>`). Coherence flagged these as exec-per-flag
# leaks in cpp#103 refinement — closing them keeps `is_safe_git_command` an
# honest read-only whitelist. See test_tier1_git_readonly_compound for the
# attacker corpus these guard.
_GIT_DENIED_GLOBAL_FLAG_RE = re.compile(
    r"(^|\s)-c\s+\S+="              # `git -c KEY=VAL` config injection (pager attack)
    r"|(^|\s)--config-env(\s|=)"    # env-var config injection
    r"|(^|\s)-C\s+\S+"              # cwd escape
    r"|(^|\s)--exec-path(\s|=)"     # git-core dir override (exec surface)
    r"|(^|\s)--output(\s|=)"        # `git diff --output=/etc/passwd`
    r"|(^|\s)-o\s+/"                # short form output (absolute path)
    r"|(^|\s)--upload-pack(\s|=)"   # arbitrary transport exec (fetch/pull)
    r"|(^|\s)--receive-pack(\s|=)"  # arbitrary transport exec (push)
)


def is_safe_git_command(sub: str) -> bool:
    match = _GIT_CMD_RE.match(sub)
    if not match:
        return False

    git_sub = match.group(1)
    if git_sub not in SAFE_GIT_SUBCOMMANDS:
        return False

    if _FORCE_FLAG_RE.search(sub):
        return False
    if _GIT_DENIED_GLOBAL_FLAG_RE.search(sub):
        return False
    if git_sub == "push" and _MAIN_MASTER_RE.search(sub):
        return False
    if git_sub == "branch":
        # cpp#103 minor resserrement: extend mutant-flag deny beyond `-D` to
        # cover `-d`/`-m`/`-M`/`--delete`/`--move`. `--force`/`-f` already
        # caught by `_FORCE_FLAG_RE`. Token-based check avoids the
        # `-\w*[dDmM]` false-positive on `--diff-filter=D` and similar.
        tokens = sub.split()
        for tok in tokens:
            if tok in _GIT_BRANCH_MUTANT_TOKENS:
                return False

    return True


# ── Read-only git compound predicate (cpp#103, Exec-si-contenu widen) ────────
#
# The compound-engineering `ce-work` skill (SKILL.md §Setup Environment lines
# 122-129) prescribes a branch-check compound the pilot LLM reformulates as
# something like:
#
#   git branch --show-current && echo "---" && \
#     git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | \
#     sed 's@^refs/remotes/origin/@@' && \
#     echo "---" && git status --short | head -30
#
# The pilot Turn-5 policy:deny [bash-git-readonly] baseline (session
# `7d4f2321-5e11-4c74-807f-fa1dabb9458a`, 2026-08-06) shows this pattern kills
# every contained dispatch — legitimate ce-work behavior, `contains_unquoted_
# metacharacter` fires on `&&` / `|` / `2>/dev/null` before any per-sub
# classification.
#
# Under `MIKA_PILOT_CONTAINED=1` this compound is bounded — fs writes land in
# bwrap tmpfs / worktree, net through egress allowlist, kernel unshares
# isolate the process. We match the compound-shape BEFORE the metachar guard
# fires (same slot as `is_ce_work_preamble_when_contained`), gated on the
# attestation. Fail-CLOSED on unknown shapes — every sub-command must match
# one of the whitelisted forms below.
#
# Scope: read-only git primitives + benign pipe tools (echo literal, sed pure
# substitution, head/tail/wc, cat, mktemp). Coherence-refined shapes closed
# the exec-per-flag leaks: git flag deny (see `_GIT_DENIED_GLOBAL_FLAG_RE`),
# sed pure `s///[gp]` only (deny `e`/`w`/`W`/`r`), echo no `$(...)`/backtick,
# mktemp no `--tmpdir=<path>`.
#
# Precondition (verified 2026-08-06 pre-merge):
#   (a) `MIKA_PILOT_CONTAINED=1` inforgeable — sole setter is
#       `mika/skills/bundled/_shared/dispatch-lib.sh:287 --setenv` inside the
#       Phase 2b bwrap invocation, AFTER `--clearenv`. Sole reader is
#       `_is_pilot_contained()` above. No mika/cpp code sets it elsewhere.
#   (b) Structural bwrap coupling — `MIKA_PILOT_SANDBOX=0` bypass returns
#       from `_run_pilot_sandboxed` direct-exec (no bwrap → no --setenv →
#       attestation absent → this predicate fails closed → strict deny).
#       No mika code sets `MIKA_PILOT_SANDBOX=0` — bypass requires explicit
#       operator env intervention.

# Strict read-only subset of SAFE_GIT_SUBCOMMANDS. Excludes any subcommand
# with ref/index/working-tree mutation semantics (commit/push/checkout/
# worktree/add/stash/tag/merge/rebase/cherry-pick/fetch/pull/remote/branch-mut).
# `branch` is allowed in this set for read-mode (`--show-current`, `--list`,
# etc.) — the compound predicate additionally denies `branch -d/-D/-m/-M/-f`
# via `_BRANCH_D_RE` + a `-m/-M` guard applied below.
SAFE_GIT_READONLY_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "rev-parse",
    "symbolic-ref", "ls-files", "describe", "shortlog", "blame",
    "merge-base",
})

_GIT_BRANCH_MUTANT_FLAG_RE = re.compile(r"-\w*[dDmM]\b")

# Sed: allow ONLY pure substitution `s<SEP>PATTERN<SEP>REPLACE<SEP>[gp]*`.
# Deny `e` flag (exec via replacement), `w`/`W` (write to file), `r` (read
# arbitrary file), any command other than `s` (`d`/`y`/`q`/`n`/`a`/`i`/`c`/
# `!`), `-e` (multi-script), `-f` (script file), `-i` (in-place).
# SEP is one of `/@#|:` — the common alternatives; separator uniqueness
# inside PATTERN/REPLACE is guaranteed by `[^SEP\\]*` character class per SEP.
_SAFE_SED_SUB_RES = [
    re.compile(r"^\s*sed\s+'s/(?:[^/\\]|\\.)*/(?:[^/\\]|\\.)*/[gp]*'\s*(?:\S+\s*)*$"),
    re.compile(r"^\s*sed\s+'s@(?:[^@\\]|\\.)*@(?:[^@\\]|\\.)*@[gp]*'\s*(?:\S+\s*)*$"),
    re.compile(r"^\s*sed\s+'s#(?:[^#\\]|\\.)*#(?:[^#\\]|\\.)*#[gp]*'\s*(?:\S+\s*)*$"),
    re.compile(r"^\s*sed\s+'s\|(?:[^|\\]|\\.)*\|(?:[^|\\]|\\.)*\|[gp]*'\s*(?:\S+\s*)*$"),
    re.compile(r"^\s*sed\s+'s:(?:[^:\\]|\\.)*:(?:[^:\\]|\\.)*:[gp]*'\s*(?:\S+\s*)*$"),
]

# Echo literal: quoted string containing NO `$` (blocks `$(...)` and `$var`),
# NO backtick (blocks `` `cmd` `` substitution), NO unescaped inner `"`.
_SAFE_ECHO_QUOTED_RE = re.compile(r'^\s*echo\s+"(?:[^"$`\\]|\\.)*"\s*$')
# Unquoted echo of pure literal (very narrow charset)
_SAFE_ECHO_LITERAL_RE = re.compile(r"^\s*echo\s+[A-Za-z0-9_.,:/=+-]+\s*$")

# Bounded pipe tools: head/tail with numeric arg only, wc with flag-only,
# cat with single filename arg, mktemp with only -d (no --tmpdir=<path>).
# File args restricted to same charset as cat — prevents `head -30 >stolen`
# where `>stolen` was accepted as a file arg by loose `\S+` (coherence
# refute 2026-08-06 mineur).
_SAFE_HEAD_TAIL_RE = re.compile(
    r"^\s*(?:head|tail)(?:\s+-[cn]\s*\d+|\s+-\d+)?(?:\s+[A-Za-z0-9_./-]+)?\s*$"
)
_SAFE_WC_RE = re.compile(r"^\s*wc(?:\s+-[lcwLm]+)?(?:\s+[A-Za-z0-9_./-]+)?\s*$")
_SAFE_CAT_RE = re.compile(r"^\s*cat\s+[A-Za-z0-9_./-]+\s*$")
_SAFE_MKTEMP_RE = re.compile(r"^\s*mktemp(?:\s+-d)?\s*$")


def _is_safe_sed_pure_substitution(sub: str) -> bool:
    """True iff sub is `sed 's<SEP>PATTERN<SEP>REPLACE<SEP>[gp]*' [FILE]`.

    Deny-list intentionally strict: no `e` flag (exec), no `w`/`W`/`r`
    (file I/O), no non-`s` command, no `-e`/`-f`/`-i` flags. Any deviation
    from the exact substitution shape → False.
    """
    return any(rgx.match(sub) for rgx in _SAFE_SED_SUB_RES)


def _is_safe_echo_literal(sub: str) -> bool:
    """True iff sub is `echo "quoted-literal-no-metachars"` or bare literal."""
    return bool(_SAFE_ECHO_QUOTED_RE.match(sub) or _SAFE_ECHO_LITERAL_RE.match(sub))


def _is_safe_pipe_tool(sub: str) -> bool:
    """True iff sub is head/tail/wc/cat/mktemp in a bounded read-only shape."""
    return bool(
        _SAFE_HEAD_TAIL_RE.match(sub)
        or _SAFE_WC_RE.match(sub)
        or _SAFE_CAT_RE.match(sub)
        or _SAFE_MKTEMP_RE.match(sub)
    )


# Token-based flag deny for the readonly compound predicate. Avoids the
# pre-existing `_FORCE_FLAG_RE` false positive that matches `-ref` inside
# the compound word `symbolic-ref` (bug in `-\w*f\b`). Tokenizes on
# whitespace and matches WHOLE tokens against the deny set.
_GIT_READONLY_DENIED_TOKENS = frozenset({
    "--force",
    "-f",
    "-c",           # `git -c KEY=VAL` config injection (pager attack)
    "-C",           # cwd escape
    "-o",           # short output
    "--output",
    "--config-env",
    "--exec-path",
    "--upload-pack",
    "--receive-pack",
})

# Prefix-match deny (for `KEY=VAL` suffixed flags: `--output=/x`, `-c KEY=X`,
# etc.). Checked separately since exact-token match doesn't cover `<flag>=X`.
_GIT_READONLY_DENIED_PREFIXES = (
    "--output=",
    "--config-env=",
    "--exec-path=",
    "--upload-pack=",
    "--receive-pack=",
    "-c",          # will match `-c` bare AND `-cKEY=X` (rare shape)
)

# Branch mutant flags: `-d`, `-D`, `-m`, `-M`, `-f`, `--delete`, `--move`,
# `--force`. Token-based (avoids the `-\w*[dDmM]` false positive on things
# like `-diff-filter=D`).
_GIT_BRANCH_MUTANT_TOKENS = frozenset({
    "-d", "-D", "-m", "-M", "-f",
    "--delete", "--move", "--force",
})


def _is_safe_git_readonly_sub(sub: str) -> bool:
    """True iff sub is a strict read-only git command (compound-safe subset).

    Stricter than `is_safe_git_command`:
      * SAFE_GIT_READONLY_SUBCOMMANDS only (no commit/push/checkout/etc.)
      * Global git-flag deny by TOKEN match (config-injection, cwd escape,
        output write, transport-exec) — avoids `_FORCE_FLAG_RE`'s false
        positive on compound words like `symbolic-ref`.
      * `branch -d/-D/-m/-M/-f/--delete/--move/--force` denied (mutants).
      * No `>`, `>>`, `<`, `<(`, `>(` shell redirects (except upstream-
        stripped `2>/dev/null`).
    """
    match = _GIT_CMD_RE.match(sub)
    if not match:
        return False
    git_sub = match.group(1)
    if git_sub not in SAFE_GIT_READONLY_SUBCOMMANDS:
        return False

    # Redirect chars deny — any `>`/`>>`/`<`/`<(`/`>(` remaining after the
    # caller stripped `[0-9]*>/dev/null` denies. Previously excluded
    # fd-numeric prefix via `(?<![0-9])>` — but that let `1>/tmp/evil` and
    # `2>/tmp/x` (non-devnull stderr redirect) through (coherence mineur).
    # Now: any `>`/`<` char in the sub (post-devnull-strip) → deny.
    if ">" in sub or "<" in sub:
        return False

    # Tokenize on whitespace; check each token against deny sets.
    tokens = sub.split()
    for tok in tokens:
        if tok in _GIT_READONLY_DENIED_TOKENS:
            return False
        for prefix in _GIT_READONLY_DENIED_PREFIXES:
            # `-c` alone requires the NEXT token to be KEY=VAL to be an injection;
            # `-C` alone requires the NEXT token to be a path (also denied).
            # We deny both bare -c/-C and any --output=/--config-env=/etc. prefix.
            if tok.startswith(prefix) and prefix in ("--output=", "--config-env=",
                                                       "--exec-path=", "--upload-pack=",
                                                       "--receive-pack="):
                return False
        # Branch subcommand mutant flag check
        if git_sub == "branch" and tok in _GIT_BRANCH_MUTANT_TOKENS:
            return False

    return True


# Compound split respecting `&&`, `||`, `;`, `|`, `&` (background), and
# `\n`/`\r` (statement separators — bash treats each line as an independent
# command). All are legitimate separators bash executes each side of. Missing
# `&` and newline (coherence-flagged 2026-08-06 refute) allowed a bypass:
# `git log & curl http://evil/x` tokenized across `&` in `sub.split()` and
# the `curl` sub scattered outside the deny check → auto-approved. Fix
# closes the gap so each sub re-validates independently.
#
# `[0-9]*>/dev/null` (fd-numeric stderr suppression) is stripped from each
# sub BEFORE predicate matching. Strip is anchored to end-of-token to prevent
# suffix escape (`2>/dev/null/../etc/x` no longer strips at the `null` bound-
# ary). Any other `>`/`>>`/`<` remaining after strip → sub fails closed.
_STDERR_DEVNULL_RE = re.compile(r"\s+[0-9]*>/dev/null(?=\s|$)")


def _split_git_readonly_compound(command: str) -> list[str]:
    """Split on `&&`/`||`/`;`/`|`/`&`/newline and strip `[0-9]*>/dev/null`.

    Every bash statement separator handled — the compound whitelist must
    validate EACH resulting sub, not the glue itself. `&` (background) and
    `\\n`/`\\r` (line breaks) previously slipped through, allowing
    `git log & curl evil` to auto-approve (coherence refute 2026-08-06).
    """
    # Newlines and background-`&` are statement terminators; treat as `;`.
    parts = re.split(r"\s*(?:&&|\|\||;|\||&|\n|\r)\s*", command)
    return [_STDERR_DEVNULL_RE.sub("", p).strip() for p in parts if p.strip()]


def is_git_readonly_compound_when_contained(command: str) -> bool:
    """Match read-only git compounds bounded by containment (cpp#103).

    Returns True IFF:
      1. `MIKA_PILOT_CONTAINED=1` — attestation gate (structurally inforgeable
         per (a)/(b) audit above)
      2. `contains_unquoted_metacharacter(command)` is False — blocks
         `$(...)`/backtick in double-quoted arg strings (cpp#41 semantics).
      3. `is_tier3_dangerous(command)` is False — defense-in-depth call at
         predicate scope so tier3 patterns (find -exec, sudo, curl, rm -rf,
         `>` redirect, etc.) still fail closed even when a sub matches the
         whitelist. Previous code path returned True BEFORE the standard
         `is_tier3_dangerous` call in `is_safe_bash_command` — coherence
         refute 2026-08-06 closed this gap.
      4. Every sub-command (split on `&&`/`||`/`;`/`|`/`&`/newline, stripping
         `[0-9]*>/dev/null`) matches ONE of:
           * `git <SAFE_GIT_READONLY_SUBCOMMAND> [flags]` per
             `_is_safe_git_readonly_sub` (flag deny-list applied)
           * `sed 's<SEP>PATTERN<SEP>REPLACE<SEP>[gp]*'` pure substitution
           * `echo "literal"` or bare literal (no `$`/backtick)
           * `head|tail|wc|cat|mktemp` in bounded shape
      5. Any unrecognized sub fails closed.

    Wired into `is_safe_bash_command` BEFORE the metachar guard so that
    `&&`, `|`, and `2>/dev/null` in these legitimate compounds don't trip
    the standard-deny path. Guards 2 and 3 are called explicitly here to
    preserve their semantic (they run downstream in `is_safe_bash_command`
    but this predicate's `return True` short-circuits them).
    """
    if not _is_pilot_contained():
        return False
    if not command.strip():
        return False

    # Metachar substitution guard — deny even if it appears inside double
    # quotes (cpp#41 semantics: bash expands `$(...)` and backticks in `"..."`).
    # We bypass `_split_compound_command`'s per-op check for the whitelisted
    # `&&`/`||`/`;`/`|` glue, but we do NOT permit hidden command substitution
    # in argument strings. `contains_unquoted_metacharacter` correctly flags
    # `$(`/backtick/`$'` in both unquoted and double-quoted regions.
    if contains_unquoted_metacharacter(command):
        return False

    # Defense-in-depth: `is_tier3_dangerous` is normally called downstream in
    # `is_safe_bash_command` — but this predicate short-circuits with `return
    # True` BEFORE the tier3 call. Call it explicitly here so tier3 patterns
    # (find -exec, sudo, curl, rm -rf, `>` redirect, etc.) still fail closed
    # even if a sub happens to match the whitelist (coherence refute 2026-08-06).
    if is_tier3_dangerous(command):
        return False

    subs = _split_git_readonly_compound(command)
    if not subs:
        return False

    for sub in subs:
        if (
            _is_safe_git_readonly_sub(sub)
            or _is_safe_sed_pure_substitution(sub)
            or _is_safe_echo_literal(sub)
            or _is_safe_pipe_tool(sub)
        ):
            continue
        return False

    return True


# ── Safe build/test commands ─────────────────────────────────────────────────

SAFE_CARGO_SUBCOMMANDS: frozenset[str] = frozenset({
    "check", "test", "clippy", "fmt", "build",
    "clean", "doc", "bench", "tree", "metadata",
})

SAFE_NPM_RUN_SCRIPTS: frozenset[str] = frozenset({
    "build", "dev", "test", "lint", "fmt", "start",
    "typecheck", "type-check", "check",
})

_CARGO_RE = re.compile(r"^\s*cargo\s+(\S+)")
_NPM_RUN_RE = re.compile(r"^\s*npm\s+run\s+(\S+)")
_NPM_BUILTIN_RE = re.compile(r"^\s*npm\s+(test|start)\b")
_NPM_INSTALL_RE = re.compile(r"^\s*npm\s+(install|ci)\b")
_NPX_RE = re.compile(r"^\s*npx\s+(tsc|vitest|prettier|eslint)\b")


def is_safe_build_command(sub: str) -> bool:
    m = _CARGO_RE.match(sub)
    if m and m.group(1) in SAFE_CARGO_SUBCOMMANDS:
        return True

    m = _NPM_RUN_RE.match(sub)
    if m and m.group(1) in SAFE_NPM_RUN_SCRIPTS:
        return True

    if _NPM_BUILTIN_RE.match(sub):
        return True
    if _NPM_INSTALL_RE.match(sub):
        return True
    if _NPX_RE.match(sub):
        return True

    return False


# ── Safe make targets ────────────────────────────────────────────────────────
#
# Closed-world allowlist (cpp#45 / mika#1639; architect session 783d4a04, n=3
# permission-policy-errs-strict class): only explicitly-enumerated read-only
# `make` targets auto-approve. `make verify-bundled-skills` is the bundled-skill
# pre-merge gate (mika#1575) CI runs on every PR — read-only, no side effects
# beyond stdout/exit code, same class as the cargo/npm verification commands.
#
# Stricter than _CARGO_RE: the pattern is full-anchored (`...\s*$`), so NO
# trailing tokens are allowed. `make` arguments can override variables and
# change behavior, so a trailing token must NOT ride the allowed prefix. Chain
# safety (`make verify-bundled-skills && rm -rf ~`) is handled upstream by
# _split_compound_command + the all-subs-safe check in is_safe_bash_command, not
# here. Each new target needs its own evidence-gated ticket (cpp#34 discipline).

SAFE_MAKE_TARGETS: frozenset[str] = frozenset({"verify-bundled-skills"})

_MAKE_RE = re.compile(r"^\s*make\s+(\S+)\s*$")


def is_safe_make_command(sub: str) -> bool:
    m = _MAKE_RE.match(sub)
    return bool(m and m.group(1) in SAFE_MAKE_TARGETS)


# ── Safe shell commands ──────────────────────────────────────────────────────

SAFE_SHELL_COMMANDS: frozenset[str] = frozenset({
    # Read-only inspection. `awk` and `sed` excluded by design (cpp#27):
    # both are general-purpose interpreters with arbitrary-code-execution
    # sub-features (awk `system()`/`print|"cmd"`/`getline|"cmd"`/`BEGIN{cmd}`,
    # GNU sed `e` command/flag) that an exhaustive sub-feature guard can't
    # enumerate safely. Both route to policy/relay where intent is judged
    # explicitly. See plan: docs/plans/2026-06-08-001-fix-27-tier1-drop-awk-sed-plan.md
    "ls", "cat", "head", "tail", "wc", "find", "grep",
    "echo", "printf", "dirname", "basename",
    # `xargs` is NOT read-only on its own — it runs an inner command. Membership
    # here only passes the SAFE_SHELL_COMMANDS gate; the actual safety decision is
    # made by the `xargs` special-case in is_safe_shell_command (cpp#40), exactly
    # like `find` is special-cased to _is_safe_find_command.
    "xargs",
    "realpath", "readlink", "stat", "file", "which", "type",
    "pwd", "date", "sort", "uniq", "tr", "cut", "diff",
    "comm", "test", "[",
    # Navigation — safe leaf so compound `cd <path> && <tier1>` auto-approves.
    # `cd` has no write side effects; path-traversal risk is addressed by the
    # TIER3 command-substitution blockers ($(...), backticks, <(...)) that
    # run on the raw compound before splitting.
    "cd",
    # `command` is NOT read-only on its own — it runs an inner command, bypassing
    # shell functions/aliases. Membership here only passes the SAFE_SHELL_COMMANDS
    # gate; the actual safety decision is made by the `command` special-case in
    # is_safe_shell_command (cpp#60): the read-only `command -v <name>` lookup, or
    # an inner command that is itself tier1-safe (recursive) — exactly like `find`
    # (_is_safe_find_command) and `xargs` (_is_safe_xargs_command) are special-cased.
    "command",
})

_FIRST_WORD_RE = re.compile(r"^\s*(\S+)")

# Closed-world allowlist of read-only commands permitted after find's exec-class
# flags (cpp#33). find runs the inner command DIRECTLY (no shell), so the first
# token after the flag is the binary that executes. We match it by exact-literal
# equality against this set — we never parse the inner command's arguments or
# semantics. This is the same shape ratified for the cpp#34 substitution
# allowlist (docs/solutions/security-issues/command-string-policy-allow-rules-are-compound-unsafe.md §4):
# over-blocking is the correct failure mode; widening the set is an
# evidence-gated follow-up, not a code change made on a hunch.
#
# An entry belongs here ONLY if the binary cannot execute another command or
# write a file through its OWN flags (we don't parse those flags). `rg`
# (ripgrep) was REMOVED before merge: `rg --pre <CMD>` / `--hostname-bin` /
# `--search-zip` execute external commands, so `find -exec rg --pre evil` is a
# proven-live RCE (cpp#33 security review). The native Grep tool (ripgrep-backed)
# covers the search use case without the exec surface.
#
# LOAD-BEARING PRECONDITION (cpp#44, RESOLVED): `grep`/`egrep`/`fgrep` are
# read-only ONLY under GNU grep. `ugrep` (a drop-in `grep` on some
# Gentoo/BSD/Homebrew hosts) adds `--filter=CMD` / `--pager` / `--view`, which
# execute commands — the same RCE class as `rg --pre` (which got `rg` dropped in
# cpp#33). This allowlist also backs `xargs <cmd>` (cpp#40), so the precondition
# governs both `find -exec grep` and `xargs grep`.
#
# Resolution (cpp#44): the cpp#33 security review empirically verified that the
# pilot's standard-Linux deployment containers resolve `find -exec` to GNU
# `/bin/grep` 3.12, which REJECTS `--filter`/`--pager`/`--view`. The ugrep exec
# vector is therefore NOT live in the deployment target. Decision: keep
# `grep`/`egrep`/`fgrep` (dropping them would defeat cpp#33 — its founding
# incidents mika#1381/#1572 are exactly `find -exec grep -l`), and treat the
# GNU-grep premise as an ACCEPTED + tracked risk documented right here.
#
# Hardening boundary: NEVER denylist `--filter`/`--pager`/`--view` by parsing the
# inner command's arguments — inner-arg lexing is forbidden (solution-doc §4). If
# a host that presents ugrep as `grep` ever enters scope, DROP the grep-family
# entries instead. A defense-in-depth startup ugrep-detection warning could live
# in `cli.py` (NOT this pure subprocess-free classifier) and is intentionally not
# added here. Do not add a new grep-family entry without re-checking this premise.
FIND_EXEC_SAFE_COMMANDS: frozenset[str] = frozenset({
    "grep", "egrep", "fgrep",
    "cat", "head", "tail", "wc",
    "ls", "stat", "file",
    "basename", "dirname", "readlink", "realpath",
    "echo", "printf",
})

# `-delete` is a built-in find action that removes matched files — always deny.
_FIND_DELETE_RE = re.compile(r"-delete\b")
# find's file-WRITING actions: `-fprintf FILE FORMAT` writes attacker-controlled
# content to an arbitrary FILE; `-fprint`/`-fprint0`/`-fls` write filenames /
# listings to FILE. None are exec or `-delete`, so they bypass the other guards
# and would otherwise fall through to the pure-search allow path — an arbitrary
# file-write primitive (cpp#33 security review, proven vs real bash). Deny them.
# `\b` keeps `-fprint` from being a false prefix of `-fprintf`/`-fprint0`; the
# stdout forms (`-printf`/`-print`/`-print0`/`-ls`) are not matched and stay
# allowed.
_FIND_WRITE_RE = re.compile(r"-(?:fprintf|fprint0|fprint|fls)\b")
# `-exec`/`-execdir`/`-ok`/`-okdir` all run an external command; capture the
# first token after each (the executed binary). Longest alternative first so
# `-execdir`/`-okdir` aren't mis-split as `-exec`/`-ok`. `-ok`/`-okdir` are
# folded in here (cpp#33) — they are exec-class (prompt-then-run) and were a
# pre-existing auto-approval gap when only `-exec`/`-execdir` were guarded.
_FIND_EXEC_INNER_RE = re.compile(r"-(?:execdir|exec|okdir|ok)\b\s+(\S+)")


def _contains_substitution(sub: str) -> bool:
    """True if *sub* contains any command-substitution marker (`$(`, backtick,
    `$'`). Used as a defense-in-depth guard by the exec-class allowlist gates
    (`_is_safe_find_command`, `_is_safe_xargs_command`): a read-only `find`/`xargs`
    invocation never needs substitution, so its presence smuggles execution.
    Shared so the two gates cannot drift. Note `is_safe_bash_command` also runs
    `contains_unquoted_metacharacter` first, which catches unquoted and
    double-quoted substitution; this substring check additionally vetoes the
    single-quoted (inert) form — the safe-direction over-block."""
    return "$(" in sub or "`" in sub or "$'" in sub


def _is_safe_find_command(sub: str) -> bool:
    """Decide whether a `find` invocation is safe to auto-approve (cpp#33).

    Safe iff it neither deletes, writes to a file, nor execs a non-read-only
    command:

    - `-delete` modifies the filesystem → deny.
    - `-fprintf`/`-fprint`/`-fprint0`/`-fls` write to an arbitrary FILE → deny
      (a write primitive that is neither exec nor `-delete`).
    - `-exec`/`-execdir`/`-ok`/`-okdir` run an external command → allow only
      when EVERY such inner command is in FIND_EXEC_SAFE_COMMANDS (exact-literal
      match; no inner-argument parsing).
    - Any command substitution (`$(`, backtick, `$'`) anywhere in the find
      invocation → deny. A legitimate read-only `find … -exec grep PATTERN …`
      never needs substitution; bash expands `$()`/backtick BEFORE find runs, so
      their presence means an outer substitution is smuggling execution. This
      guard makes the find path sound independent of whether
      ``contains_unquoted_metacharacter`` catches double-quoted `$()` (it does
      NOT today — see the separately-filed broader-gap ticket). Mirrors the
      permissions.py cpp#34 §4 rule that backtick/`$'` are never allowlistable.
    - No exec-class clause and no `-delete` → a pure read-only search → allow.

    `sh -c`/`bash -c` inside `-exec` are denied here (not in the allowlist) and
    independently by the TIER3 `sh -c`/`bash -c` patterns (defense in depth).
    """
    if _FIND_DELETE_RE.search(sub) or _FIND_WRITE_RE.search(sub):
        return False

    inner_commands = _FIND_EXEC_INNER_RE.findall(sub)
    if not inner_commands:
        return True  # pure search — no exec-class clause, no -delete

    if _contains_substitution(sub):
        return False

    return all(inner in FIND_EXEC_SAFE_COMMANDS for inner in inner_commands)


# `xargs` short flags that take a REQUIRED SEPARATE value token (e.g. `-I {}`,
# `-n 1`, `-d ,`, `-P 4`). When one of these appears as its own token, the NEXT
# token is its value, not the inner command — skip both. Attached forms (`-n1`,
# `-I{}`, `-P4`) and value-less flags (`-0`, `-r`, `-t`, `-x`, `-p`) are a single
# token and skip just themselves.
#
# This set lists ONLY getopt *required-argument* short flags. The deprecated
# `-e[eof]`/`-i[replace]`/`-l[lines]` are getopt *optional-argument* forms — an
# optional argument is taken ONLY when attached (`-i{}`), NEVER as a separate
# token. They are deliberately EXCLUDED: if they were here, `xargs -i rm cat`
# would skip `-i` AND `rm` (treating the real command `rm` as `-i`'s value) and
# allow on `cat` — a confirmed auto-approval of `rm` (cpp#40 security review, P0).
# Excluded, they fall to the single-token skip below, so `xargs -i rm cat`
# correctly evaluates `rm` and denies. This is a parser-arity contract with GNU
# getopt; over-block is the safe direction, NEVER under-block.
_XARGS_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s"}
)


def _is_safe_xargs_command(sub: str) -> bool:
    """Decide whether an `xargs` invocation is safe to auto-approve (cpp#40).

    Sibling to `_is_safe_find_command`: `xargs [flags] <cmd> …` runs `<cmd>` for
    each stdin record, so the safety question is identical to `find -exec <cmd>`.
    Allow iff the first non-flag token after `xargs` (the executed binary) is in
    the SAME closed-world FIND_EXEC_SAFE_COMMANDS read-only allowlist. We skip
    xargs' own flags structurally (see _XARGS_VALUE_FLAGS) but never parse the
    inner command's arguments — exact-literal match only, no inner lexing.

    Denies:
    - any command substitution (`$(`, backtick, `$'`) anywhere — a read-only
      `xargs grep …` never needs it; its presence smuggles execution (mirrors
      `_is_safe_find_command`; also caught at the scanner layer by cpp#41).
    - `xargs sh -c`/`xargs bash -c` (sh/bash not in the allowlist; also caught by
      the TIER3 `sh -c`/`bash -c` patterns — defense in depth).
    - `xargs sudo`/`xargs rm`/etc. (not in the allowlist).
    - a bare `xargs` with no inner command (defaults to `echo`, but ambiguous →
      over-block is the safe default).
    """
    if _contains_substitution(sub):
        return False

    tokens = sub.split()
    if not tokens or tokens[0] != "xargs":
        return False

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":  # explicit end-of-options; next token is the command
            i += 1
            break
        if tok.startswith("--"):
            # GNU long option. A getopt long option's value may be SEPARATE
            # (`--arg-file cat`) or `=form` (`--arg-file=cat`); we cannot know a
            # given option's arity without a full getopt table, and assuming
            # `=form`-only let `xargs --arg-file cat rm` skip just `--arg-file`,
            # land on `cat`, and allow while real xargs runs `rm` (cpp#40 security
            # review, P0). `=form` packs the value into this one token, so the
            # NEXT token is reliably the command/another flag → skip one. A BARE
            # `--long` has unknowable arity → deny (over-block). The inner command
            # may still follow `--` or an `=form` option.
            if "=" in tok:
                i += 1
                continue
            return False
        if tok.startswith("-"):
            if tok in _XARGS_VALUE_FLAGS:  # separate-value short flag → skip value
                i += 2
                continue
            i += 1  # attached-value or value-less short flag → single token
            continue
        return tok in FIND_EXEC_SAFE_COMMANDS  # first non-flag token = inner cmd

    if i < len(tokens):  # token immediately after `--`
        return tokens[i] in FIND_EXEC_SAFE_COMMANDS

    return False  # no inner command found → deny


def _is_safe_command_builtin(sub: str) -> bool:
    """Decide whether a `command` builtin invocation is safe to auto-approve (cpp#60).

    `command [-pVv] <name> [arg ...]` runs <name> while bypassing shell functions
    and aliases — so, exactly like `find -exec` (cpp#33) and `xargs` (cpp#40),
    safe-listing `command` without restricting the inner command lets that inner
    command run unchecked. Membership of `command` in SAFE_SHELL_COMMANDS only
    passes the gate; THIS function is the actual guard.

    Allow iff:
    - the read-only lookup form `command -v <name>` / `command -V <name>` — the
      `which`-equivalent the original entry intended; preserves the dev-pilot
      footprint (`command -v lefthook`, `command -v cargo && cargo test`), OR
    - the inner command is itself a tier1-safe SHELL command, decided by recursing
      through `is_safe_shell_command`. So `command` is never MORE permissive than
      the inner command alone (`command grep foo` allows because `grep foo` does;
      `command cp …`/`command tee …`/`command mkdir …` deny because the bare forms
      do). It is intentionally NARROWER: the recursion re-enters only the shell
      allowlist (+ the find/xargs/command sub-guards), NOT the full
      `_is_safe_sub_command` dispatch — so `command cargo test`/`command git status`/
      `command gh …` deny even though their bare forms auto-approve via the build/
      git/gh allowlists. That over-block (an extra relay round-trip, never a hole)
      mirrors the read-only posture of `find`/`xargs`; the live dev-pilot idiom is
      the `command -v <tool> && <tool>` lookup form above, which is unaffected.

    Denies:
    - any command substitution (`$(`, backtick, `$'`) anywhere — a read-only
      `command …` never needs it; its presence smuggles execution (shared
      `_contains_substitution`, mirrors find/xargs; also caught at the scanner
      layer by cpp#41).
    - a leading flag other than `-v`/`-V` (e.g. `-p`, which runs with a default
      PATH and is NOT a read-only lookup; `--help`). Closed-world: widening needs
      an evidence-gated ticket, never a hunch (cpp#34 discipline).
    - a bare `command` with no inner token (ambiguous → over-block).
    - `command sh -c …`/`command bash -c …`/`command sudo …` and every other
      non-safe-listed inner command, via the recursion (sh/bash/sudo are not in
      SAFE_SHELL_COMMANDS; sh -c/bash -c also caught by the TIER3 patterns).

    Recursion terminates: each call strips the leading `command` token, so the
    re-classified string strictly shrinks.
    """
    if _contains_substitution(sub):
        return False

    tokens = sub.split()
    if not tokens or tokens[0] != "command":
        return False

    rest = tokens[1:]
    if not rest:
        return False  # bare `command` — no inner command to classify

    if rest[0] in ("-v", "-V"):
        return True  # read-only lookup form (which-equivalent)

    if rest[0].startswith("-"):
        return False  # closed-world: -p/--help/etc. are not read-only lookups

    return is_safe_shell_command(" ".join(rest))


def _is_safe_sort_command(sub: str) -> bool:
    """Decide whether a `sort` invocation is safe to auto-approve (cpp#64).

    `sort` is in SAFE_SHELL_COMMANDS because the common shape `sort <file>` is
    read-only — but `sort -o FILE` (and `--output=FILE` / `--output FILE`) writes
    its sorted output to an arbitrary FILE. That output flag is a `sort` built-in,
    NOT a shell redirect, so neither the Tier-2 policy nor the Tier-3 `>` pattern
    catches it: it is a tier1-reachable arbitrary-file-write primitive, including
    the control plane (`.git/hooks/*`, `.github/workflows/*`, `.claude/*`). Same
    architectural move as cpp#33 (`find -fprintf` write) / cpp#60 (`command tee`):
    the entry stays in SAFE_SHELL_COMMANDS as a marker; THIS function is the real
    guard, enforcing the §6(a) precondition that an allowlist entry is only as
    safe as the read-only premise of its own flags (see
    docs/solutions/security-issues/command-string-policy-allow-rules-are-compound-unsafe.md).

    Closed-world: DENY any invocation carrying the output flag, in any of its
    shapes; ALLOW the read-only forms (`sort file`, `sort -k 2 file`,
    `sort -u file`, a pipe segment `… | sort`). Denial routes to policy/relay —
    the destination is NOT validated here (that is cpp#42's layer, reached once
    the command routes through Tier 2). Over-block is the safe direction.

    Denies:
    - `-o FILE` / `-oFILE` (attached) / a cluster whose `-o` is reached before
      any value-taking flag (e.g. `-uo FILE`). The cluster is walked
      left-to-right with getopt semantics so a value-taking flag (-k/-S/-t/-T)
      consumes the rest of the token — `-to` / `-T/tmp/log` carry an `o` in
      their value, not the output flag, and stay allowed.
    - `--output` / `--output=FILE` and every GNU getopt prefix abbreviation
      down to `--o` (long forms).
    - any command substitution (`$(`, backtick, `$'`) anywhere — a read-only
      `sort` never needs it; its presence smuggles execution (shared
      `_contains_substitution`, mirrors find/xargs/command).

    A `--` end-of-options token stops flag scanning: tokens after `--` are
    positional file operands, never flags, so `sort -- -o` sorts a file literally
    named `-o` (no write) and is allowed.
    """
    if _contains_substitution(sub):
        return False

    tokens = sub.split()
    if not tokens or tokens[0] != "sort":
        return False

    for tok in tokens[1:]:
        if tok == "--":
            break  # end of options — the rest are file operands, never flags
        if tok.startswith("--"):
            # Long option. GNU getopt accepts any UNAMBIGUOUS PREFIX abbreviation
            # of a long option, and `--output` is `sort`'s only `--o…` option, so
            # `--output`, `--outpu`, `--outp`, `--out`, `--ou`, `--o` — each with
            # `=FILE` or a separate value — ALL reach the write path. An exact
            # `--output` match would miss every abbreviation (the cpp#64 review's
            # founding bypass). Deny any long token whose name (before `=`) is a
            # non-empty prefix of `--output` (i.e. `--o` … `--output`). No
            # read-only `sort` long option begins with `--o`, so this over-blocks
            # nothing legitimate.
            name = tok.split("=", 1)[0]
            if len(name) >= 3 and "--output".startswith(name):
                return False  # output write flag (full or abbreviated)
            continue  # other long flag (e.g. --key=, --reverse)
        if tok.startswith("-") and len(tok) > 1:
            # Short-flag token (cluster + optional attached value). Walk the
            # cluster left-to-right with getopt semantics: `-o` is the output
            # write flag → deny; the OTHER value-taking short flags
            # (-k/-S/-t/-T) consume the REST of the token as their attached
            # value, so an `o` after one of them is data, not the output flag →
            # stop scanning. No-arg flags (-u/-n/-r/…) skip to the next char.
            # This distinguishes `-uo` (cluster -u -o → write → deny) and `-oF`
            # (output to file F → deny) from `-to` / `-T/tmp/log` (separator /
            # temp-dir value containing `o`, read-only → allow). A bare `o` is
            # always the output flag because `-o` is `sort`'s only `o` short
            # flag.
            for ch in tok[1:]:
                if ch == "o":
                    return False  # output write flag
                if ch in ("k", "S", "t", "T"):
                    break  # value-taking flag — rest of token is its value
            continue
        # positional / value token → skip
    return True


def is_safe_shell_command(sub: str) -> bool:
    match = _FIRST_WORD_RE.match(sub)
    if not match:
        return False

    cmd = match.group(1)
    if cmd not in SAFE_SHELL_COMMANDS:
        return False

    if cmd == "find":
        return _is_safe_find_command(sub)

    if cmd == "xargs":
        return _is_safe_xargs_command(sub)

    if cmd == "command":
        return _is_safe_command_builtin(sub)

    if cmd == "sort":
        return _is_safe_sort_command(sub)

    return True


# ── Safe GitHub CLI commands ─────────────────────────────────────────────────

SAFE_GH_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "pr":       frozenset({"create", "view", "list", "checkout", "diff", "checks"}),
    "issue":    frozenset({"view", "list", "edit", "comment"}),
    "run":      frozenset({"view", "list"}),
    "repo":     frozenset({"view"}),
    "release":  frozenset({"view", "list"}),
    "workflow": frozenset({"view", "list"}),
    # `auth status` is read-only — surfaces which gh installation is active,
    # which scopes are granted, and whether the cached token works. The
    # output never includes the raw token value. Other `gh auth` verbs
    # (login, logout, refresh, setup-git, token) MUST stay out — `token`
    # emits secret to stdout, the rest are mutation/auth-flow operations.
    "auth":     frozenset({"status"}),
}

_GH_DOMAIN_RE = re.compile(r"^\s*gh\s+(\S+)\s+(\S+)")
_GH_API_RE = re.compile(r"^\s*gh\s+api\b")
_GH_API_MUTATION_RE = re.compile(r"-(X|method)\b|-(f|F|field|raw-field)\b|--input\b")


def is_safe_gh_command(sub: str) -> bool:
    match = _GH_DOMAIN_RE.match(sub)
    if match:
        allowed = SAFE_GH_SUBCOMMANDS.get(match.group(1))
        if allowed is not None:
            return match.group(2) in allowed

    if _GH_API_RE.match(sub):
        if _GH_API_MUTATION_RE.search(sub):
            return False
        return True

    return False


# ── Safe intra-platform agent dispatch ───────────────────────────────────────
#
# Narrow allow-list for `mika ask --agent <agent>` calls between platform
# agents. The `mika-arch` first-pass / second-pass groom briefs, dev-pilot
# acceptance pings, and qa-review escalations all flow through this verb.
# Mirrors the prose entry at mika/skills/bundled/permission-policy/system_prompt.md:21.
#
# Sentinel cross-ref: mika/crates/mika-agent/src/well_known_agents.rs:386-396
# documents this as a deliberately duplicated list across languages with a
# "if it grows beyond 5 entries OR diverges, escalate to build-time codegen"
# callout. 3 entries < 5, so manual duplication is acceptable for Phase A.

INTRA_PLATFORM_AGENTS: frozenset[str] = frozenset({
    "mika-arch",
    "mika-dev",
    "mika-qa",
})

_MIKA_DISPATCH_RE = re.compile(r"^\s*mika\s+ask\s+--agent\s+(\S+)\b")


def is_safe_mika_dispatch(sub: str) -> bool:
    match = _MIKA_DISPATCH_RE.match(sub)
    if not match:
        return False
    return match.group(1) in INTRA_PLATFORM_AGENTS


# ── Write/Edit path safety ───────────────────────────────────────────────────


def is_within_project(file_path: str, cwd: str) -> bool:
    """Check whether a file path resolves within the project directory.

    Uses Path.resolve(strict=False) which resolves symlinks on existing
    components and leaves non-existent tails as-is — equivalent to the TS
    realpathSync with parent-dir fallback for new files.
    """
    if not file_path:
        return False

    try:
        resolved_cwd = Path(cwd).resolve(strict=True)
    except OSError:
        return False

    abs_path = (resolved_cwd / file_path).resolve(strict=False) if not Path(file_path).is_absolute() else Path(file_path).resolve(strict=False)

    try:
        abs_path.relative_to(resolved_cwd)
        return True
    except ValueError:
        return False
