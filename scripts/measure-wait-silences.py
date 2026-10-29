#!/usr/bin/env python3
"""measure-wait-silences.py — cpp#146.

WHY silences and not tool durations. `toolWaitCeilingMs` and
`modelWaitCeilingMs` act only when the SDK has been SILENT for longer than
`idleTimeoutMs`: any message, a subagent's included, rearms the deadline. A
tool that runs for twenty minutes while its subagent chatters is never
measured against a ceiling. So the quantity that calibrates a ceiling is the
length of a silence, classified by what the session was waiting for when it
fell silent — not how long tools take, whose p95 is seconds.

HOW a silence is classified, from the line that precedes it:
  - `stream event: message_stop` (or another end-of-turn trailer), then a tool
    result after the silence: a tool was running   -> awaiting_tool
  - `user message (tool result) received`: the model owes the next turn
                                                     -> awaiting_model
  - anything else                                    -> other
The tool is named from the last `[tool:use]`, `[tool:request]` or `[tool]` line
before the silence. Before cpp#184 (2026-10-29) only callback-gated tools left
such a line, so older silences can name a PREVIOUS tool: read the label as a
hint there, never as proof.

WHAT it reports: per state, how many silences reached the threshold, the
longest, the cumulative time held, and how many crossed each ceiling; then the
guardrails that actually fired. That cumulative column is the slot time cpp#146
asked to see: mika-dev runs one pilot at a time, so a silence is the loop's
single slot held.

Requires per-line timestamps (logger.py, since 2026-09-10); older logs are
skipped, not guessed at.

Usage:
  scripts/measure-wait-silences.py [--dir /var/log/claude-pilot] [--min-secs 300]
                                   [--days N] [--top 15]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TOOL_CEILING_S = 1800.0
MODEL_CEILING_S = 900.0

STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z\] (.*)$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TOOL_LINE = re.compile(r"^\[(?:tool|tool:use|tool:request)\](?: \(subagent\))? (\w+): (.*)")
GUARDRAIL = re.compile(r"^\[guardrail\] (\w+)")
TURN_END = ("message_stop", "message_delta", "content_block_stop")


@dataclass
class Silence:
    secs: float
    state: str
    session: str
    tool: str


def classify(previous: str, following: str) -> str:
    if "tool result) received" in previous:
        return "awaiting_model"
    if any(f"stream event: {t}" in previous for t in TURN_END) and (
        "tool result) received" in following
    ):
        return "awaiting_tool"
    return "other"


def scan(path: Path, min_secs: float, fired: dict[str, int]) -> list[Silence]:
    found: list[Silence] = []
    prev_t: float | None = None
    prev_body = ""
    last_tool = "?"
    with path.open(errors="replace") as fh:
        for raw in fh:
            m = STAMP.match(raw.rstrip("\n"))
            if not m:
                continue
            t = datetime.fromisoformat(m.group(1)).timestamp()
            body = ANSI.sub("", m.group(2))
            if prev_t is not None and t - prev_t >= min_secs:
                found.append(
                    Silence(t - prev_t, classify(prev_body, body), path.stem[:8], last_tool)
                )
            tool = TOOL_LINE.match(body)
            if tool:
                last_tool = f"{tool.group(1)}: {tool.group(2)[:80]}"
            g = GUARDRAIL.match(body)
            if g:
                fired[g.group(1)] += 1
            prev_t, prev_body = t, body
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default="/var/log/claude-pilot")
    ap.add_argument("--min-secs", type=float, default=300.0)
    ap.add_argument("--days", type=float, default=None)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"no such log directory: {root}", file=sys.stderr)
        return 2
    cutoff = time.time() - args.days * 86400 if args.days else 0.0

    fired: dict[str, int] = defaultdict(int)
    silences: list[Silence] = []
    scanned = 0
    for path in sorted(root.glob("*.log")):
        if path.stat().st_mtime < cutoff:
            continue
        scanned += 1
        silences.extend(scan(path, args.min_secs, fired))

    print(f"logs scanned: {scanned} (untimestamped lines are skipped)")
    print(f"silences >= {args.min_secs:.0f}s, by what the session was waiting for:")
    for state in ("awaiting_tool", "awaiting_model", "other"):
        xs = sorted((s.secs for s in silences if s.state == state), reverse=True)
        if not xs:
            print(f"  {state:15} n=0")
            continue
        print(
            f"  {state:15} n={len(xs):5} longest={xs[0]:6.0f}s held={sum(xs) / 3600:6.1f}h"
            f"  >=900s={sum(x >= MODEL_CEILING_S for x in xs)}"
            f"  >=1800s={sum(x >= TOOL_CEILING_S for x in xs)}"
        )
    print("guardrails that fired:")
    for name, n in sorted(fired.items(), key=lambda kv: -kv[1]):
        print(f"  {name:28} {n}")
    if not fired:
        print("  none")
    print(f"longest {args.top} silences:")
    for s in sorted(silences, key=lambda s: -s.secs)[: args.top]:
        print(f"  {s.secs:6.0f}s {s.session} {s.state:15} last tool seen: {s.tool}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
