"""Tier 1 auto-approval filter. Port of src/tier1.ts.

Returns True if a tool request is safe to auto-approve without relaying to the
external agent. Security principle: deny-list first, conservative default.
When in doubt, return False (relay decides).

Note: Bash shell commands do NOT get path-containment checks (unlike
Write/Edit), with ONE named exception (cpp#207): `bash <p>` / `sh <p>` /
`./<p>` is admitted only when `<p>` is a git-TRACKED, relative,
worktree-contained script — see `is_safe_tracked_repo_script_invocation`
below. Every other Bash shape still gets no path-containment check. Static
analysis of shell redirect/copy targets remains impractical in general; only
commands with no write side effects, or (for this one class) a git-reviewed
script, are safe-listed.

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
import shlex
import subprocess
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
        return is_safe_bash_command(command, cwd)

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


# cpp#234 + cpp#235: ONE definition of "shell `eval` at command position", used
# by BOTH admission (`TIER3_PATTERNS`, below) and lethality
# (`_TIER3_VERB_PATTERNS_FOR_LETHALITY`). Bare `\beval\s` matched `eval` as an
# ORDINARY ARGUMENT (`cargo test --test eval <name>`, `node --eval`,
# `--test=eval`) — the cargo integration-test target name, never the builtin.
# This anchors `eval` to a command position: start / after a pipe-or-separator
# `[|&;\n(` / a backtick / a command-opening keyword (`then|do|else|elif|!`),
# optionally behind an exec-prefix chain (`sudo|env|exec|command|nohup|time|
# xargs`). cpp#234 WIDENED anchor (backtick / keywords / exec-prefixes) so a real
# `eval` behind `sudo`/`env`/`` ` ``/`then` stays flagged. The `env` prefix only
# matches `env … eval`, never `env VAR= cargo` (a different rule refuses that).
_EVAL_COMMAND_POSITION_RE: re.Pattern[str] = re.compile(
    r"(?:^|[|&;\n(`]|(?:\b(?:then|do|else|elif)|!)\s+)"
    r"\s*"
    r"(?:(?:sudo|env|exec|command|nohup|time|xargs)(?:\s+-\S+)*\s+)*"
    r"eval\s"
)


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
    # cpp#235: admission adopts the same command-position `eval` definition as
    # lethality (shared object above). `cargo test --test eval <name>` /
    # `node --eval` / `--test=eval` are no longer flagged (the word is a test
    # target name, not the builtin); a real command-position `eval` still is.
    _EVAL_COMMAND_POSITION_RE,
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


# ── cpp#205: proven-danger VERBS for LETHALITY (case a generalization) ──────
#
# mika#1686 comment 5844642872 named a class cpp#130/#154/#155/#157/#196/#201/
# #203 had each carved an exemption from one shape at a time: a policy denial
# whose cause is pure FORM (a classifier that could not parse/prove a target
# safe) was ending the run, not merely refusing the command. cpp#205 is the
# doctrine-level generalization Prime + Vincent ratified ("Oui. A", 2026-09-26):
# invert `_denial_is_terminal`'s DEFAULT to SURVIVABLE, terminal only for a
# named, enumerated PROVEN-DANGER set (case a) — a syntactic classifier cannot
# prove a target safe for these VERBS, so the verb itself stays lethal
# regardless of what it is pointed at, exactly the reasoning TIER3_PATTERNS'
# own `rm -rf` entry already embodies.
#
# TIER3_PATTERNS' OWN TRAILING ENTRY — the generic bare `>`/`>>` catch-all —
# is the ONE entry in that tuple that is NOT this kind of unprovable verb: it
# names a FILE TARGET, and a file target is exactly what
# `_redirect_destination_veto_reason` / `permissions._destination_veto_reason`
# (called with `for_lethality=True`) CAN prove safe or unsafe by resolving it
# against `cwd` (cpp#38/#42/#154/#155/#176/#195/#201). Excluding only that one
# trailing entry here, and letting `_denial_is_terminal`'s cwd-aware
# destination-veto calls decide redirect-target lethality instead, fixes a
# real over-refusal, measured on this HEAD (cpp#205 plan doc audit table): the
# purely lexical, cwd-free `_is_contained_redirect_target` (below) only ever
# exempts a target that is worktree-RELATIVE or lexically under `/tmp/` — it
# has no way to see that an ABSOLUTE path actually RESOLVES inside the
# worktree. `_destination_veto_reason` already got that fallthrough (cpp#176's
# own fix, for the REFUSAL question, via its `is_within_project` resolution)
# but this purely-lexical function never did — cpp#154 D1 deliberately keeps
# it cwd-free. A generic-verb command redirecting to its own absolute worktree
# path (the exact mika#1719/cpp#176 shape — `/usr/bin/time -v cargo build …
# > /data/workspace/.../worktrees/.../out`, reproduced here for ANY verb, not
# only `cp`/`mv`/`mkdir`/`git show` which cpp#176 already fixed) matched this
# trailing entry and stayed terminal even though the write never left the
# worktree.
#
# Removing it from the verb-only set does NOT weaken any existing terminal
# case: `_destination_veto_reason` is a documented, pre-existing STRICT
# SUPERSET of what a redirect target needs for the REFUSAL question — every
# out-of-worktree, control-plane, `~`/`$`/`..`-disqualified, or unparseable
# redirect target this trailing entry used to catch is independently proven
# dangerous by that function too (verified case by case in the cpp#205 plan
# doc's audit table) — this was already true before cpp#205 (that function's
# own docstring already claimed the superset) and is unaffected by this change.
# mika#2573 (case A) + cpp#234/#235: the lethality verb set is `TIER3_PATTERNS`
# minus its trailing generic-redirect catch-all. The `eval` entry there IS the
# shared `_EVAL_COMMAND_POSITION_RE` (command position, widened anchor — see its
# block comment above), so ADMISSION and LETHALITY consult ONE definition of
# "shell eval": no per-entry swap, no duplicated regex. `cargo test --test eval`
# / `node --eval` / `--test=eval` are neither refused nor terminal; a real
# command-position `eval` (incl. behind backtick/then/sudo/env) is both.
_TIER3_VERB_PATTERNS_FOR_LETHALITY: tuple[re.Pattern[str], ...] = TIER3_PATTERNS[:-1]

# NEW verb patterns, LETHALITY-ONLY. NOT added to `TIER3_PATTERNS`, NEVER
# consulted by `is_tier3_dangerous` (the REFUSAL) or by `is_safe_bash_command`/
# `is_tier1_auto_approve`/any YAML rule — admission is completely untouched.
# A command matching one of these is ALREADY refused today via the ordinary
# tier1/policy default-deny path (none of these verbs is in
# `SAFE_SHELL_COMMANDS` or admitted by any YAML allow rule); what changes is
# only whether that refusal also ends the run.
#
# Measured on this HEAD (cpp#205 plan doc audit table): before this change,
# `chmod -R`/`chown -R`/`dd`/`mkfs`/`truncate`/a fork bomb were, in fact,
# ALREADY non-terminal — none of them ever matched any existing
# `TIER3_PATTERNS` entry, so `is_tier3_dangerous_for_lethality` returned
# `False` for every one of them and `_denial_is_terminal` fell through to
# `False` (survivable) by the pre-cpp#205 default's own construction. This is
# a genuine UNDER-terminal gap the ticket's case (a) closes on purpose: their
# blast radius (a recursive permission/ownership change, a raw block-device
# write, filesystem creation, file truncation, a fork bomb) is the same class
# `rm -rf` already names in `TIER3_PATTERNS`, just never enumerated there. A
# bare, non-recursive `chmod`/`chown` (a single-file mode change) is
# deliberately NOT included — cpp#205's own audit named `chmod 000 x` as an
# example of a denial ALREADY correctly survivable, and this ticket's mandate
# is case (a), "regardless of target," which applies to the RECURSIVE form
# specifically (a classifier cannot prove a recursive target's blast radius
# safe); the non-recursive form is an ordinary single-file write, exactly the
# kind of target a destination veto (not a verb ban) already governs when the
# command also redirects, and stays covered by that path unchanged.
_PROVEN_DANGEROUS_VERB_PATTERNS_CPP205: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bchmod\s+(?:-\w*R\w*|--recursive\b)"),  # chmod -R / --recursive
    re.compile(r"\bchown\s+(?:-\w*R\w*|--recursive\b)"),  # chown -R / --recursive
    re.compile(r"\bdd\b"),                                 # dd (raw block-device copy)
    re.compile(r"\bmkfs(?:\.\w+)?\b"),                     # mkfs / mkfs.ext4 / …
    re.compile(r"\btruncate\b"),                           # truncate -s …
    # Classic fork bomb, whitespace-tolerant: :(){ :|:& };:
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&?\s*\}\s*;\s*:"),
)


def _matches_proven_dangerous_lethality_verb(stripped_command: str) -> bool:
    """Whether *stripped_command* — already put through
    `is_tier3_dangerous_for_lethality`'s quote-mask / `/dev/null` / contained-
    redirect strips — matches a verb this module proves dangerous regardless
    of its target (cpp#205 case a): the union of every `TIER3_PATTERNS` entry
    EXCEPT its own trailing generic-redirect catch-all
    (`_TIER3_VERB_PATTERNS_FOR_LETHALITY`), plus the new verbs enumerated in
    `_PROVEN_DANGEROUS_VERB_PATTERNS_CPP205`. Never consulted by the REFUSAL
    path (`is_tier3_dangerous`); LETHALITY only."""
    return any(
        p.search(stripped_command)
        for p in (
            *_TIER3_VERB_PATTERNS_FOR_LETHALITY,
            *_PROVEN_DANGEROUS_VERB_PATTERNS_CPP205,
        )
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


# ── cpp#203: `sed -i` targeting the inert /dev/null sink is not lethal ───────
#
# mika#1686 comment 5844642872 (deny-death instance #1, mika#2532 impl): the
# incident command
#     sed -i 's/.../ X/' /dev/null; grep -n "created_by_session" crates/m.rs
# measures `policy=allow(rule_id=bash-grep)` (first-match on the innocent
# `grep`, which would run) but `_denial_is_terminal=True` — the `sed -i`
# segment alone kills the session. TIER3_PATTERNS' `sed -i` entry (`:172`)
# matches on the FLAG alone, regardless of target — correct for the REFUSAL
# (`sed -i` is not an allow-listed idiom, so the command must stay denied)
# but wrong for LETHALITY when the target writes nowhere.
#
# Unlike cpp#130's `>/dev/null` just above, `/dev/null` here is NOT a shell
# redirect target — it is sed's own in-place-edit FILE ARGUMENT, a different
# grammar position entirely. No `<`/`>` character appears anywhere in
# `sed -i 's/a/b/' /dev/null`, so none of `_STDOUT_DEVNULL_RE`,
# `_strip_contained_redirects`, or the `_REDIRECT_RE` extraction they rest on
# ever see it. This is therefore a NEW narrowing, not a reapplication of the
# redirect-stripping machinery — but it reuses cpp#130's exact /dev/null
# RECOGNITION: the same literal target text and the same trailing-boundary
# lookahead `(?![/\w.])`, so `/dev/null.txt`, `/dev/nullified`, and
# `/dev/null/../etc/passwd` stay exactly as fatal as cpp#130 already keeps
# them for redirects (no new recognition invented, per cpp#203 scope B).
#
# Scoped deliberately narrow — lethality only, no admission change:
#   - Only the bare flag shape `TIER3_PATTERNS` itself matches (`-\w*i|
#     -i\w*`, same alternation) — so this narrowing can never fire on a
#     command the flag pattern would not have matched anyway. NARROWER than
#     that shape in one respect: whitespace is required immediately after
#     the flag, so a backup-suffix flag glued to `-i` (`-i.bak`, `-i_orig`)
#     does not match here and stays lethal — an intentional, undemonstrated
#     shape, not the mika#1686 incident, left on the fail-closed side.
#   - At most ONE script/expression argument between the flag and the
#     target — single-quoted, double-quoted, or one bare token (no
#     separator characters). A second flag, a second script argument, or
#     anything else between the flag and `/dev/null` is not this shape and
#     is left untouched: the regex fails to match and the command stays
#     lethal (fail-closed).
#   - `/dev/null` must be the LAST token before a compound-command
#     separator (`;`, `&`, `|`, `)`) or end-of-string — i.e. `/dev/null` is
#     the ONLY file operand `sed -i` receives on this segment. A SECOND,
#     real target — `sed -i 's/a/b/' /dev/null realfile.rs` — does NOT
#     match: the lookahead after `/dev/null` fails on the trailing
#     `realfile.rs`, so the whole match fails and the command stays lethal.
# A dangerous verb elsewhere in the same or a later compound segment is
# unaffected — this narrowing only ever blanks the exact `sed -i <script>?
# /dev/null` text it matched, exactly as `_STDOUT_DEVNULL_RE.sub(" ", …)`
# does for its own match, never anything outside it.
_SED_I_DEVNULL_RE = re.compile(
    r"\bsed\s+(?:-\w*i|-i\w*)\b"
    r"(?:\s+(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[^\s;&|]+))?"
    r"\s+/dev/null(?![/\w.])"
    r"(?=\s*(?:[;&|)]|$))"
)


# ── Contained redirect targets are not on their own session-fatal (cpp#154) ───
#
# cpp#130 (just above) removed ONE redirect target from the lethality class: the
# inert `/dev/null` sink. Its own docstring left the rest as debt — "widening the
# exemption to in-worktree targets is left to the destination veto
# (`permissions._destination_veto_reason`)". cpp#154 measured that the
# destination veto could not carry it AT THE TIME: `_segment_write_kind`
# classified only `cp`/`mv`, `mkdir` and `git show >`, so a redirect was
# invisible to it — `echo hi > /etc/passwd` returned
# `_destination_veto_reason = None`. cpp#154 therefore landed the widening HERE
# instead, as a LETHALITY-only narrowing. cpp#155 later taught
# `_segment_write_kind` to classify redirects too (so `_destination_veto_reason`
# now ALSO covers this class, on both the allow and deny routes), but that is
# an additive, independent check — this function keeps answering its own
# question (is an already-refused, never-executed command's redirect lexically
# contained) exactly as it did before, unaffected by cpp#155.
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


def _is_lexically_disqualified_redirect_target(dest: str) -> bool:
    """Whether a LITERAL redirect target text is disqualified OUTRIGHT — never a
    write destination any part of this module will accept, no matter what
    ``cwd``/worktree it is weighed against, and never handed to
    ``Path.resolve()`` (cpp#38's ``is_within_project``) to find out.

    This is the SAME disqualifier set `_is_contained_redirect_target` has
    always used for `~`/leading-`$`/`..`/a disallowed charset/a bare `$` — see
    that function's own docstring and cpp#154 plan D3 for why those must fail
    closed BEFORE any resolve: `is_within_project` does no shell-expansion, so
    `Path(cwd) / "~/x"` or `Path(cwd) / "$VAR/x"` would resolve to literal
    same-named subdirectories INSIDE the worktree and wrongly read as
    contained — the opposite of what bash would actually do.

    Extracted as its own predicate (cpp#176) so `_destination_veto_reason` can
    tell "disqualified outright" apart from "merely fails the /tmp/ prefix
    literal" — an absolute path that fails ONLY the latter is not disqualified
    here: it is a worktree-containment CANDIDATE the caller may still route
    through `is_within_project`. `_is_contained_redirect_target` itself is
    unchanged in behavior — it is now just this predicate plus its own final
    `/tmp/`-prefix literal — so every existing caller of that function keeps
    its exact verdict.
    """
    if not dest:
        return True
    if ".." in dest:
        return True
    if dest.startswith("~") or dest.startswith("$"):
        return True
    if _CONTAINED_REDIRECT_TARGET_RE.match(dest) is None:
        return True
    if _BARE_DOLLAR_RE.search(dest) is not None:
        return True
    return False


# ── mktemp-scratch VARIABLE targets, for LETHALITY only (cpp#201) ────────────
#
# `_is_lexically_disqualified_redirect_target` (just above) answers ONE
# question — "never a write destination this module will accept" — and that
# answer is correct and UNCHANGED for the REFUSAL: `~`, `..`, a bad charset,
# and any `$`-bearing operand are all denied fail-closed alike, because none
# of them can be proven safe. This block does NOT touch that predicate.
#
# The temptation (rejected, kept here as a named non-choice so it is not
# re-attempted) was to read ANY `$`-bearing disqualified target as merely
# "cannot be proven ANYTHING, therefore not worth killing the session over".
# That reading is WRONG and was caught by the test suite itself:
# `test_cpp154_home_expansion_target_stays_terminal` /
# `test_leading_expansion_target_stays_lethal` /
# `test_real_redirect_stays_lethal` (cpp#154 D3, cpp#157 AC replay) pin
# `$HOME/x`, `${HOME}/x`, `$OLDPWD/y`, `$(whoami)` and a bare `$` as staying
# TERMINAL, on purpose: "`$HOME/x` names the same destination as `~/x`;
# admitting it would make the `~` rejection one respelling away from
# useless." A blanket `$`-means-survivable rule silently reopens exactly
# that hole — an actual regression on a ratified invariant, not a
# conservative-but-safe widening. Confirmed by running that batch: it fails
# 5 pre-existing tests, none of which this ticket's incident touches.
#
# The actual incident (mika#2458: `T=$(mktemp -d); cat >"$T/log"`) is a
# NARROWER and qualitatively different shape than `$HOME`: `T` is not an
# ambient environment variable an attacker (or a confused pilot) can alias
# to an arbitrary path — it is assigned, in the SAME command, from the
# literal output of `mktemp`, a fixed system utility with a well-known
# contract (a fresh path under `$TMPDIR`/`/tmp` unless a template argument
# says otherwise, which the ticket's shape never supplies). That is provable
# LEXICALLY, without resolving anything on disk (cpp#143's rule holds: never
# resolve to GRANT — this doesn't; it recognizes a fixed textual idiom, the
# same way `_is_sanctioned_pure_heredoc` recognizes the `bash-cat-heredoc-tmp`
# idiom). So the cut actually drawn is: a redirect target whose disqualified
# form is `$VAR`/`"$VAR"`/`${VAR}` (optionally followed by a safe, `..`-free
# relative tail) is treated as a `/tmp`-scratch carve-out for LETHALITY ONLY
# when the SAME command assigns `VAR` from `$(mktemp …)` or `` `mktemp …` ``
# — never for any other variable, however spelled. `$HOME`, `${HOME}`,
# `$OLDPWD`, `$(whoami)`, a bare `$` all fail the "assigned from mktemp
# here" test and stay exactly as lethal as before — the 5 tests above are
# unmodified and pass unchanged.
_MKTEMP_ASSIGNMENT_RE = re.compile(
    r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*)="
    r"(?:\$\(\s*mktemp\b[^)]*\)|`\s*mktemp\b[^`]*`)"
)

# The target-side match: an (already-disqualified) operand whose ENTIRE text
# is an optional leading quote, a `$VAR`/`${VAR}` reference, and a safe
# relative tail — no `..` segment anywhere in the tail (a traversal out of
# the mktemp directory is never carved out, even if `VAR` itself is
# legitimate), and no further `$`/other metacharacter, mirroring the same
# fail-closed charset discipline `_CONTAINED_REDIRECT_TARGET_RE` already
# applies. Anchored both ends — a target that is MOSTLY a var reference but
# has trailing garbage does not match and falls through to the ordinary
# "stays fatal" path.
_MKTEMP_SCRATCH_TARGET_RE = re.compile(
    r'^"?\$\{?(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}?(?P<tail>[\w./-]*)"?$'
)


def _mktemp_scratch_variable_names(command: str) -> frozenset[str]:
    """Every variable name the command assigns, anywhere in its raw text, from
    `$(mktemp …)` or `` `mktemp …` `` (cpp#201).

    Scans the WHOLE command text rather than per-segment: the assignment and
    the redirect that consumes it are almost always different
    `_split_compound_command` segments (`T=$(mktemp -d)` then, later,
    `cat >"$T/log"`), so a per-segment view would never see both at once.
    Deliberately does not try to tell an assignment that executes from one
    that merely appears as inert heredoc BODY text — the worst case of that
    imprecision is a slightly-too-generous LETHALITY carve-out on a command
    that is, either way, still fully REFUSED (this function is never
    consulted by anything that grants an allow); bounded and named here
    rather than solved, matching this ticket's scope.
    """
    return frozenset(m.group(1) for m in _MKTEMP_ASSIGNMENT_RE.finditer(command))


def _is_mktemp_scratch_redirect_target(command: str, dest: str) -> bool:
    """Whether the (already-disqualified) redirect target ``dest`` is rooted at
    a variable that ``command`` itself assigns from `mktemp`'s own output
    (cpp#201) — see the block comment above for the full boundary and why it
    is drawn this way rather than on the mere presence of ``$``.

    Requires the caller to have already established
    ``_is_lexically_disqualified_redirect_target(dest)`` — mirrors every
    other helper in this module, which never re-derives a precondition its
    caller already holds. Consulted ONLY for LETHALITY (cpp#201); the
    REFUSAL keeps denying every disqualified operand alike, mktemp-rooted or
    not — see `permissions._destination_veto_reason`'s per-target loop and
    `is_tier3_dangerous`, neither of which calls this.
    """
    m = _MKTEMP_SCRATCH_TARGET_RE.match(dest)
    if m is None:
        return False
    if ".." in m.group("tail"):
        return False
    return m.group("var") in _mktemp_scratch_variable_names(command)


# ── ce-* /tmp scratch sanction: uid token + same-command var tracing (mika#2562) ─
#
# The compound-engineering ce-* skills open EVERY /mika pipeline step with this
# preamble (SKILL.md step 6, single Bash string, multi-line):
#
#     SCRATCH_ROOT="/tmp/compound-engineering-$(id -u)"
#     (umask 077; mkdir -p "$SCRATCH_ROOT") || exit 1
#     chmod 700 "$SCRATCH_ROOT" || exit 1
#
# cpp#143 already sanctions a LITERAL /tmp scratch mkdir (`_is_sanctioned_tmp_
# scratch`, `_TMP_SCRATCH_MKDIR_RE = ^/tmp/(?!.*\.\.)[\w./-]+$`). Two things put
# the ce-* scratch OUTSIDE that literal recognition, and cpp#218's `$`-rooted
# `mkdir` veto (`permissions._destination_veto_reason`) then turned the resulting
# refusal TERMINAL — killing the pilot at the top of every ce-* step (mika#2562):
#
#   * AXIS A — the mkdir target is a VARIABLE (`$SCRATCH_ROOT`), not the literal.
#   * AXIS B — the assigned VALUE carries a `$(id -u)` command substitution, so
#     `_TMP_SCRATCH_MKDIR_RE`'s `[\w./-]` charset rejects it even as a literal.
#
# These two helpers extend the cpp#143 recognition on BOTH axes, each TIGHTLY
# bounded — the same discipline as cpp#201's mktemp same-command tracing
# (`_is_mktemp_scratch_redirect_target` above), which this mirrors:
#
#   * Axis B is a LITERAL WHITELIST of exactly the benign uid tokens
#     (`$(id -u)`, `` `id -u` ``, `$UID`, `${UID}`, `$EUID`, `${EUID}`) — never
#     arbitrary `$(...)` or `$VAR`. A path with ANY other substitution
#     (`/tmp/x-$(whoami)`, `/tmp/x-$(rm -rf /)`, `/tmp/$OTHER`) still fails,
#     because after replacing ONLY the whitelisted tokens the remainder must
#     match the plain-`/tmp`-scratch charset, which admits no residual
#     `$`/backtick/`(`/`)`. The uid-token regex is anchored so a decorated
#     subst (`$(id -u; rm -rf /)`, `$(id -u$(evil))`) never matches the token
#     and its metacharacters survive into the charset check, which rejects them.
#   * Axis A resolves a `$VAR`/`${VAR}` mkdir/chmod target to a `/tmp`-scratch
#     literal ONLY when the SAME command string assigns that var to such a
#     literal (uid-tolerant). A var with no same-command assignment, or one
#     assigned to a NON-`/tmp` value (`X="/etc/evil"`), is not recognized and
#     stays refused by the ordinary cpp#218 / cpp#38 veto. A traversal tail on
#     the reference (`"$SCRATCH_ROOT/../etc"`) is not a bare var reference, so it
#     never matches and stays refused too.
#
# LEXICAL only — no `Path.resolve`, no filesystem access — matching cpp#143's own
# rule (never resolve to GRANT). Consulted ONLY by `permissions._is_sanctioned_
# tmp_scratch`; `is_tier1_auto_approve` and every YAML rule are untouched.
_UID_TOKEN_RE = re.compile(
    r"\$\(\s*id\s+-u\s*\)"          # $(id -u)  (optional inner whitespace)
    r"|`\s*id\s+-u\s*`"             # `id -u`
    r"|\$\{UID\}|\$\{EUID\}"        # ${UID} / ${EUID}
    r"|\$UID(?![A-Za-z0-9_])"       # $UID   (not a longer name like $UIDFOO)
    r"|\$EUID(?![A-Za-z0-9_])"      # $EUID
)

# Same charset as `_TMP_SCRATCH_MKDIR_RE` in permissions.py — kept in lockstep
# (the uid-token substitution below reduces to exactly this after masking).
_CE_SCRATCH_TMP_RE = re.compile(r"^/tmp/(?!.*\.\.)[\w./-]+$")

# A `VAR=<value>` assignment whose value is a `/tmp/...` literal (quoted or bare).
# The double-quoted branch is what the real preamble uses and is the only one
# that can carry `$(id -u)` (the subst's internal space needs the quotes). The
# bare branch excludes `(`/`)`/quotes/separators so it never spans a subst.
_CE_SCRATCH_ASSIGN_RE = re.compile(
    r"(?<![\w$])(?P<var>[A-Za-z_][A-Za-z0-9_]*)="
    r"""(?:"(?P<dq>/tmp/[^"]*)"|'(?P<sq>/tmp/[^']*)'|(?P<bare>/tmp/[^\s"'`;|&()]+))"""
)

# A bare (shlex-stripped) variable reference: the WHOLE operand is `$VAR` or
# `${VAR}`, with at most one trailing `)` (the artifact of a `(subshell; mkdir
# -p "$V")` split). A tail (`$V/x`, `$V/../etc`) does NOT match — such an
# operand is not a bare scratch reference and stays under the ordinary veto.
_CE_SCRATCH_VARREF_RE = re.compile(
    r"^\$\{?(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}?\)?$"
)


def _is_uid_tolerant_tmp_scratch(value: str) -> bool:
    """Axis B (mika#2562): whether ``value`` is a ``/tmp`` scratch literal that
    is plain except for the whitelisted uid tokens.

    Masks ONLY the exact uid tokens, then requires the remainder to be an
    ordinary cpp#143 ``/tmp`` scratch — so any OTHER substitution or variable in
    the path survives the mask and fails the charset. See the block comment above.
    """
    if not isinstance(value, str) or not value:
        return False
    masked = _UID_TOKEN_RE.sub("0", value)
    return _CE_SCRATCH_TMP_RE.match(masked) is not None


def _ce_scratch_variable_names(command: str) -> frozenset[str]:
    """Every var name whose EFFECTIVE (LAST-WINS) same-command assignment value is
    a uid-tolerant ``/tmp`` scratch literal (mika#2562, tightened by cpp#224).

    Mirrors ``_mktemp_scratch_variable_names``: scans the WHOLE command (the
    assignment and the mkdir/chmod that consumes it are different compound
    segments), and is only ever consulted to GRANT a recognition on a command
    that is otherwise refused — so an over-generous match on inert heredoc-body
    text is bounded to a refusal that stays a refusal.

    cpp#224 (TIGHTENING): recognition is LAST-WINS. A var is admitted ONLY if its
    LAST same-command assignment roots at scratch. ``_CE_SCRATCH_ASSIGN_RE`` gives
    the candidate vars (those with at least one ``/tmp`` assignment); for each we
    re-resolve the LAST assignment across ANY value with ``_last_assignment_value``
    and require THAT to be scratch. So ``X=/tmp/ok; X=$HOME/evil`` is NOT
    recognized — its effective value is ``$HOME/evil`` — where the old "any
    assignment roots at /tmp" scan wrongly admitted it. A scratch->scratch
    reassignment (``X=/tmp/ok; X=/tmp/still-ok``) still is recognized.
    """
    names: set[str] = set()
    for m in _CE_SCRATCH_ASSIGN_RE.finditer(command):
        var = m.group("var")
        last = _last_assignment_value(command, var)
        if last is not None and _is_uid_tolerant_tmp_scratch(last):
            names.add(var)
    return frozenset(names)


def _is_ce_scratch_variable_ref(command: str, dest: str) -> bool:
    """Axis A (mika#2562): whether the (shlex-stripped) target ``dest`` is a bare
    ``$VAR``/``${VAR}`` reference to a var ``command`` itself assigns to a
    uid-tolerant ``/tmp`` scratch literal.

    Mirrors ``_is_mktemp_scratch_redirect_target``. LEXICAL only.
    """
    m = _CE_SCRATCH_VARREF_RE.match(dest)
    if m is None:
        return False
    return m.group("var") in _ce_scratch_variable_names(command)


# --- mika#2562 correction: TRANSITIVE scratch-rooting, LETHALITY ONLY ---------
# The canonical ce-* preamble builds `RUN_DIR="$SCRATCH_ROOT/ce-…/$RUN_ID"` then
# `mkdir -p "$RUN_DIR"`. `$RUN_DIR`'s value is `$<recognized-scratch-var>/<suffix>`,
# NOT a `/tmp` literal, so axis A (`_is_ce_scratch_variable_ref`) does not admit it
# and cpp#218's `$`-rooted mkdir veto fires TERMINALLY. Pre-#218 that same preamble
# was a SURVIVABLE deny (the assignment-prefixed compound is policy default-deny,
# non-terminal — the dir was never created by that command). So the regression is
# purely the terminality. This carve restores the pre-#218 posture: it is consulted
# ONLY from the `for_lethality` veto path (see `permissions._destination_veto_reason`),
# never from an admission call, so the deny STAYS a deny and NOTHING new is admitted —
# only `_denial_is_terminal` flips to False. Same shape as the cpp#201/#209/#213
# lethality carves.
#
# Resolution is LAST-WINS (`_last_assignment_value`), so a reassignment out of scratch
# (`SR=/tmp/ok; SR=$HOME/evil; RUN=$SR/x; mkdir "$RUN"`) stays FATAL. cpp#224 made the
# DIRECT axis-A ADMISSION last-wins too (`_ce_scratch_variable_names` above), so the
# direct `X=/tmp/ok; X=$HOME/evil; mkdir "$X"` form is now REFUSED + terminal — both the
# admission axis and this lethality carve resolve the var's LAST assignment.
_ANY_ASSIGNMENT_RE = re.compile(
    r'(?<![\w$])(?P<var>[A-Za-z_][A-Za-z0-9_]*)='
    r'(?:"(?P<dq>[^"]*)"|\'(?P<sq>[^\']*)\'|(?P<bare>[^\s;|&()<>]*))'
)
_TMPDIR_DEFAULT_RE = re.compile(r"^\$\{TMPDIR:?-/tmp\}(?P<rest>/.*)$")
_TRANSITIVE_VAR_PREFIX_RE = re.compile(
    r"^\$\{(?P<vb>[A-Za-z_][A-Za-z0-9_]*)\}(?P<tb>/.*)$"
    r"|^\$(?P<v>[A-Za-z_][A-Za-z0-9_]*)(?P<t>/.*)$"
)
_TRANSITIVE_SCRATCH_MAX_DEPTH = 8


def _last_assignment_value(command: str, var: str) -> str | None:
    """LAST-WINS value of the last same-command ``var=…`` assignment, else None.

    Matches ANY assignment value (not only ``/tmp`` ones), so a reassignment OUT
    of scratch is seen and overrides an earlier scratch assignment.
    """
    found: str | None = None
    for m in _ANY_ASSIGNMENT_RE.finditer(command):
        if m.group("var") == var:
            if m.group("dq") is not None:
                found = m.group("dq")
            elif m.group("sq") is not None:
                found = m.group("sq")
            else:
                found = m.group("bare")
    return found


def _is_tmpdir_default_scratch(value: str) -> bool:
    """``${TMPDIR:-/tmp}/<rest>`` treated as a ``/tmp`` scratch root (the canonical
    preamble's fallback), the ``<rest>`` held to the same uid-tolerant charset."""
    m = _TMPDIR_DEFAULT_RE.match(value)
    if m is None:
        return False
    return _is_uid_tolerant_tmp_scratch("/tmp" + m.group("rest"))


def _value_roots_at_scratch(value: str, command: str, depth: int = 0) -> bool:
    """Whether ``value`` roots (transitively, via same-command LAST-WINS assignments)
    at a recognized scratch. LEXICAL, bounded depth, refuses ``..`` in a suffix."""
    if depth > _TRANSITIVE_SCRATCH_MAX_DEPTH or not value:
        return False
    if _is_uid_tolerant_tmp_scratch(value) or _is_tmpdir_default_scratch(value):
        return True
    m = _TRANSITIVE_VAR_PREFIX_RE.match(value)
    if m is None:
        return False
    inner = m.group("vb") or m.group("v")
    tail = m.group("tb") or m.group("t")
    if ".." in tail:
        return False
    nxt = _last_assignment_value(command, inner)
    if nxt is None:
        return False
    return _value_roots_at_scratch(nxt, command, depth + 1)


def _is_transitive_ce_scratch_mkdir_target(command: str, dest: str) -> bool:
    """LETHALITY-ONLY: whether the (shlex-stripped) ``dest`` is a bare ``$VAR``
    whose LAST same-command assignment roots (transitively) at a recognized
    scratch. Consulted only from the ``for_lethality`` veto path; the deny stays.
    """
    m = _CE_SCRATCH_VARREF_RE.match(dest)
    if m is None:
        return False
    value = _last_assignment_value(command, m.group("var"))
    if value is None:
        return False
    return _value_roots_at_scratch(value, command, 0)


def _is_contained_redirect_target(dest: str) -> bool:
    """Whether a LITERAL redirect target text is contained: in-worktree (relative)
    or under ``/tmp`` (cpp#154).

    Purely lexical on the text as written — no ``Path.resolve()``, no ``stat``,
    no ``cwd``. ``/dev/null`` is covered upstream by ``_STDOUT_DEVNULL_RE`` and
    is deliberately NOT duplicated here (an absolute path outside ``/tmp/``
    returns False).
    """
    if _is_lexically_disqualified_redirect_target(dest):
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
    """Blank out each redirect whose target is contained OR a mktemp-scratch
    variable this same command assigned (cpp#201); leave every other
    redirect in place so the generic ``>`` pattern keeps matching (cpp#154).

    The ``or _is_mktemp_scratch_redirect_target(command, target)`` arm is
    cpp#201, scoped NARROWLY — see that predicate's block comment for the
    full boundary and for why a blanket "any `$` is survivable" rule was
    tried and rejected (it silently reopens the `$HOME`-as-`~`-respelling
    hole cpp#154 D3 closed; pinned by
    `test_leading_expansion_target_stays_lethal` /
    `test_real_redirect_stays_lethal` in `tests/test_tier1.py` and
    `test_cpp154_home_expansion_target_stays_terminal` /
    `test_cpp157_a_real_redirect_still_ends_the_run` in
    `tests/test_policy_devpilot.py`, none of which this arm touches). Only a
    target rooted at a variable the SAME command assigns from `mktemp`'s own
    output is exempted; `$HOME`, `${HOME}`, `$OLDPWD`, `$(whoami)`, a bare
    `$`, and any other variable stay exactly as lethal as before. The write
    itself stays refused regardless (`is_tier3_dangerous`, the REFUSAL
    classifier, is untouched; `permissions._destination_veto_reason` still
    vetoes it fail-closed for the refusal question); only whether the
    refusal ends the run changes, and only for this one named idiom.
    """
    if _redirect_targets(command) is None:
        return command

    def _replace(m: re.Match[str]) -> str:
        if m.group("ignored") is not None:
            return m.group(0)
        target = m.group("target")
        if target and (
            _is_contained_redirect_target(target)
            or _is_mktemp_scratch_redirect_target(command, target)
        ):
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
# THIRD CONSUMER OF THE SHARED QUOTE SCANNER (cpp#157 introduced it as a third
# independent state machine; cpp#158 extracted `_quote_spans`, and this is now
# one of its three call sites, not a scanner of its own). Its OWN quote-
# boundary math does not change: this function already used the atomic
# `\X`-pair rule inside AND outside quotes (cpp#157), which is exactly what
# `_quote_spans` implements — cpp#158 is a pure extraction here, zero
# behavioral delta. `_split_compound_command` and `contains_unquoted_metacharacter`
# are the two that gain correctness (see `_quote_spans`'s docstring and the
# plan doc's security section) by routing through the same scanner this
# function already had right.
#
# Length is preserved (one space per masked character), so no downstream regex
# index shifts and no two tokens can be glued together.
def _mask_quoted_redirect_chars(command: str) -> str:
    r"""Blank every ``<`` and ``>`` that falls inside a single- or double-quoted
    region; leave the rest of the command byte-for-byte intact (cpp#157).

    Purely lexical — no ``cwd``, no filesystem, ``(str) -> str`` — as
    ``permissions._is_sanctioned_tmp_scratch`` (`:922-968`) requires of everything
    on this path. Quote boundaries come from the shared `_quote_spans`
    (cpp#158) — see its docstring for the atomic ``\X`` escape rule, inside and
    outside quotes alike, that this function already implemented pre-cpp#158
    and that the shared scanner now applies to all three call sites.

    - An unterminated quote (`_quote_spans` reports ``closed=False`` on the
      trailing span) returns the command UNCHANGED (fail-closed toward
      lethal; see the header comment for why this direction is the INVERSE of
      `_split_compound_command`'s and `contains_unquoted_metacharacter`'s).
    - Inside ``"..."``, a masking walk must repeat `_quote_spans`'s own
      ``\\X`` pair-consumption (any ``X``): an escaped pair is skipped WHOLE
      and neither of its two characters is masked, so an escaped ``\\>`` (a
      literal ``>`` to bash, per the header comment) stays visible to
      ``TIER3_PATTERNS`` and stays lethal — masking it would silently exempt
      a real redirect character AC2 requires to stay fatal. Inside ``'...'``,
      there is no escape concept (backslash is a plain character), so every
      ``<``/``>`` in the region is masked unconditionally, backslash-preceded
      or not.
    """
    spans = _quote_spans(command)
    if spans and not spans[-1][2]:
        return command

    out = list(command)
    for start, end, _closed in spans:
        quote_char = command[start]
        k = start
        while k < end:
            ch = out[k]
            if quote_char == '"' and ch == "\\" and k + 1 < end:
                k += 2
                continue
            if ch in ("<", ">"):
                out[k] = " "
            k += 1
    return "".join(out)


# ── cpp#236: a `<`/`>` that is not a TOP-level outer redirect operator ────────
#
# Pilot 946d2786 (mika#2578) died on a benign `git commit -m "$(printf … 'Co-
# Authored-By: … <noreply@anthropic.com>' …)"`. The `>` closing the quoted
# trailer email leaked into the destination veto as a phantom out-of-worktree
# redirect. `_mask_quoted_redirect_chars` misses it: the `'"'"'` literal-
# apostrophe idiom embeds a `"` inside the outer `"$( … )"`, and bash parses
# `$(…)` RECURSIVELY (re-scoping quotes) while `_quote_spans` is flat — with an
# odd `'"'"'` count the scanner desyncs, reports the trailing region
# unterminated (→ mask returns the command unchanged, cpp#157 D5) AND leaves the
# trailer `>` between spans (phantom-unquoted). `shlex` fails on the same input
# ("No closing quotation"). Only an explicit substitution-context parser works.
#
# LETHALITY PATH ONLY — never consulted by `is_tier3_dangerous` / `TIER3_PATTERNS`
# / tier1 / `is_tier3_dangerous_for_lethality`; admission is byte-identical.
def _mask_lethality_redirect_chars(command: str) -> str:
    r"""Blank every ``<``/``>`` that is NOT a TOP-level outer-command redirect
    operator — one inside a single/double quote, a command substitution
    ``$(...)``, or a backtick region — leaving real outer redirect operators
    intact. Length-preserving (one space per masked char). Purely lexical.
    """
    out = list(command)
    stack: list[str] = []          # "DQ" | "SQ" | "SUB" | "BT"
    sub_paren: list[int] = []      # nested-paren depth, one entry per SUB frame
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        top = stack[-1] if stack else "TOP"
        if top == "SQ":            # single quotes: no escapes; only ' closes
            if ch == "'":
                stack.pop()
            elif ch in "<>":
                out[i] = " "
            i += 1
            continue
        if top == "BT":            # backtick: \X escapes; ` closes
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "`":
                stack.pop()
            elif ch in "<>":
                out[i] = " "
            i += 1
            continue
        # top in TOP / DQ / SUB : \X is an atomic escape pair
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            stack.append("SUB")
            sub_paren.append(0)
            i += 2
            continue
        if ch == "`":
            stack.append("BT")
            i += 1
            continue
        if ch == '"':
            if top == "DQ":
                stack.pop()
            else:
                stack.append("DQ")
            i += 1
            continue
        if ch == "'":
            if top in ("TOP", "SUB"):   # inside "..." a ' is a literal char
                stack.append("SQ")
            i += 1
            continue
        if top == "SUB":
            if ch == "(":
                sub_paren[-1] += 1
            elif ch == ")":
                if sub_paren[-1] > 0:
                    sub_paren[-1] -= 1
                else:
                    stack.pop()
                    sub_paren.pop()
            elif ch in "<>":
                out[i] = " "
            i += 1
            continue
        if top == "DQ":
            if ch in "<>":
                out[i] = " "
            i += 1
            continue
        # TOP: a real outer redirect operator — leave intact.
        i += 1
    if stack:
        # Genuinely unterminated (a quote/substitution never closed): fail-closed
        # toward lethal (cpp#157 D5) — do NOT exempt any `<`/`>`. The verbatim
        # `'"'"'`-in-`$(…)` killer parses BALANCED here (every context closes) and
        # is still masked; only a real dangling quote reaches this and is left raw.
        return command
    return "".join(out)


def _has_unterminated_quote(command: str) -> bool:
    """True when the trailing quoted region runs off the end unterminated per the
    shared ``_quote_spans`` scanner — the condition under which
    ``_mask_quoted_redirect_chars`` returns the command unchanged and a quoted
    ``<``/``>`` leaks into redirect extraction (cpp#236). Purely lexical.
    """
    spans = _quote_spans(command)
    return bool(spans) and not spans[-1][2]


def _needs_lethality_redirect_mask(command: str) -> bool:
    """Whether the substitution-aware mask is required (cpp#236): a command
    carrying a command/process substitution or an unbalanced quote — the only
    cases where a ``<``/``>`` cannot be attributed by the flat scanner. Every
    other command uses the raw text, so lethality is byte-identical to HEAD.
    """
    return "$(" in command or "`" in command or _has_unterminated_quote(command)


# ── cpp#241: a `<`/`>` inside a LITERAL-QUOTED heredoc BODY is not a redirect ──
#
# Pilots #2590 (7cd3ce9a) and #1990 (2bf1c7f3) died within three hours on the SAME
# shape: an interpreter reading a QUOTED-delimiter heredoc — `python3 - <<'PY' …
# PY`, `node - <<'JS' … JS` — whose body regex-edits Rust source (`-> Vec<T>`,
# `None::<…>`, `if a > b`). Because the delimiter is QUOTED, bash performs NO
# expansion: the body is fed VERBATIM to the interpreter on stdin, so a `>`/`<` in
# it is pure DATA. But the flat redirect/destination vetoes do not model the
# heredoc body, so a body `>` is read as a phantom out-of-worktree redirect →
# `_destination_veto_reason` returns non-None → the denial is made TERMINAL. This
# is the cpp#236 family (a `<`/`>` that is not a real outer redirect), but the
# cpp#236 mask models quotes and substitutions, NOT heredoc bodies. This pass
# blanks `<`/`>` inside the body of a heredoc whose delimiter is QUOTED/ESCAPED
# (`<<'D'`/`<<"D"`/`<<\D`). An UNQUOTED `<<D` body IS expanded, so it is left RAW
# (its `$(…)`/backticks execute) → `<<D $(curl …)` and `curl … | python3` stay
# terminal. LETHALITY PATH ONLY — admission byte-identical. Fail-closed: the
# opener line is never masked (a real redirect on it stays terminal), and an
# UNTERMINATED heredoc returns the command unchanged.
_HEREDOC_DELIM_STOP = frozenset(" \t\n;|&<>()")


def _parse_heredoc_opener(line: str, i: int) -> tuple[str, bool, bool, int] | None:
    r"""At ``line[i:i+2] == '<<'`` (already known OUTSIDE quotes), parse the heredoc
    opener. Return ``(delim, no_expansion, dash, end)`` where ``delim`` is the
    closing terminator (quotes/backslash removed), ``no_expansion`` is True iff the
    delimiter was QUOTED or ESCAPED, ``dash`` is the ``<<-`` form, ``end`` is just
    past the delimiter. ``None`` for a ``<<<`` here-string or an empty delimiter.
    """
    n = len(line)
    j = i + 2
    if j < n and line[j] == "<":
        return None  # `<<<` here-string, not a heredoc
    dash = False
    if j < n and line[j] == "-":
        dash = True
        j += 1
    while j < n and line[j] in (" ", "\t"):
        j += 1
    delim_chars: list[str] = []
    no_expansion = False
    while j < n:
        ch = line[j]
        if ch == "'":
            no_expansion = True
            j += 1
            while j < n and line[j] != "'":
                delim_chars.append(line[j])
                j += 1
            if j < n:
                j += 1  # consume closing '
            continue
        if ch == '"':
            no_expansion = True
            j += 1
            while j < n and line[j] != '"':
                if line[j] == "\\" and j + 1 < n:
                    delim_chars.append(line[j + 1])
                    j += 2
                    continue
                delim_chars.append(line[j])
                j += 1
            if j < n:
                j += 1  # consume closing "
            continue
        if ch == "\\" and j + 1 < n:
            no_expansion = True
            delim_chars.append(line[j + 1])
            j += 2
            continue
        if ch in _HEREDOC_DELIM_STOP:
            break
        delim_chars.append(ch)
        j += 1
    delim = "".join(delim_chars)
    if not delim:
        return None
    return delim, no_expansion, dash, j


def _scan_command_line_for_heredocs(line: str) -> list[tuple[str, bool, bool]]:
    """Quote-aware scan of ONE command line for `<<` openers OUTSIDE quotes. Return
    ``(delim, no_expansion, dash)`` per opener, left-to-right (bash reads their
    bodies in this order). A `<<` inside a quote and a `<<<` here-string are ignored.
    """
    result: list[tuple[str, bool, bool]] = []
    i, n = 0, len(line)
    in_sq = in_dq = False
    while i < n:
        ch = line[i]
        if in_sq:
            if ch == "'":
                in_sq = False
            i += 1
            continue
        if in_dq:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "'":
            in_sq = True
            i += 1
            continue
        if ch == '"':
            in_dq = True
            i += 1
            continue
        if ch == "<" and i + 1 < n and line[i + 1] == "<":
            parsed = _parse_heredoc_opener(line, i)
            if parsed is None:
                i += 2  # `<<<` or empty delim — step past `<<`
                continue
            delim, no_expansion, dash, end = parsed
            result.append((delim, no_expansion, dash))
            i = end
            continue
        i += 1
    return result


def _needs_lethality_heredoc_mask(command: str) -> bool:
    """Whether the heredoc-body mask could apply (cpp#241): a `<<` marker present.
    A command without one skips the pass and is byte-identical to HEAD.
    """
    return "<<" in command


def _mask_lethality_heredoc_redirect_chars(command: str) -> str:
    r"""Blank every ``<``/``>`` inside the BODY of a QUOTED/ESCAPED-delimiter heredoc
    (cpp#241). The opener line is never touched; an UNQUOTED body is left raw (it IS
    expanded); an UNTERMINATED heredoc returns the command UNCHANGED (fail-closed
    toward lethal). Length-preserving and purely lexical. LETHALITY PATH ONLY.
    """
    lines = command.split("\n")
    out_lines = list(lines)
    pending: list[tuple[str, bool, bool]] = []
    masked_any = False
    for idx, line in enumerate(lines):
        if not pending:
            pending.extend(_scan_command_line_for_heredocs(line))
            continue
        delim, no_expansion, dash = pending[0]
        candidate = line.lstrip("\t") if dash else line
        if candidate == delim:
            pending.pop(0)  # terminator line — consumed, never masked
            continue
        if no_expansion and ("<" in line or ">" in line):
            out_lines[idx] = line.replace("<", " ").replace(">", " ")
            masked_any = True
    if pending:
        # Unterminated heredoc: body span not provable — do not exempt (cpp#157 D5).
        return command
    if not masked_any:
        return command
    return "\n".join(out_lines)


# ── mika#2573 (case A): a print-only `sed` segment is not proven danger ──────
#
# A `sed -n '<addr>[,<addr>]p'` invocation (the closed-world print-only shape
# `_is_safe_sed_print_only` already recognizes — `-n` mandatory and sole flag,
# script is exactly one address/range + bare `p`, NO `-i` in-place, NO `w`/`W`
# write command) reads its input and prints matching lines. It writes nothing
# and executes nothing. Yet the LITERAL TEXT of its print SCRIPT is scanned by
# the lethality verb patterns like any other segment, so a pilot filtering a
# log for a dangerous string — `… | sed -n '/rm -rf/p'`, `… | sed -n
# '/DROP TABLE/p'` — trips the very verb it is searching FOR and has its denial
# made TERMINAL, although the verb is DATA (a search pattern), never a command.
# This is the same syntactic over-refusal class cpp#205 ratified as SURVIVABLE:
# a read filter is not proven danger.
#
# This blanks each print-only `sed` SEGMENT before the verb scan, mirroring the
# segment-drop-then-recheck mechanism of `rm_confined_to_pilot_scratch`
# (cpp#213): the surviving segments are re-joined with a newline (a separator
# bash honors, so no two tokens glue into a phantom verb) and carry their full
# text unchanged, so any genuine danger OUTSIDE the sed segment still matches.
# LETHALITY ONLY — `is_tier3_dangerous` (the REFUSAL) never calls this, so the
# command stays DENIED; only whether the denial ends the run changes. Reuses
# `_is_safe_sed_print_only` verbatim, so the print-only shape recognized here
# cannot drift from the one already whitelisted for the chain-safety path.
def _blank_print_only_sed_segments(command: str) -> str:
    survivors: list[str] = []
    carved = False
    for seg in _split_compound_command(command):
        if _is_safe_sed_print_only(seg):
            carved = True
            continue
        survivors.append(seg)
    if not carved:
        return command
    return "\n".join(survivors)


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
    At the time that veto could not carry it: `_segment_write_kind` classified
    only `cp`/`mv`, `mkdir` and `git show >`, so a bare redirect never reached
    it — `echo hi > /etc/passwd` measured `_destination_veto_reason = None`
    (cpp#154 plan measurement M3). cpp#155 closed that specific gap
    (`_segment_write_kind` now classifies redirects too, as write-kind
    `bash-redirect`), but this function's OWN narrowing — purely lexical and
    cwd-free — is unaffected and stays exactly as it was: it answers a
    different question (LETHALITY of an already-refused, never-executed
    command) from `_destination_veto_reason`'s (containment of a write that
    IS about to execute, on the allow path), and the two must keep answering
    it independently. The widening therefore still lands here too, applied
    AFTER the /dev/null strip so cpp#130's trailing-boundary edge cases
    (`/dev/null.txt`, `/dev/nullified`, `/dev/null/../etc/passwd`) keep their
    own behaviour.

    cpp#157 adds a third narrowing, and it runs INNERMOST: a `<` or `>` sitting
    inside a quoted region is ordinary text to bash, not a redirect operator, so
    it is masked before any pattern runs. `sed 's/=.*/=<set>/'` is no longer on
    its own session-fatal — while staying REFUSED, unchanged, since
    `is_tier3_dangerous` is not touched.

    cpp#203 adds a fourth, independent narrowing: `sed -i <script>? /dev/null`
    (`_SED_I_DEVNULL_RE`, above) — `sed -i`'s own in-place-edit FILE ARGUMENT,
    not a shell redirect target, so it shares no character (`<`/`>`) with the
    other three narrowings and its ORDER relative to them does not matter; it
    is applied here between the quote mask and the /dev/null-redirect strip
    purely for readability, next to the constant it pairs with.

    ORDER IS LOAD-BEARING for the other three: quoted `<`/`>` masked FIRST
    (cpp#157), /dev/null second (cpp#130), contained targets third (cpp#154).
    The mask must come first because the two later strips extract redirect
    TARGETS, and a quoted `>` fabricates a phantom one: on `main`,
    `_redirect_targets("echo 'a>b'")` yields `["b'"]`, which
    `_is_contained_redirect_target`'s charset rejects only by the accident of the
    trailing quote. Masking first removes the phantom target outright instead of
    relying on that accident. The relative order of cpp#130 and cpp#154 is
    unchanged, so their edge cases (`/dev/nullified`, `/dev/null.txt`,
    `/dev/null/../etc/passwd`) keep their own behaviour. All four live in this
    one function, and `_denial_is_terminal` is the single consumer — cpp#151 B0
    collapsed three separate lethality computations into one precisely so two
    notions of "fatal" could not drift apart; cpp#203 keeps that invariant.

    cpp#205 (case a): the FINAL pattern-match step no longer runs
    `is_tier3_dangerous` (the full `TIER3_PATTERNS`, refusal-facing) on the
    stripped text. It runs `_matches_proven_dangerous_lethality_verb`
    instead — `TIER3_PATTERNS` MINUS its own trailing generic-redirect
    catch-all (deferred to the cwd-aware destination-veto calls in
    `_denial_is_terminal`, which can actually prove a redirect target safe or
    unsafe — see `_TIER3_VERB_PATTERNS_FOR_LETHALITY`'s block comment) PLUS the
    new verbs enumerated in `_PROVEN_DANGEROUS_VERB_PATTERNS_CPP205`
    (`chmod -R`/`chown -R`/`dd`/`mkfs`/`truncate`/fork-bomb — measured
    ALREADY non-terminal pre-cpp#205, an under-terminal gap this closes). The
    four strips above are unchanged and still run unconditionally: they are
    inert for the new verb patterns (none of them contains `<`/`>`, `/dev/
    null`, or a redirect target) and remain load-bearing for `sed -i`
    (cpp#203) and for the verb-only patterns that DO carry `<`/`>` characters
    (`<(`, `>(`, still quote-masked per cpp#157). This function's name and
    signature (`(command: str) -> bool`, no `cwd`) are unchanged; only what it
    proves at the end is retargeted from "the full tier3 denylist minus two
    narrow target carve-outs" to "the proven-danger verb set minus the one
    entry that names a provable target."
    """
    return _matches_proven_dangerous_lethality_verb(
        _strip_contained_redirects(
            _STDOUT_DEVNULL_RE.sub(
                " ",
                _SED_I_DEVNULL_RE.sub(
                    " ",
                    _mask_quoted_redirect_chars(
                        _blank_print_only_sed_segments(command)
                    ),
                ),
            )
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


def _quote_spans(command: str) -> list[tuple[int, int, bool]]:
    r"""Return every single- or double-quoted region in *command*, as
    ``(start, end, closed)`` triples in left-to-right, non-overlapping order.

    ``start`` is the index of the opening quote character. ``end`` is one
    past the region's last byte: the index after the closing quote character
    when ``closed`` is True, or ``len(command)`` when the region runs off the
    end unterminated (``closed`` False). Both delimiters (when present) are
    included in ``[start, end)`` — callers that only need "is index *i* inside
    a quoted run" treat the whole span as opaque, delimiters included, which
    is what all three call sites below already did before this extraction.

    cpp#158: this is the single POSIX-correct quoted-region scanner shared by
    `_split_compound_command` (authorization), `contains_unquoted_metacharacter`
    (authorization) and `_mask_quoted_redirect_chars` (lethality) — previously
    three independent state machines, two of which measurably disagreed on
    `main` (cpp#157 D6 / `TestQuoteScannerBoundaryParity`). The atomic escape
    rule below is `_mask_quoted_redirect_chars`'s own (cpp#157) — this
    extraction changes that function's OWN quote-boundary math not at all; it
    is the two older scanners that gain correctness here.

    Escape semantics, atomic throughout — the single rule this module now
    applies everywhere a quote might open or close:

    - OUTSIDE any quote, ``\X`` is an escape pair consumed as one unit for ANY
      ``X`` — so ``\'`` and ``\"`` are literal, escaped characters and do NOT
      open a region. Neither `_split_compound_command` nor
      `contains_unquoted_metacharacter` had this rule before cpp#158: both
      treated a bare backslash outside quotes as a no-op, so the following
      quote character opened a PHANTOM region. On `main`,
      `contains_unquoted_metacharacter("echo \\'$(echo INJECTED)")` is
      `False` — the phantom region swallows the real, live `$(...)` into what
      the old scanner believes is inert single-quoted text, and
      `is_safe_bash_command` on that same string returns `True`. Real bash
      does expand it (verified: `echo \'$(echo INJECTED)` prints
      ``'INJECTED``, not a literal `$(echo INJECTED)`). This is a genuine
      authorization-path false negative on `main`, independent of and
      pre-dating cpp#157; the plan doc's security section carries the full
      write-up and the closing proof.
    - INSIDE ``"..."``, ``\X`` is likewise an escape pair consumed as a unit
      for any ``X`` — so ``\"`` does not close the region and a doubled
      ``\\`` immediately before a closing ``"`` does not swallow it. This is
      the one boundary `_split_compound_command` and
      `contains_unquoted_metacharacter` already disagreed on before cpp#158
      (`echo "a\\"` — CORPUS row `double-quote-double-backslash`,
      `TestQuoteScannerBoundaryParity`): `_split_compound_command` only ever
      special-cased ``\"`` (checking that the escaped character IS a quote),
      so on ``\\"`` its first backslash passed through as an ordinary
      character and its second paired with the closing quote and consumed it,
      leaving the region open. `contains_unquoted_metacharacter` and
      `_mask_quoted_redirect_chars` already treated the pair atomically
      (any ``X``) and closed. This scanner adopts the atomic form throughout,
      i.e. the POSIX-correct, already-shipped `_mask_quoted_redirect_chars`
      reading.
    - INSIDE ``'...'``, backslash is a plain, literal character — only a
      matching ``'`` closes the region. Unanimous across all three scanners
      before this extraction; unchanged.

    An unterminated region is reported with ``closed=False`` rather than
    silently choosing a fail-closed direction: `_split_compound_command` and
    `contains_unquoted_metacharacter` want the remainder treated as INSIDE
    the quote (their fail-closed direction is "refuse", and a span already
    reaching to ``len(command)`` gives them exactly that, span-membership
    alone, no extra branching needed); `_mask_quoted_redirect_chars` wants the
    opposite — return the command unchanged (its fail-closed direction is "do
    not exempt", cpp#157 D5) — and does so by checking ``closed`` on the
    trailing span itself. Interpreting ``closed`` is deliberately a
    per-caller decision, per cpp#158's explicit ask: the caller's own verdict
    on an unterminated quote is a policy choice about what that caller is
    deciding (an allowance vs. a lethality exemption), not a property of the
    lexical scan.
    """
    spans: list[tuple[int, int, bool]] = []
    n = len(command)
    i = 0
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch in ("'", '"'):
            quote_char = ch
            start = i
            i += 1
            closed = False
            while i < n:
                c2 = command[i]
                if quote_char == '"' and c2 == "\\" and i + 1 < n:
                    i += 2
                    continue
                if c2 == quote_char:
                    i += 1
                    closed = True
                    break
                i += 1
            spans.append((start, i, closed))
            continue
        i += 1
    return spans


def _split_compound_command(command: str) -> list[str]:
    """Quote-aware split on shell operators AND raw newlines.

    Splits on ``&&``, ``||``, ``;``, ``|``, and ``\\n`` only when they appear
    OUTSIDE of single- or double-quoted regions, as computed by the shared
    `_quote_spans` (cpp#158). Quote boundaries themselves — where a region
    opens and closes — are entirely `_quote_spans`'s concern; see its
    docstring for the atomic escape rule (inside AND outside quotes) that
    replaces this function's former ad hoc, ``\\"``-only escape handling.

    Unterminated quotes: a `_quote_spans` region that never closes already
    extends to the end of the command, so every character in it — including
    what would otherwise be a separator — is skipped exactly as before
    (conservative — falls through to the LLM relay on malformed input). No
    separate handling is needed here for that case.

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
    span_end_at: dict[int, int] = {
        start: end for start, end, _closed in _quote_spans(command)
    }
    i = 0
    seg_start = 0

    while i < n:
        if i in span_end_at:
            i = span_end_at[i]
            continue

        ch = command[i]

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
    regions as inert. Quote boundaries come from the shared `_quote_spans`
    (cpp#158); this function's own job is only to decide, for each region
    `_quote_spans` reports, which markers still count as "unquoted" bash would
    expand:

    - Outside any quoted region (the gaps `_quote_spans` leaves unclaimed), a
      bare backtick, ``$(`` or ``$'`` returns True.
    - Inside a ``"..."`` region, a bare backtick or ``$(`` returns True (cpp#41
      closed the double-quoted gap — bash expands both there). ``$'`` is NOT
      flagged inside double quotes: ANSI-C ``$'...'`` quoting is only recognized
      outside quotes, so inside a double-quoted region ``$'`` is literal. The
      interior scan applies the same atomic ``\\X`` escape rule `_quote_spans`
      used to find the region's end, so a backslash-suppressed ``\\$(``/``\\```` is
      NOT flagged.
    - Inside a ``'...'`` region, nothing is scanned — bash treats everything
      there as literal, full stop.
    - Unterminated quotes: `_quote_spans` already extends an unclosed region to
      `len(command)`, so the remainder is scanned (double-quoted) or skipped
      entirely (single-quoted) exactly as a closed region of the same kind
      would be — conservative, falls through to the LLM relay on malformed
      input.

    cpp#158 fixes a real authorization-path false negative this function
    carried before the shared scanner: with no escape handling OUTSIDE quotes,
    a backslash-escaped quote character (``\\'``, ``\\"``) — a literal,
    non-quoting apostrophe or double-quote to bash — opened a PHANTOM region
    here. On `main`,
    ``contains_unquoted_metacharacter("echo \\'$(echo INJECTED)")`` is
    `False`: the phantom single-quoted region the stray ``'`` after the
    backslash opens swallows the live, unquoted ``$(echo INJECTED)`` as
    (wrongly) inert. Verified against real bash:
    ``echo \\'$(echo INJECTED)`` prints ``'INJECTED`` — the substitution runs
    for real. `_quote_spans`'s outside-quote atomic ``\\X`` rule closes this:
    the escaped ``'`` no longer opens anything, so the ``$(`` is correctly
    seen at top level. See the plan doc's security section for the full
    authorization-path analysis (this fix only ever ADDS detections here, it
    never removes one — see the doc for why no case flips the other way).

    NOTE: the Rust mirror ``contains_unquoted_metacharacter`` in
    ``crates/mika-agent/src/server/permission_pre_classifier.rs`` (mika repo) does
    NOT yet detect double-quoted substitution. This Python side intentionally
    diverges (hardened) until the paired-audit ticket mirrors the cpp#41 fix.

    See mika#944 (ANSI-C quoting bypass), mika#946 (mika#938 F5 sentinel),
    cpp#41 (double-quoted substitution gap).
    """
    n = len(command)
    span_end_at: dict[int, tuple[int, str]] = {
        start: (end, command[start]) for start, end, _closed in _quote_spans(command)
    }
    i = 0

    while i < n:
        if i in span_end_at:
            end, quote_char = span_end_at[i]
            if quote_char == '"':
                # cpp#41: bash performs command substitution inside DOUBLE
                # quotes — only SINGLE quotes suppress it. Scan the interior
                # for the two markers bash still expands there: `$(` and
                # backtick. `$'` is deliberately NOT flagged inside double
                # quotes — ANSI-C `$'...'` quoting is only recognized
                # OUTSIDE quotes; inside a double-quoted region `$'` is a
                # literal dollar + apostrophe (mika#944's `$'` guard
                # correctly lives in the top-level branch only).
                j = i + 1
                while j < end:
                    c2 = command[j]
                    if c2 == "\\" and j + 1 < end:
                        # Same atomic escape pair `_quote_spans` used to find
                        # `end`: `\` inside double quotes suppresses `$`/
                        # backtick, so `"\$(x)"` / `"\`x\`"` are literal.
                        j += 2
                        continue
                    if c2 == "`":
                        return True
                    if c2 == "$" and j + 1 < end and command[j + 1] == "(":
                        return True
                    j += 1
            # quote_char == "'": single-quoted region, fully inert — nothing
            # to scan.
            i = end
            continue

        # Top level — outside any quoted region.
        ch = command[i]
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


def is_safe_bash_command(command: str, cwd: str | None = None) -> bool:
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

    return all(_is_safe_sub_command(sub, cwd) for sub in sub_commands)


def _is_safe_sub_command(sub: str, cwd: str | None = None) -> bool:
    return (
        is_safe_git_command(sub)
        or is_safe_build_command(sub)
        or is_safe_make_command(sub)
        or is_safe_shell_command(sub)
        or is_safe_gh_command(sub)
        or is_safe_mika_dispatch(sub)
        or is_safe_exec_when_contained(sub)
        # cpp#189/#190: `sed -n '<addr>[,<addr>]p' [FILE...]` print-only —
        # NOT via `is_safe_shell_command`/`SAFE_SHELL_COMMANDS` (sed stays
        # excluded there, cpp#27); this is its own narrow, self-contained
        # closed-world predicate. See `_is_safe_sed_print_only` above.
        or _is_safe_sed_print_only(sub)
        # cpp#207 (RATIFIED admission widening, Prime + Vincent 2026-09-26):
        # `bash <p>` / `sh <p>` / `./<p>` where `<p>` is a git-TRACKED,
        # worktree-relative script. `cwd` is None for every call site that
        # predates this ticket (`permissions._bash_allow_is_chain_safe` at
        # `permissions.py:659`, and every direct unit-test call) — None
        # fails CLOSED here unconditionally, so those callers' behavior is
        # byte-identical to before this ticket. Only `is_tier1_auto_approve`
        # (the Tier-1 admission fast path, which already threads `cwd`
        # through today) can ever pass a real cwd and reach this class.
        or is_safe_tracked_repo_script_invocation(sub, cwd)
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


# ── Tracked-repo-script invocation (admission widening, cpp#207) ────────────
#
# RATIFIED (Prime + Vincent, 2026-09-26, cpp#207 comment "RATIFIÉ — spec
# implémentable"): the FIRST admission-widening of the series — every prior
# fix in this ticket's ancestry (#154 D3, #176, #196, #201, #203, #205) was
# LETHALITY-only (narrowing what ends a session on refusal); none of them
# widened what tier1 auto-approves. This one does, to exactly one named,
# bounded class.
#
# Founding case (replayed here, mika#2054): a pilot dispatch needs to run a
# repo-committed helper script (`scripts/verify-egress-no-log.sh`) as part of
# its own verification pipeline. Pre-cpp#207, `bash <script>` had no tier1
# predicate at all (see `is_safe_exec_when_contained`'s own "Not covered here"
# note above — `bash <script>` was explicitly left to "its own compound
# checker path", which never materialized) — every such invocation, however
# innocuous, escalated to the relay.
#
# The rule, ALL FOUR conditions required (spec's Q1=(a) tracked-git,
# Q2=args inherit path restrictions):
#
#   1. `<p>` is LEXICALLY relative: no leading `/`, no `~`, no leading/bare
#      `$`, no `..`-escape anywhere. Reuses the SAME lexical disqualifier
#      set `_is_lexically_disqualified_redirect_target` already enforces
#      for redirect targets (cpp#154/#176), plus an explicit leading-`/`
#      reject: unlike a redirect target (where an absolute `/tmp/...` is a
#      carved-out exception, cpp#154), a script-invocation path admits NO
#      absolute form at all — the ratified spec names "relative" outright.
#   2. `<p>` RESOLVES under the worktree: `is_within_project(<p>, cwd)` —
#      the SAME cwd/fs-aware mechanism the Write/Edit tier1 path already
#      uses (cpp#38). Not a new mechanism; reused as specified.
#   3. `<p>` is TRACKED by git in the `cwd` worktree: exactly one
#      `git -C <cwd> ls-files --error-unmatch -- <p>` query
#      (`_is_tracked_repo_script`). A pilot-written, uncommitted file is NOT
#      tracked → refused. THIS is the anti-bypass proof the spec calls out
#      by name: an attacker (or a confused pilot) who writes their own
#      script to disk cannot ride this rule by also naming it in a `bash`
#      invocation — only a script some human already committed and pushed
#      through review qualifies. FAIL-CLOSED on any git error (missing
#      binary, `cwd` not inside a repo, non-zero exit) — never auto-approved
#      by default when the query cannot prove tracked status.
#   4. Every ARGUMENT inherits the same path restriction (Q2):
#      `_is_out_of_worktree_arg_path` refuses any operand that is `~`-
#      rooted, contains a `..` segment, or is an absolute path that does not
#      itself resolve inside the worktree. We cannot inspect what the
#      script DOES with an argument, so an argument that is merely a flag or
#      an opaque value is never flagged (it might not be a path at all) —
#      only a SPELLING that is provably an escaping path is refused.
#
# This is a NEW mechanism in ONE respect only: it is the first tier1
# predicate that shells out to `git` (a subprocess call) rather than staying
# a pure, filesystem-free text/regex classifier. It is deliberately the
# single narrowest possible form of that — one `git ls-files
# --error-unmatch` invocation, list-form argv (no shell interpolation), a
# bounded timeout so a wedged git process cannot stall admission
# indefinitely, and a `--` separator so a script path that happens to look
# like a git flag is never mis-parsed. `is_within_project` was already
# cwd/fs-aware (cpp#38); this is the same posture extended to git's own
# index.
#
# Double control (AC5, doctrine graved cpp#205/#207 — "admission never
# reopens without (1) survivability closed [cpp#205] AND (2) double control
# orthogonal"): this classifier (tracked-git) and the pilot's bwrap sandbox
# (`--unshare-net` / tmpfs, dispatch-lib.sh Phase 2b) are TWO INDEPENDENT
# boundaries. A tracked script has already been reviewed at the repository
# (this classifier's own contribution); even in the residual case where a
# tracked script turns out to misbehave, its blast radius is still the
# sandbox's — net cut, fs writes confined to tmpfs/worktree. Neither
# boundary is "the" safety property on its own; this rule only ever narrows
# admission to commands where BOTH hold.
#
# Explicitly NOT covered (falls through to every other predicate, which all
# deny it, so the whole command is refused, same as before this ticket):
#   * `bash -c '<inline>'` / `sh -c '<inline>'` / `eval …` — already TIER3,
#     matched on the WHOLE command in `is_tier3_dangerous` BEFORE
#     `_is_safe_sub_command` is ever reached (`is_safe_bash_command`'s
#     ordering). This predicate is never even consulted for those shapes.
#   * `curl … | sh` / `wget … | bash` — the interpreter token is not the
#     FIRST word of its own compound segment (`_split_compound_command`
#     splits on `|`), so `_tracked_script_invocation_path("bash")` (no path
#     operand) returns `None` here, and `curl …` fails every other
#     predicate independently. Remote code never reaches this rule at all.
#   * a script OUT of the worktree (absolute, `/tmp/x.sh`, a `..`-escape) —
#     condition 1 or 2 refuses it.
#   * flags before the path (`bash -x scripts/x.sh`) — the flag token is
#     what gets checked as "the path" (this predicate does not special-case
#     flags), and `-x` is never `is_within_project`/git-tracked → refused,
#     fail-closed by construction, not by an explicit flag denylist.

_TRACKED_SCRIPT_INTERPRETERS: frozenset[str] = frozenset({"bash", "sh"})


def _tracked_script_invocation_path(sub: str) -> tuple[str, list[str]] | None:
    """Split a sub-command into ``(script_path, args)`` iff it is shaped like
    ``bash <p> [args...]`` / ``sh <p> [args...]`` / ``./<p> [args...]``, else
    ``None``.

    Pure syntax — no filesystem or git access here; that happens in the
    caller once this shape is confirmed. Fails closed (returns ``None``) on
    an unparseable (unbalanced-quote) sub, exactly like
    ``permissions._shlex_operands``.
    """
    try:
        tokens = shlex.split(sub)
    except ValueError:
        return None
    if not tokens:
        return None

    head = tokens[0]
    if head in _TRACKED_SCRIPT_INTERPRETERS:
        if len(tokens) < 2:
            return None
        return tokens[1], tokens[2:]
    if head.startswith("./"):
        return head, tokens[1:]
    return None


def _is_lexically_disqualified_script_path(p: str) -> bool:
    """Condition 1: whether ``p`` fails the "relative, no `..`-escape" test.

    Reuses `_is_lexically_disqualified_redirect_target` (the SAME `~`/`$`/
    `..`/charset disqualifiers already enforced for redirect targets), plus
    an explicit leading-`/` reject: a redirect target may carve out an
    absolute `/tmp/...` exception (cpp#154); a script-invocation path never
    does — the ratified spec says "relative", full stop.
    """
    if p.startswith("/"):
        return True
    return _is_lexically_disqualified_redirect_target(p)


def _is_tracked_repo_script(script_path: str, cwd: str) -> bool:
    """Condition 3: whether ``script_path`` is TRACKED by git in the
    worktree rooted at ``cwd``.

    FAIL-CLOSED on any of: git binary missing, ``cwd`` not inside a git
    repository, the query timing out, or any other non-zero exit —
    including the actual "not tracked" case. This is the ONE subprocess call
    in the tier1 admission path; a single `git ls-files --error-unmatch`
    invocation, list-form argv (never a shell string — no interpolation
    risk), `--` before the path (so a path that lexically matches the
    redirect-target charset but starts with `-` is never mis-parsed as a git
    flag), and a short timeout so a wedged git process cannot stall
    admission indefinitely.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "ls-files", "--error-unmatch", "--", script_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _is_out_of_worktree_arg_path(arg: str, cwd: str) -> bool:
    """Condition 4 (Q2): whether ``arg`` is a path that escapes the worktree.

    We cannot inspect what the script does with its arguments, so this is
    deliberately narrow: a `..`-bearing or `~`-rooted operand is refused
    outright (lexical, never resolved — same discipline as
    `_is_lexically_disqualified_redirect_target`); an ABSOLUTE operand is
    checked against the worktree via `is_within_project` (an absolute
    spelling CAN legitimately resolve inside the worktree); a plain relative
    operand with no `..` cannot escape by construction and is never flagged
    — it might not even be a path (a bare flag, an opaque value), and this
    predicate only refuses a spelling that IS provably an escaping path.
    """
    if not arg:
        return False
    if ".." in arg:
        return True
    if arg.startswith("~"):
        return True
    if arg.startswith("/"):
        return not is_within_project(arg, cwd)
    return False


def is_safe_tracked_repo_script_invocation(sub: str, cwd: str | None) -> bool:
    """ADMISSION rule (cpp#207): allow ``bash <p>`` / ``sh <p>`` / ``./<p>``
    (with args) as a tier1 auto-approve IFF ALL of:

      1. ``<p>`` is lexically relative (no leading ``/``, ``~``, ``$``, or
         ``..``-escape) — `_is_lexically_disqualified_script_path`.
      2. ``<p>`` resolves UNDER the worktree — `is_within_project`.
      3. ``<p>`` is git-TRACKED in the worktree — `_is_tracked_repo_script`.
         The anti-bypass proof: an untracked, pilot-written script never
         qualifies no matter how it got onto disk.
      4. Every argument passes `_is_out_of_worktree_arg_path`'s inherited
         path restriction.

    ``cwd is None`` means the caller predates cpp#207 (every call site
    except `is_tier1_auto_approve`, which already threads a real `cwd`
    through today) — fails CLOSED unconditionally, so those callers'
    behavior never changes. See the module-level comment block above this
    function for the full rationale, the double-control note (AC5), and
    what stays explicitly refused.
    """
    if cwd is None:
        return False
    parsed = _tracked_script_invocation_path(sub)
    if parsed is None:
        return False
    script_path, args = parsed
    if _is_lexically_disqualified_script_path(script_path):
        return False
    if not is_within_project(script_path, cwd):
        return False
    if not _is_tracked_repo_script(script_path, cwd):
        return False
    if any(_is_out_of_worktree_arg_path(a, cwd) for a in args):
        return False
    return True


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


# ── cpp#190/#189: `sed -n <range>p` print-only, general (non-contained) ──────
#
# `_is_safe_sed_pure_substitution` above (the `s///` form) is wired ONLY into
# `is_git_readonly_compound_when_contained` — a containment-attested (
# `MIKA_PILOT_CONTAINED=1`) predicate, not reachable by the ordinary dev-groom
# pilot. cpp#189/#190 are a DIFFERENT sed shape (`-n '<addr-range>p'`, print
# a line range, no substitution at all) failing in the GENERAL (uncontained)
# chain path: `sed`/`awk` are deliberately absent from `SAFE_SHELL_COMMANDS`
# entirely (cpp#27 — both are general-purpose interpreters whose sub-features
# can't be exhaustively guarded), so every sed shape used to route straight to
# relay, and a `&&`/`|` chain containing one (`echo … && sed -n … && grep …`,
# cpp#190; `sed -n … | cat -n`, cpp#189) failed per-segment chain-safety on
# that ONE segment — logged under whatever unrelated rule_id `policy.evaluate`
# happened to first-match on the whole string (`[bash-grep]`, `\sgrep\s`
# matching a LATER segment; see `_is_sanctioned_readonly_for_loop`'s docstring
# above for the same misattribution class).
#
# This predicate does NOT reopen cpp#27: it is not added to
# `SAFE_SHELL_COMMANDS` (so `is_safe_shell_command("sed …")` — and every
# cpp#27 regression test — is untouched), and it is a NARROW, closed-world,
# self-contained shape, exactly the `_is_safe_sed_pure_substitution` pattern:
#   * `-n` is REQUIRED and is the ONLY flag before the script (no `-i`
#     in-place, no `-e`/`-f` multi-script/script-file).
#   * The single-quoted script must be EXACTLY one address or address RANGE
#     (`NUMBER`, `$` last-line, or `/regex/`) followed by the bare `p`
#     (print) command and nothing else — the closing `'` is anchored
#     immediately after `p`, so no other sed command letter (`w`/`W` write,
#     `e`/`r`/`R` exec/read, `d`/`s`/`y`/`q`/`n`/`a`/`i`/`c`/`!`) or trailing
#     flag can ride inside the script.
#   * `sed 'ADDRp' file` WITHOUT `-n` is deliberately NOT admitted — it
#     prints the addressed line TWICE (the explicit `p` plus sed's default
#     auto-print), a different shape than the evidence, and read-only-but-
#     unenumerated routes to relay (over-block is the safe direction, cpp#34
#     discipline: widen only on evidence).
#   * File operands share `_SAFE_CAT_RE`'s charset (no shell metacharacters,
#     no redirect char) — consistent with every other bounded pipe tool here.
_SED_ADDR = r"(?:\d+|\$|/(?:[^/\\]|\\.)*/)"
_SAFE_SED_PRINT_RE = re.compile(
    rf"^\s*sed\s+-n\s+'{_SED_ADDR}(?:,{_SED_ADDR})?p'\s*(?:\s+[A-Za-z0-9_./-]+)*\s*$"
)


def _is_safe_sed_print_only(sub: str) -> bool:
    """True iff sub is `sed -n '<addr>[,<addr>]p' [FILE...]` (print-only).

    Closed-world: `-n` mandatory and sole flag, script is exactly one
    address/range + bare `p`, no other sed command letter can appear (the
    closing quote is anchored right after `p`). See the module comment
    above (cpp#189/#190) for the full rationale and what stays denied.
    """
    return bool(_SAFE_SED_PRINT_RE.match(sub))


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


# ── cpp#213: designated `.pilot-scratch/` carve-out for rm/rmdir LETHALITY ───
#
# Sibling of the cpp#201/#209 mktemp lethality carve, applied to the `rm`/
# `rmdir` VERB under one designated, dispatch-managed prefix. cpp#205 case (a)
# ratified that destructive verbs stay TERMINAL "regardless of target" — which
# is right for `rm -rf` pointed at an arbitrary path, but killed pilots that
# `rm -rf`'d their OWN scratch directory inside the worktree (mika#2054 pilot
# 83db3a82 `rm -rf verif-2054-redcheck`; mika#2544 pilot cbdd3f5b
# `rm -rf .t2544 && git status`). Dispatch (mika#2548 / PR mika#2550) now
# pre-creates a git-excluded `.pilot-scratch/` (emptied each run) and forbids
# `rm` on it in the prompt; this is the classifier half. It flips the LETHALITY
# of exactly that shape from terminal to survivable — the command is STILL
# refused (admission is byte-identical: `is_tier3_dangerous`,
# `is_tier1_auto_approve`, and every YAML rule are untouched, and none of these
# functions is consulted by any of them), it just no longer ends the run, so
# the pilot routes around the refusal instead of dying on it.
#
# The prefix is resolved fs-aware against the SESSION worktree (`cwd`), the SAME
# way `is_within_project` resolves containment — NOT a substring match. Every
# fail-closed direction below keeps the command TERMINAL, matching cpp#213's
# hard boundary that this only ever flips terminal→survivable, never
# refused→allowed.


def is_within_pilot_scratch(target: str, cwd: str) -> bool:
    """Whether *target* resolves strictly under ``<cwd>/.pilot-scratch/`` (or is
    that directory itself), resolved symlink-aware exactly like
    ``is_within_project`` (cpp#213).

    Fail-CLOSED (returns ``False``, i.e. the caller keeps the command terminal)
    for every operand cpp#213 must reject:

    - an ABSOLUTE path — rejected outright before any resolve, even one that
      happens to name the real scratch dir (the designated idiom is always a
      worktree-relative operand);
    - a ``..`` escape — ``Path.resolve`` collapses it, so ``.pilot-scratch/../src``
      lands at ``<cwd>/src`` and fails ``relative_to`` the scratch root;
    - an OUTBOUND symlink component — the scratch root is the LITERAL
      ``<resolved_cwd>/.pilot-scratch`` (never itself resolved through a
      symlink), while the target IS resolved (``strict=False`` follows symlinks
      on existing components); a ``.pilot-scratch`` (or any component) that is a
      symlink pointing outside therefore resolves away from the literal root and
      fails ``relative_to``;
    - anything outside the prefix (`src`, `/tmp/x`, `~/…`, `$HOME/…`);
    - a ``cwd`` that cannot be resolved (git unavailable, cwd outside the repo) —
      ``Path(cwd).resolve(strict=True)`` raises and we fail closed, the SAME
      ``OSError`` guard ``is_within_project`` uses.

    The directory itself IS accepted (``relative_to`` treats a path as under
    itself), matching cpp#213's positive `rm -r .pilot-scratch` case.
    """
    if not target:
        return False
    if Path(target).is_absolute():
        return False
    try:
        resolved_cwd = Path(cwd).resolve(strict=True)
    except OSError:
        return False
    # LITERAL root — deliberately NOT `.resolve()`d, so a symlinked
    # `.pilot-scratch` cannot fold an outbound target back under the prefix.
    scratch_root = resolved_cwd / ".pilot-scratch"
    resolved = (resolved_cwd / target).resolve(strict=False)
    try:
        resolved.relative_to(scratch_root)
        return True
    except ValueError:
        return False


_RM_VERBS: frozenset[str] = frozenset({"rm", "rmdir"})


def _rm_segment_operands(segment: str) -> list[str] | None:
    """The positional (non-flag) operands of a bare ``rm``/``rmdir`` *segment*,
    or ``None`` when *segment* is not such a command or cannot be tokenized
    (cpp#213).

    ``None`` (fail-closed — the caller keeps the segment, so it stays terminal)
    when: the segment does not tokenize (unbalanced quotes → ``shlex`` raises),
    it is empty, or its leading word is not exactly ``rm``/``rmdir`` (a
    path-qualified ``/bin/rm`` is deliberately not carved — it stays terminal).

    Flags (``-rf``, ``-r``, ``--recursive``, …) are dropped; ``--`` ends option
    parsing; a lone ``-`` is treated as a flag-like token and dropped. A shell
    redirect operator that ``shlex`` surfaces as a bare ``>``/``<`` token becomes
    an ordinary operand here — it is never under the scratch prefix, so the
    segment is not carved and stays terminal (and `_denial_is_terminal`'s own
    redirect/destination vetoes run on the full command regardless)."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return None
    if not tokens or tokens[0] not in _RM_VERBS:
        return None
    operands: list[str] = []
    end_of_opts = False
    for tok in tokens[1:]:
        if not end_of_opts and tok == "--":
            end_of_opts = True
            continue
        if not end_of_opts and tok.startswith("-"):
            continue
        operands.append(tok)
    return operands


def rm_confined_to_pilot_scratch(command: str, cwd: str) -> bool:
    """Whether *command*'s tier3-for-lethality danger is due SOLELY to
    ``rm``/``rmdir`` segments whose EVERY operand resolves strictly under
    ``<cwd>/.pilot-scratch/`` (cpp#213).

    Consulted ONLY by ``permissions._denial_is_terminal`` — the LETHALITY
    decision. Never touches admission (`is_tier3_dangerous`,
    `is_tier1_auto_approve`, YAML rules); the command stays refused either way.

    Mechanism mirrors cpp#201's `_strip_contained_redirects`: each ``rm``/
    ``rmdir`` segment that is FULLY confined to the prefix is removed, and the
    remainder is re-checked with the unchanged `is_tier3_dangerous_for_lethality`.
    That re-check is what makes every mixed/chained shape stay terminal without a
    per-shape carve:

    - a single ``rm`` with a mixed operand list (`rm -rf .pilot-scratch/x /etc/y`)
      is not fully confined, so it is NOT removed and still matches `rm -rf`;
    - a confined ``rm`` chained with another destructive verb
      (`rm -rf .pilot-scratch/x && git reset --hard`) leaves that verb in the
      remainder, which still fires;
    - two ``rm``s, one confined one not (`… .pilot-scratch/x && rm -rf /etc`),
      leaves the unconfined one, which still fires.

    Returns ``False`` (stays terminal) when no segment was confined, or when the
    remainder is still proven-dangerous. Fail-closed throughout via
    `is_within_pilot_scratch` and `_rm_segment_operands`."""
    survivors: list[str] = []
    carved = False
    for seg in _split_compound_command(command):
        operands = _rm_segment_operands(seg)
        if operands and all(is_within_pilot_scratch(op, cwd) for op in operands):
            carved = True
            continue
        survivors.append(seg)
    if not carved:
        return False
    return not is_tier3_dangerous_for_lethality("\n".join(survivors))


# ── mika#2565 (case B): in-worktree `sed -i` source edit is not proven danger ─
#
# `TIER3_PATTERNS`' `sed -i` entry (`:183`, `\bsed\s+(-\w*i|-i\w*)\b`) matches on
# the FLAG alone, regardless of target — correct for the REFUSAL (`sed -i` is
# not an allow-listed idiom, so the command must stay DENIED and the pilot must
# fall back to the Edit tool), but it made the denial TERMINAL even when the
# in-place edit lands on an ORDINARY REPO SOURCE FILE inside the worktree. The
# verbatim mika#2565 killer (session 6d61c747, l.5597, 20:38:05Z,
# `[bash-grep] (terminal)`):
#
#   sed -i '5870,5990s/classify_dependabot_verdict(/classify_2519(/' \
#     crates/mika-agent/src/evidence/guards.rs && grep -n "…" …/guards.rs
#
# edits a tracked source file by RELATIVE path — a routine in-worktree edit, not
# proven danger (cpp#205). The deny STAYS (admission byte-identical: neither
# `is_tier3_dangerous`, `is_tier1_auto_approve`, nor any YAML rule consults the
# functions below); only lethality flips, so the pilot adapts to the Edit tool
# instead of dying.
#
# Containment is decided by the SAME `is_within_project` mechanism the
# destination veto uses — cwd/fs-aware, symlink-resolving. Every fail-closed
# direction keeps the command TERMINAL, matching cpp#213's boundary that this
# only ever flips terminal→survivable, never refused→allowed.

# ONE `sed -i` in-place write-free substitution UNIT, optional leading address/
# range, matched from the start of the (whitespace-stripped) string but NOT
# anchored at the end — `_sed_i_script_all_safe_subs` (cpp#243) consumes these
# units one at a time, requiring a `;` between them, so a multi-substitution
# script (`s/a/b/g; s/c/d/g`, several `-e`) is validated without ever
# hand-parsing the script to find where it ends. Reuses the exact separators and
# flag charset of `_SAFE_SED_SUB_RES`/`_is_safe_sed_pure_substitution` (only
# `g`/`p`/`i`/`I`/`m`/`M`/digits — NO `w`/`W` write flag, NO `e` exec flag), so
# the carve can NEVER apply to a script that writes to, reads from, or execs
# another file. A standalone `w`/`W`/`r`/`R`/`e` command, a trailing `w FILE`
# write on a substitution (`s/a/b/w /etc/x` — `w` is not in the flag charset,
# so the unit ends before it and the leftover `w …` matches no further unit), a
# `d`/`y`/`a`/`i`/`c` command, or an unrecognized separator all fail the scan
# and keep the segment TERMINAL (fail-closed). The incident's script is a plain
# address-range substitution and matches.
_SED_I_ADDR = r"(?:\d+|\$|/(?:[^/\\]|\\.)*/)"
_SED_I_SUBST_UNIT_RE = re.compile(
    r"\s*(?:" + _SED_I_ADDR + r"(?:," + _SED_I_ADDR + r")?)?"
    r"s(?P<sep>[/@#|:])"
    r"(?:(?!(?P=sep))[^\\]|\\.)*(?P=sep)"
    r"(?:(?!(?P=sep))[^\\]|\\.)*(?P=sep)"
    r"[gpiImM0-9]*"
)


def _sed_i_script_all_safe_subs(script: str) -> bool:
    """True iff *script* is one or more write-free substitution commands,
    separated by ``;`` (cpp#243).

    Consumes `_SED_I_SUBST_UNIT_RE` matches from the front; after each unit the
    remainder must be empty or a ``;`` followed by another unit. This validates
    the WHOLE script (single-`s`, multi-`;`, or the concatenation of several
    ``-e`` scripts) as substitutions ONLY, without hand-parsing the script to
    locate file operands — the `;` seen by the scanner is always a top-level
    command separator, because each unit regex has already consumed the
    substitution up to its closing separator and flags (a `;` inside a PATTERN or
    REPLACEMENT is an ordinary character there, never reached by the scanner).

    Fail-CLOSED (``False``): an empty script, any non-`s` command (`w`/`W`/`r`/
    `R`/`e`/`d`/`y`/`a`/`i`/`c`), a trailing `w FILE` write flag on a
    substitution, an unrecognized separator, or a trailing/empty `;` command.
    """
    rest = script.strip()
    if not rest:
        return False
    while True:
        m = _SED_I_SUBST_UNIT_RE.match(rest)
        if m is None:
            return False
        rest = rest[m.end() :].lstrip()
        if not rest:
            return True
        if rest[0] != ";":
            return False
        rest = rest[1:].lstrip()
        if not rest:
            # A trailing `;` with no following command — fail closed.
            return False

def _sed_inplace_suffix(tok: str) -> str | None:
    """If *tok* is a GNU sed in-place flag — bare ``-i``, a clustered ``-ni``, or
    a SUFFIX form ``-i.bak`` / ``-i.orig`` / ``-ibak`` (cpp#253, mika#2601) —
    return its backup SUFFIX (``""`` for bare ``-i``); otherwise ``None``.

    GNU sed: inside a short-option cluster the ``-i`` option consumes the REST of
    the argument as its optional backup suffix, so the flag is the FIRST ``i``
    after the leading dash and the suffix is everything after it. The suffix forms
    ALSO write ``<file><SUFFIX>`` beside each target (`mod.rs` → `mod.rs.bak`),
    inside the worktree; the returned suffix lets the caller confine that backup
    too (cpp#229 invariant, extended to the backup). The leading cluster (before
    the ``i``) must be ASCII letters — other no-arg short flags (`-n`, `-r`, …) —
    matching the pre-cpp#253 `-[A-Za-z]*i[A-Za-z]*` charset; ``--in-place`` (a
    ``--``-prefixed long option) is not recognized here and is not caught by
    ``TIER3_PATTERNS`` anyway (already survivable). A ``*`` in the suffix is a GNU
    wildcard (each ``*`` is replaced by the filename, which can PROJECT the backup
    to an arbitrary path), so it disqualifies the token → fail closed (stays
    terminal). This is a LINEAR scan — no backtracking regex — so recognition stays
    bounded-time on pathological input (cpp#250 ReDoS lesson).
    """
    if len(tok) < 2 or tok[0] != "-":
        return None
    idx = tok.find("i", 1)
    if idx == -1:
        return None
    lead = tok[1:idx]
    if lead and not (lead.isascii() and lead.isalpha()):
        return None
    suffix = tok[idx + 1 :]
    if "*" in suffix:
        return None
    return suffix


def _sed_i_target_operands(segment: str) -> tuple[list[str], str] | None:
    """The in-place FILE target operand(s) of a ``sed -i`` *segment* whose script
    is one or more write-free substitutions, paired with the backup SUFFIX (empty
    string for bare ``-i``; ``.bak``/``.orig``/… for the GNU attached-suffix forms
    ``-i.bak`` — cpp#253), or ``None`` (fail-closed — the caller keeps the segment,
    so it stays terminal) for every shape this carve must not touch (mika#2565,
    cpp#243). The suffix lets the caller confine the ``<file><SUFFIX>`` backup that
    the suffix forms write beside each target.

    The sed SCRIPT is one shlex argument regardless of its internal content
    (multiple `s///g` separated by `;`, escaped separators `\\/`, embedded
    `"…"`), and several ``-e``/``--expression`` scripts each contribute one such
    argument. shlex separates the script argument(s) from the FILE operands
    reliably; the file operands are then the non-option positionals that are NOT
    the (positional) script. The script content is NOT hand-parsed to find where
    it ends — every collected script is validated in full by
    `_sed_i_script_all_safe_subs` (substitutions only, no `w`/`W`/`r`/`R`/`e`).

    ``None`` when: the segment does not tokenize (``shlex`` raises); its leading
    word is not exactly ``sed`` (a path-qualified ``/bin/sed`` stays terminal);
    no short ``-i`` in-place flag is present; a script-FILE flag
    (``-f``/``--file``) is present (the external script cannot be inspected →
    fail closed); ANY collected script is not a pure-substitution script
    (`_sed_i_script_all_safe_subs`); or there is no file operand after the
    script(s).
    """
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return None
    if not tokens or tokens[0] != "sed":
        return None
    saw_in_place = False
    backup_suffix = ""  # GNU `-i<SUFFIX>` attached backup suffix (cpp#253)
    scripts: list[str] = []  # explicit `-e`/`--expression` script expressions
    positionals: list[str] = []
    end_of_opts = False
    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not end_of_opts and tok == "--":
            end_of_opts = True
            i += 1
            continue
        if not end_of_opts and tok.startswith("-") and tok != "-":
            # `-f`/`--file` name an EXTERNAL script file whose content is
            # unknowable — fail closed rather than admit an uninspectable script.
            if (
                tok in ("-f", "--file")
                or tok.startswith("--file=")
                or tok.startswith("-f")  # combined `-f<path>`
            ):
                return None
            # `-e`/`--expression` supply an INLINE script expression: separate
            # (`-e SCRIPT`), combined (`-eSCRIPT`), or `--expression=SCRIPT`.
            if tok in ("-e", "--expression"):
                i += 1
                if i >= n:
                    return None
                scripts.append(tokens[i])
                i += 1
                continue
            if tok.startswith("--expression="):
                scripts.append(tok[len("--expression=") :])
                i += 1
                continue
            if tok.startswith("-e") and len(tok) > 2:
                scripts.append(tok[2:])
                i += 1
                continue
            suffix = _sed_inplace_suffix(tok)
            if suffix is not None:
                saw_in_place = True
                backup_suffix = suffix
            i += 1
            continue
        positionals.append(tok)
        i += 1
    if not saw_in_place:
        return None
    if scripts:
        # `-e`/`--expression` present → every positional is a file operand.
        files = positionals
    else:
        # Bare form → the FIRST positional is the script, the rest are files.
        if not positionals:
            return None
        scripts = [positionals[0]]
        files = positionals[1:]
    if not files:  # need >= 1 file target
        return None
    if not all(_sed_i_script_all_safe_subs(scr) for scr in scripts):
        return None
    return files, backup_suffix


def _sed_i_target_confined(target: str, cwd: str) -> bool:
    """Whether a ``sed -i`` file *target* is a RELATIVE path resolving strictly
    inside the worktree ``cwd`` (mika#2565).

    Fail-CLOSED (``False`` — caller keeps the command terminal) for an ABSOLUTE
    path (`/etc/x`), a ``$``/``~``-rooted operand (`$HOME/x`, `~/x` — the SAME
    anti-respelling disqualifier the redirect/cp-mv/mkdir vetoes apply, since
    `is_within_project` does no shell expansion and would read `Path(cwd) /
    "$HOME/x"` as a contained same-named subdir), and — via `is_within_project`
    itself — any ``..`` traversal or outbound-symlink target, or a ``cwd`` that
    cannot be resolved.
    """
    if not target:
        return False
    if Path(target).is_absolute():
        return False
    if target.startswith("$") or target.startswith("~"):
        return False
    return is_within_project(target, cwd)


def _sed_i_edit_and_backup_confined(target: str, suffix: str, cwd: str) -> bool:
    """Whether a ``sed -i<suffix>`` edit is fully confined: the edited *target*
    AND — for the attached-suffix forms (`-i.bak`) — the ``<target><suffix>``
    backup file both resolve strictly inside the worktree ``cwd`` (cpp#253).

    GNU sed with a non-``*`` suffix writes the backup by APPENDING the suffix to
    the filename (`crates/…/mod.rs` → `crates/…/mod.rs.bak`), so the backup path
    is ``target + suffix``. A suffix that would project that backup out of the
    worktree (`.bak/../../etc/x`) is rejected by `_sed_i_target_confined` via
    `is_within_project` (fail-closed). Bare ``-i`` (empty suffix) writes no
    backup, so only the edited target is checked. (The ``*`` wildcard suffix
    never reaches here — `_SED_INPLACE_FLAG_RE` excludes it, so such a token is
    not recognized as in-place and the segment stays terminal.)
    """
    if not _sed_i_target_confined(target, cwd):
        return False
    if suffix and not _sed_i_target_confined(target + suffix, cwd):
        return False
    return True


def sed_i_confined_to_worktree(command: str, cwd: str) -> bool:
    """Whether *command*'s tier3-for-lethality danger is due SOLELY to
    ``sed -i`` substitution segment(s) whose EVERY file target resolves
    strictly inside the worktree ``cwd`` (mika#2565).

    Consulted ONLY by ``permissions._denial_is_terminal`` — the LETHALITY
    decision. Never touches admission (`is_tier3_dangerous`,
    `is_tier1_auto_approve`, YAML rules); the command stays refused either way.

    Mirrors `rm_confined_to_pilot_scratch` (cpp#213): each fully-confined
    ``sed -i`` segment is removed and the remainder re-checked with the
    unchanged `is_tier3_dangerous_for_lethality`, so every mixed/chained shape
    stays terminal without a per-shape carve — a `sed -i` chained with another
    destructive verb (`… && git reset --hard`), a mixed target list
    (`sed -i '…' a.rs /etc/passwd`), or a second unconfined danger all leave a
    proven-danger remainder that still fires. Returns ``False`` (stays terminal)
    when no segment was confined or the remainder is still proven-dangerous.
    """
    survivors: list[str] = []
    carved = False
    for seg in _split_compound_command(command):
        result = _sed_i_target_operands(seg)
        if result is not None:
            targets, suffix = result
            if all(
                _sed_i_edit_and_backup_confined(t, suffix, cwd) for t in targets
            ):
                carved = True
                continue
        survivors.append(seg)
    if not carved:
        return False
    return not is_tier3_dangerous_for_lethality("\n".join(survivors))


# ── cpp#237: a read-only WAIT-LOOP script is not on its own session-fatal ──────
#
# mika#2105 (pilot e1a6c78b, 2026-09-29T15:13:20Z) died TERMINAL on a pure
# read-only wait-loop:
#
#   sh -c 'n=0; while [ $n -lt 55 ]; do if [ -f .pilot-scratch/measures.txt ];
#          then cat .pilot-scratch/measures.txt; exit 0; fi; sleep 10;
#          n=$((n+1)); done; du -sm target; tail -1 .pilot-scratch/cold0.log'
#
# The ONLY terminal cause (proven at source on HEAD c136814) is the `\bsh\s+-c\b`
# entry of `TIER3_PATTERNS` — one of the verb patterns in
# `_TIER3_VERB_PATTERNS_FOR_LETHALITY`, so `is_tier3_dangerous_for_lethality`
# returns True and `_denial_is_terminal` returns True at its first gate. NOT the
# arithmetic `$((n+1))` (single-quoted, never reaches a redirect scanner; it is
# not a `$(` command substitution to any lethality mask), NOT `du`/`cat`/`tail`,
# NOT a `while`/`done`/`;` token, NOT any redirect (there is none). The `sh -c`
# verb is lethal because a wrapper CAN smuggle anything — but under cpp#205
# (terminal reserved to PROVEN danger) the danger of `sh -c` is exactly the
# danger of the SCRIPT it wraps. When that script is a read-only wait-loop whose
# every command is read-only and every file target is worktree-relative, there
# is no proven danger, so the denial must not be terminal.
#
# LETHALITY ONLY, admission byte-identical. This is the exact sibling of the
# cpp#213 rm/.pilot-scratch and mika#2565 sed-i carves: consulted ONLY by
# `permissions._denial_is_terminal`, never by `is_tier3_dangerous` /
# `is_tier1_auto_approve` / any YAML rule / `is_tier3_dangerous_for_lethality`
# itself. The command STAYS refused (`sh -c` is still tier3-dangerous for the
# REFUSAL and is never tier1-auto-approved); only `_denial_is_terminal` flips
# True→False, so the pilot survives and adapts (reach for a native tool / a
# scratch-file poll) instead of the run being killed.
#
# Recognition is PURELY LEXICAL (no cwd, no filesystem, no `Path.resolve()`) —
# the same load-bearing choice `is_tier3_dangerous_for_lethality` and
# `_is_sanctioned_tmp_scratch` document: the script is DENIED and never executes,
# so this decides only whether an already-refused command's refusal is fatal.
# The recognizer is FAIL-CLOSED on every ambiguity: any command word outside the
# read-only allowlist (`curl`, `wget`, `rm`, `eval`, `cp`, `mv`, `tee`, `dd`, a
# nested `sh`/`bash`, …), any command substitution (`` ` ``, `$(cmd)`, `${ …; }`
# funsub, `$'…'`), any pipe/background/subshell/redirect metacharacter
# (`| & < > ( )`), any absolute / `..` / `~` / dangerous-env-var (`$HOME`,
# `$OLDPWD`, `$PWD`, …) file operand, or any unbalanced quote makes it return
# False and the `sh -c` verb keeps the denial TERMINAL. Arithmetic `$((…))` with
# no nested `$` is inert and is blanked before the substitution check, so a
# counter increment (`n=$((n+1))`) is allowed while `$(( $(cmd) ))` (a real
# command substitution smuggled inside arithmetic) survives the blank and is
# rejected by the `$(`-remains check.

# Command words that only READ or are inert — safe as the leading word of a
# simple command inside a recognized wait-loop. No writer, no network, no
# exec/eval, no `sh`/`bash` wrapper. `[`/`test` are the POSIX condition builtins.
_WAITLOOP_READONLY_CMDS: frozenset[str] = frozenset(
    {
        "[",
        "test",
        "cat",
        "head",
        "tail",
        "du",
        "ls",
        "wc",
        "sleep",
        "exit",
        ":",
        "true",
        "false",
        "echo",
        "printf",
        "grep",
        "stat",
        "dirname",
        "basename",
    }
)

# Shell keywords that STRUCTURE a loop/conditional — not commands.
_WAITLOOP_KEYWORDS: frozenset[str] = frozenset(
    {"while", "until", "for", "do", "done", "if", "then", "elif", "else", "fi", "in"}
)

# Environment variables whose expansion names a path OUTSIDE the worktree (or
# rewrites word-splitting/lookup) — a `$HOME`/`$OLDPWD`/… file operand is not a
# worktree-relative target, so it disqualifies the read-only recognition.
_WAITLOOP_DANGEROUS_VARS: frozenset[str] = frozenset(
    {"HOME", "OLDPWD", "PWD", "IFS", "PATH", "ENV", "BASH_ENV", "CDPATH", "TMPDIR"}
)

# `sh -c '<script>'` / `bash -c "<script>"` wrapper: the whole command is the
# wrapper and a single quoted script argument, nothing after the closing quote.
_WAITLOOP_SH_C_WRAPPER_RE = re.compile(
    r"^\s*(?:sh|bash)\s+-c\s+(['\"])(?P<body>.*)\1\s*$", re.DOTALL
)
_WAITLOOP_SH_C_PREFIX_RE = re.compile(r"^\s*(?:sh|bash)\s+-c\b")

# Arithmetic expansion `$((…))` whose interior carries NO `$`/backtick — inert
# integer arithmetic, blanked to `0` before the command-substitution check so a
# loop counter (`n=$((n+1))`) is allowed. An interior `$` (a command
# substitution smuggled inside arithmetic, `$(( $(cmd) ))`) does NOT match, so
# the residual `$(` is caught by the reject below (fail-closed).
_WAITLOOP_ARITH_RE = re.compile(r"\$\(\((?:[^()$`]|\([^()$`]*\))*\)\)")

_WAITLOOP_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_WAITLOOP_LOOP_KEYWORD_RE = re.compile(r"\b(?:while|until|for)\b")


def _waitloop_operand_is_safe(tok: str) -> bool:
    """Whether *tok* is a safe operand inside a recognized read-only wait-loop:
    a flag, the `[`/`]` test brackets, or a WORKTREE-RELATIVE literal — never an
    absolute / `..` / `~` / dangerous-env-var path. Purely lexical."""
    if tok in ("[", "]"):
        return True
    if tok.startswith("-"):  # a flag (`-f`, `-lt`, `-sm`, `-1`, `-n`, `--`)
        return True
    if tok.startswith("/") or tok.startswith("~"):
        return False
    if ".." in tok:
        return False
    if "$" in tok:
        # Only bare parameter expansions `$name` / `${name}` survive here (`$(`,
        # backtick, `$'`, funsub are already globally rejected upstream). A bare
        # `$` with no identifier, or a reference to a dangerous env var, fails.
        if re.search(r"\$(?![A-Za-z_{])", tok):
            return False
        for name in re.findall(r"\$\{?([A-Za-z_]\w*)", tok):
            if name in _WAITLOOP_DANGEROUS_VARS:
                return False
    return True


def _waitloop_statement_is_readonly(statement: str) -> bool:
    """Whether one `;`/newline-separated statement is a read-only simple command
    (optionally led by loop/conditional keywords or variable assignments), or a
    `for NAME in <safe list>` head. Fail-closed on any tokenization error."""
    try:
        toks = shlex.split(statement)
    except ValueError:
        return False
    i = 0
    while i < len(toks) and toks[i] in _WAITLOOP_KEYWORDS:
        kw = toks[i]
        i += 1
        if kw == "for":
            # `for NAME in <list>` — skip the loop variable and `in`; every
            # remaining token is an iteration operand, with no command word.
            if i < len(toks):
                i += 1  # loop variable name
            if i < len(toks) and toks[i] == "in":
                i += 1
            return all(_waitloop_operand_is_safe(t) for t in toks[i:])
    if i >= len(toks):
        return True  # keywords only (`fi`, `done`, `else`) or empty
    # Leading `NAME=value` assignment prefixes (the command word, if any, follows).
    while i < len(toks) and _WAITLOOP_ASSIGN_RE.match(toks[i]):
        if not _waitloop_operand_is_safe(toks[i].split("=", 1)[1]):
            return False
        i += 1
    if i >= len(toks):
        return True  # a pure assignment statement (`n=0`)
    if toks[i] not in _WAITLOOP_READONLY_CMDS:
        return False
    return all(_waitloop_operand_is_safe(t) for t in toks[i + 1 :])


def _waitloop_split_statements(script: str) -> list[str] | None:
    """Quote-aware split of *script* on unquoted `;` / newline. ``None`` on an
    unbalanced quote (fail-closed)."""
    out: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    for ch in script:
        if quote is not None:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch in ";\n":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if quote is not None:
        return None
    out.append("".join(cur))
    return out


def is_readonly_waitloop_script(command: str) -> bool:
    """Whether *command*'s tier3-for-lethality danger is due SOLELY to being a
    read-only WAIT-LOOP script — a `while`/`until`/`for` loop (optionally wrapped
    in a single `sh -c`/`bash -c`), composed only of read-only commands
    (`[`/`test`, `cat`, `head`, `tail`, `du`, `ls`, `wc`, `sleep`, `exit`, `:`,
    `true`, `false`, `echo`, `printf`, `grep`, …), inert arithmetic (`$((…))`),
    and worktree-relative file operands (cpp#237).

    Consulted ONLY by ``permissions._denial_is_terminal`` — the LETHALITY
    decision. Never touches admission (`is_tier3_dangerous`,
    `is_tier1_auto_approve`, YAML rules, `is_tier3_dangerous_for_lethality`); the
    command stays refused either way. Purely lexical, fail-closed on any
    ambiguity — see the block comment above. Returns ``False`` (stays terminal)
    for anything that is not provably a self-contained read-only wait-loop."""
    if not isinstance(command, str) or not command:
        return False
    m = _WAITLOOP_SH_C_WRAPPER_RE.match(command)
    if m is not None:
        script = m.group("body")
    elif _WAITLOOP_SH_C_PREFIX_RE.match(command):
        # A `sh -c`/`bash -c` wrapper we cannot cleanly unwrap (unbalanced /
        # trailing tokens) → fail closed.
        return False
    else:
        script = command
    # Global rejects: any command substitution or unquotable expansion.
    if "`" in script or "$'" in script or re.search(r"\$\{[\s|]", script):
        return False
    blanked = _WAITLOOP_ARITH_RE.sub("0", script)
    if "$(" in blanked:  # a command substitution (arithmetic already blanked)
        return False
    # Global rejects: any pipe / background / subshell / redirect metacharacter.
    if any(ch in blanked for ch in "|&<>()"):
        return False
    # Must actually BE a loop — a bare `sh -c 'cat x'` is out of scope.
    if not _WAITLOOP_LOOP_KEYWORD_RE.search(blanked):
        return False
    statements = _waitloop_split_statements(blanked)
    if statements is None:
        return False
    saw_do = saw_done = False
    for stmt in statements:
        toks = stmt.split()
        if "do" in toks:
            saw_do = True
        if "done" in toks:
            saw_done = True
        if not _waitloop_statement_is_readonly(stmt):
            return False
    return saw_do and saw_done
