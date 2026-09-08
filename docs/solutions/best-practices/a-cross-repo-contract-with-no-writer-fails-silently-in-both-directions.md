---
title: "A cross-repo contract shipped without one of its three parts fails silently — the absence of a file is not a signal"
date: 2026-09-08
module: claude_pilot.transcript_writer
component: pilot-transcript
problem_type: architecture_pattern
category: best-practices
severity: high
tags: [cross-repo-contract, observability, silent-failure, anti-vacuity-detector, fail-open, schema-version, claude-pilot-165, mika-2040, mika-1705]
applies_when: "shipping a data contract whose producer, writer, and reader live in different repositories"
---

# A cross-repo contract with no writer fails silently in both directions

## What happened

mika#1705 shipped a pilot-transcript capture in two of its three parts:

- the **producer** — the skills executor in the **mika** repo (a different
  repository; no path below resolves in this checkout), which injects
  `ANTHROPIC_LOG_FILE=~/.mika/data/pilot-transcripts/<task-id>.jsonl` at every
  dispatch and mounts the directory writable in the sandbox;
- the **reader** — added later (mika#2040 / PR#2240), which parses the lines
  into `pilot_transcripts`.

The **writer** was never built. `grep ANTHROPIC_LOG_FILE` in claude-pilot
returned zero hits. For 25+ days every dispatch set the variable, mounted the
directory, and produced nothing — and nothing said so, because **"no file" is
indistinguishable from "no session."** Both halves looked healthy from their own
side: the producer set its variable, the reader found nothing to reject.

A second failure compounded it. Five autonomous attempts on the parent ticket
all touched mika only (0 files in claude-pilot): the ticket was cross-repo but
dispatched mono-repo, so the missing writer was never *attempted*. That is a
non-try, not a feasibility failure — and the two are easy to confuse from the
outside, because both look like "five attempts, still broken."

## The pattern

**A contract with N parts in N repos needs each part to be able to say it is
missing.** Silence is only evidence when someone is listening for it. Three
things make that true:

**1. A mandatory version field, refused by name.** The reader accepts exactly
`schema_version: "v1"` and rejects a line whose version is absent, malformed, or
unknown — quoting what it received, not only what it expected. A contract with
no version and no refusal fails silently in *both* directions: the writer cannot
tell it is writing a shape nobody reads, and the reader cannot tell it is
skipping lines it should have understood.

**2. An anti-vacuity detector on each side.** Not "does the pipeline run" but
"did it produce ≥1 line." On the writer's side that is a test driving a short
session with the variable set and asserting the file exists *and* parses. On the
reader's side it is a WARN when a dispatch completes with an empty transcript.
Either alone leaves the other half's absence invisible.

**3. Own the writer's failure mode explicitly.** An observability side channel
must **fail open** — an unwritable path can never cost a dispatch. But
fail-open is where silence comes back, so the failure classes must be
separated by *scope*:

```python
# A value the serializer cannot render is a property of ONE message.
try:
    line = json.dumps(transcript_line(message), default=str)
except Exception as exc:
    _warn_once("_warned_serialization", f"skipped one message: {exc}")
    return False

# An unwritable path is a property of the SESSION.
try:
    ...
    fh.write(line + "\n")
except OSError as exc:
    _disarmed = True
    log_error("pilot-transcript", [f"disabled after write failure on {path}: {exc}"])
    return False
```

Sharing one handler lets a single odd tool input disarm the writer for every
message after it — reintroducing exactly the silent loss the work was closing.
Both paths log **once**: a session carries thousands of messages, and one error
line per message drowns the stderr that the diagnosis depends on.

## Where the hook goes

Put the capture at the **top of the message loop**, not inside the per-type
branches. claude-pilot's `ResultMessage` branch can `break` before doing
anything (the deny-resume recovery path), so a writer sitting inside it would
lose precisely the sessions that died on a refusal — the population a transcript
exists to diagnose. One site above every branch cannot be escaped by a `break`
or `continue` added later, and there is only one place to audit.

Record the **complete** messages only. The partial-message stream carries the
same tokens as the message that closes the turn; recording both double-counts
every token in the session.

## Prevention

When a ticket's work spans repositories, name the repo each part lives in
*before* dispatching, and dispatch into the repo where the missing part belongs.
A ticket that is cross-repo in fact but mono-repo in dispatch produces attempts
that cannot reach half its own scope — and the attempt count then reads as
difficulty rather than as the routing bug it is.
