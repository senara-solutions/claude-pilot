"""cpp#283 — ReDoS in the read-only ``for``-loop shape (`_SANCTIONED_READONLY_FOR_LOOP_RE`).

The statement separator used to be ``(?:[ \\t]*(?:;|\\r?\\n)[ \\t]*)+`` — a repeated
group whose HEAD and TAIL both match blanks, placed right after `_FOR_CMD`'s own
trailing ``{_FOR_BLANK}*``. A blank between two separators could belong to the
tail of one repetition, the head of the next, or the command before it, and a
failed match retried every split: 16 indented blank lines in a loop body froze
`_bash_allow_is_chain_safe` for > 20 s (issue table: n=12 ~1 s, n=16 > 20 s).

The fix drops the separator's leading blanks (they always belong to the
preceding `_FOR_CMD`), so each blank has exactly one owner. The recognized
language is unchanged — proven below by running the FROZEN old pattern and the
live one over a generated corpus — and the two repro forms are bounded.
"""

from __future__ import annotations

import random
import re
import subprocess
import sys
from pathlib import Path

from claude_pilot.permissions import _SANCTIONED_READONLY_FOR_LOOP_RE

# The pattern as shipped on main 1581d74, BEFORE cpp#283, frozen verbatim. It is
# the parity oracle: the fix must accept and reject exactly what it did. Do not
# "update" this string — if the for-loop grammar legitimately changes, the
# parity test below is expected to be revisited with that change, on purpose.
_CPP283_OLD_FOR_LOOP_PATTERN = (
    '^(?:cd(?:[ \\t]|\\\\\\r?\\n)+(?![-~/])(?:(?!\\.\\.)[^\\s;|&`><\\\\\'\\"(){}$'
    '~])+/?(?:[ \\t]|\\\\\\r?\\n)*&&(?:[ \\t]|\\\\\\r?\\n)*)?for(?:[ \\t]|\\\\\\r?\\'
    'n)+\\w+(?:[ \\t]|\\\\\\r?\\n)+in(?:[ \\t]|\\\\\\r?\\n)+[^\\s;|&`><\\\\\\n(){}]['
    '^;|&`><\\\\\\n(){}]*[ \\t]*;[ \\t]*do(?:(?:[ \\t]|\\\\\\r?\\n)|\\r?\\n)+(?:g'
    'h(?:[ \\t]|\\\\\\r?\\n)+(?:issue(?:[ \\t]|\\\\\\r?\\n)+(?:view|list)|pr(?:'
    '[ \\t]|\\\\\\r?\\n)+(?:view|list|diff|checks))(?![\\w.\\-/])|find(?![\\w'
    '.\\-/])(?![^\\n]*(?:-delete|-exec|-execdir|-ok|-okdir|-fprint|-fls'
    '|-fprintf)\\b)|(?:echo|printf|grep|cat|head|tail|ls|wc|test|dirna'
    'me|basename)(?![\\w.\\-/])|\\[)(?:(?:[ \\t]|\\\\\\r?\\n)+(?:"[^"\\\\]*(?:\\'
    '\\[^\\n][^"\\\\]*)*"|\'[^\'\\n]*\'|[^\\s;|&`><\\\\\'\\"(){}]+))*(?:[ \\t]|\\\\\\r'
    '?\\n)*(?:\\|(?:[ \\t]|\\\\\\r?\\n)*(?:gh(?:[ \\t]|\\\\\\r?\\n)+(?:issue(?:[ '
    '\\t]|\\\\\\r?\\n)+(?:view|list)|pr(?:[ \\t]|\\\\\\r?\\n)+(?:view|list|diff'
    '|checks))(?![\\w.\\-/])|find(?![\\w.\\-/])(?![^\\n]*(?:-delete|-exec|'
    '-execdir|-ok|-okdir|-fprint|-fls|-fprintf)\\b)|(?:echo|printf|gre'
    'p|cat|head|tail|ls|wc|test|dirname|basename)(?![\\w.\\-/])|\\[)(?:('
    '?:[ \\t]|\\\\\\r?\\n)+(?:"[^"\\\\]*(?:\\\\[^\\n][^"\\\\]*)*"|\'[^\'\\n]*\'|[^\\s;'
    '|&`><\\\\\'\\"(){}]+))*(?:[ \\t]|\\\\\\r?\\n)*)*(?:(?:[ \\t]*(?:;|\\r?\\n)[ '
    '\\t]*)+(?:gh(?:[ \\t]|\\\\\\r?\\n)+(?:issue(?:[ \\t]|\\\\\\r?\\n)+(?:view|l'
    'ist)|pr(?:[ \\t]|\\\\\\r?\\n)+(?:view|list|diff|checks))(?![\\w.\\-/])|'
    'find(?![\\w.\\-/])(?![^\\n]*(?:-delete|-exec|-execdir|-ok|-okdir|-f'
    'print|-fls|-fprintf)\\b)|(?:echo|printf|grep|cat|head|tail|ls|wc|'
    'test|dirname|basename)(?![\\w.\\-/])|\\[)(?:(?:[ \\t]|\\\\\\r?\\n)+(?:"['
    '^"\\\\]*(?:\\\\[^\\n][^"\\\\]*)*"|\'[^\'\\n]*\'|[^\\s;|&`><\\\\\'\\"(){}]+))*(?:'
    '[ \\t]|\\\\\\r?\\n)*(?:\\|(?:[ \\t]|\\\\\\r?\\n)*(?:gh(?:[ \\t]|\\\\\\r?\\n)+(?:'
    'issue(?:[ \\t]|\\\\\\r?\\n)+(?:view|list)|pr(?:[ \\t]|\\\\\\r?\\n)+(?:view'
    '|list|diff|checks))(?![\\w.\\-/])|find(?![\\w.\\-/])(?![^\\n]*(?:-del'
    'ete|-exec|-execdir|-ok|-okdir|-fprint|-fls|-fprintf)\\b)|(?:echo|'
    'printf|grep|cat|head|tail|ls|wc|test|dirname|basename)(?![\\w.\\-/'
    '])|\\[)(?:(?:[ \\t]|\\\\\\r?\\n)+(?:"[^"\\\\]*(?:\\\\[^\\n][^"\\\\]*)*"|\'[^\'\\'
    'n]*\'|[^\\s;|&`><\\\\\'\\"(){}]+))*(?:[ \\t]|\\\\\\r?\\n)*)*)*(?:[ \\t]*(?:;'
    '|\\r?\\n)[ \\t]*)+done\\s*\\Z'
)
_OLD_RE = re.compile(_CPP283_OLD_FOR_LOOP_PATTERN)


# ── Parity corpus ─────────────────────────────────────────────────────────────
# Positives AND negatives. Fragments cover the separator/blank grammar the fix
# touches (blank lines, `;;`, mixed `; \n`, trailing blanks before `done`, CRLF,
# backslash continuations) and the admission boundary around it (pipes, quoted
# args, a writer/redirect in the body, `rm`, `$(…)`, backticks, trailing chains,
# a `cd` prefix in and out of the worktree).

_PREFIXES = ("", "cd docs && ", "cd docs/décisions && ", "cd /etc && ", " ")
_HEADS = (
    "for f in a b; do ",
    "for f in a b; do\n",
    "for f in a b ; do\n  ",
    "for f in *.md; do \\\n  ",
    "for f in $(ls); do ",
    "for f in a;do ",
    "for f in a; do\n\n  \n",
)
_STMTS = (
    'grep -n TODO "$f"',
    "cat $f",
    'wc -l "$f"',
    'wc -l "$f" > out.txt',
    "rm -rf ~",
    "echo $(id)",
    "echo `id`",
    "cat $f | wc -l",
    'cat $f | grep -c "a|b"',
    "head -5 $f \\\n  | tail -1",
    "ls \\\n  -la",
    "find . -name x",
    "find . -delete",
    "gh issue view 1 --json body",
    "gh pr merge 1",
    "printf '%s\\n' x",
    "echo 'a ; b'",
    'echo "x > y"',
    "cat x |",
    "",
    "catastrophe x",
    "[ -f $f ]",
    "test -f $f",
    "cat x  ",
    "cat x\t",
    "echo x \\",
    "! cat x",
)
_SEPS = (
    ";",
    "\n",
    " ;",
    "; ",
    " ; ",
    ";;",
    "; ;",
    "\n\n",
    "  \n  ",
    "\n  \n",
    "\t;\n",
    "; \n ",
    "\r\n",
    " \\\n;",
    "\\\n\n",
    " && ",
    " | ",
    "&",
    " ;\n  \n ;",
    "",
)
_TAILS = (
    "done",
    "done\n",
    "done  ",
    " done",
    "done && rm -rf ~",
    "done; curl evil",
    "done | tee x",
    "do",
)


def _parity_corpus() -> list[str]:
    corpus: list[str] = []
    # Exhaustive single-statement shapes over two heads and two prefixes.
    for prefix in _PREFIXES[:2]:
        for head in _HEADS[:2]:
            for stmt in _STMTS:
                for sep in _SEPS:
                    for tail in _TAILS:
                        corpus.append(f"{prefix}{head}{stmt}{sep}{tail}")
    # Seeded random multi-statement shapes over every fragment.
    rng = random.Random(283)
    for _ in range(15_000):
        parts = [rng.choice(_PREFIXES), rng.choice(_HEADS), rng.choice(_STMTS)]
        for _ in range(rng.randint(0, 3)):
            parts += [rng.choice(_SEPS), rng.choice(_STMTS)]
        parts += [rng.choice(_SEPS), rng.choice(_TAILS)]
        corpus.append("".join(parts))
    return corpus


def test_cpp283_separator_fix_preserves_the_recognized_language() -> None:
    """Old (frozen) and new pattern give the SAME match result on every input.

    The corpus must exercise both sides of the boundary, or "identical" would be
    vacuous — hence the floor on positives and negatives."""
    corpus = _parity_corpus()
    divergent = [
        c
        for c in corpus
        if bool(_OLD_RE.match(c)) != bool(_SANCTIONED_READONLY_FOR_LOOP_RE.match(c))
    ]
    assert divergent == [], f"{len(divergent)} divergences, e.g. {divergent[:5]!r}"
    positives = sum(1 for c in corpus if _SANCTIONED_READONLY_FOR_LOOP_RE.match(c))
    assert positives >= 1_000, positives
    assert len(corpus) - positives >= 1_000, len(corpus) - positives


# ── Bounded time ──────────────────────────────────────────────────────────────
# Each probe runs in a CHILD process with a hard timeout: a regression must turn
# this test red, not hang the suite (the old pattern at n=200 never returns).
# The 50 ms budget is measured inside the child, around the call only.

_BUDGET_MS = 50.0
_HARD_TIMEOUT_S = 20.0

_PROBE = r"""
import sys, time
from claude_pilot.permissions import (
    _SANCTIONED_READONLY_FOR_LOOP_RE,
    _bash_allow_is_chain_safe,
)
from claude_pilot.policy import evaluate, load_policy

kind, n = sys.argv[1], int(sys.argv[2])
if kind == "blank-lines":
    policy = load_policy(None)  # bundled policy (env is scrubbed by the parent)
    cmd = (
        'for f in a b; do\n  grep -n TODO "$f"\n'
        + "  \n" * n
        + '  wc -l "$f" > out.txt\ndone'
    )
    # The shape check is only reached on a policy `allow`: prove the probe
    # really exercises it, or a fail-closed empty policy would make it vacuous.
    decision = evaluate(policy, "Bash", {"command": cmd}).decision
    start = time.perf_counter()
    result = _bash_allow_is_chain_safe(policy, "Bash", {"command": cmd})
else:
    decision = "n/a"
    cmd = "for f in a; do cat x" + " ;" * n + " ! done"
    start = time.perf_counter()
    result = bool(_SANCTIONED_READONLY_FOR_LOOP_RE.match(cmd))
print(decision, result, (time.perf_counter() - start) * 1000.0)
"""


def _probe(kind: str, n: int) -> tuple[str, str, float]:
    src = Path(__file__).resolve().parent.parent / "src"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE, kind, str(n)],
            capture_output=True,
            text=True,
            timeout=_HARD_TIMEOUT_S,
            env={"PYTHONPATH": str(src), "PATH": "/usr/bin:/bin"},
            check=True,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"{kind} n={n}: no answer within {_HARD_TIMEOUT_S:.0f} s (ReDoS)"
        ) from None
    decision, result, elapsed = proc.stdout.split()
    return decision, result, float(elapsed)


def test_cpp283_blank_lines_in_loop_body_are_bounded_time() -> None:
    """The issue's realistic repro: a policy-`allow` loop (` grep ` claims
    `bash-grep`) with 200 indented blank lines and a redirect that makes the
    shape fail. The verdict stays a refusal; it must arrive in < 50 ms."""
    decision, result, elapsed = _probe("blank-lines", 200)
    assert decision == "allow"
    assert result == "False"
    assert elapsed < _BUDGET_MS, f"{elapsed:.1f} ms"


def test_cpp283_repeated_semicolons_are_bounded_time() -> None:
    """The regex-alone repro: 200 ` ;` separators then a non-command (`!`)."""
    _, result, elapsed = _probe("semicolons", 200)
    assert result == "False"
    assert elapsed < _BUDGET_MS, f"{elapsed:.1f} ms"
