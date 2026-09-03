---
title: "One trace is not a population — verify the premise on every member you name"
date: 2026-09-03
last_updated: 2026-09-03
module: claude_pilot.guardrails
component: idle-watchdog
problem_type: process_failure
category: best-practices
tags: [evidence, grooming, premise, n-of-1, log-analysis, mutation-testing, cpp-145, review]
applies_when: "a ticket, plan, or code comment states a shared property of N named cases, and the evidence shown is one of them"
---

# One trace is not a population

## Context

claude-pilot#145 opened with a strong, well-evidenced claim: six pilot sessions
were killed by `idleTimeout` while working, and

> **n=6, unanime.** Jamais un `[tool:request]` resté sans réponse. Le résultat
> d'outil **était arrivé** [...] puis 300 s de rien.

The ticket printed one verbatim trace (`aae80d84`) and a table listing all six
session ids beside that shared last line. Grooming accepted it. The plan carried
it into its Product Contract. The implementation carried it into a module
docstring. Three artifacts, one unverified generalization.

It is false for **half the population**. In `c56a973e`, `c5201301` and
`aae80d84` the tool result really is the last line. In `3d5fe1ec`, `f26add11`
and `e2f0ef97` the SDK delivers the old turn's closing trailers —
`message_delta`, `message_stop` — *after* it. Those trailers are members of
`_PROGRESS_STREAM_EVENT_TYPES`, so the fix built on the premise closed the wait
window at the instant it should open: **three of the six sessions the fix
existed to save would still have died**, now with an abort message additionally
claiming "nobody outstanding".

The check that would have caught it, run after the fact, took one command:

```bash
for s in 3d5fe1ec f26add11 e2f0ef97 c56a973e c5201301 aae80d84; do
  echo "=== $s ==="
  grep -nE "tool result\) received|\[guardrail\]|stream event:" "$s"*.stderr | tail -6
done
```

A full multi-agent review caught it instead.

## Guidance

### 1. A claim about N cases needs evidence from N cases

"All six share X" is a claim with six obligations. One verbatim trace discharges
one of them. The remaining five were satisfied by a table that listed the ids
next to the conclusion — which looks like evidence and is actually restatement.

The tell is cheap to spot: **count the citations against the cardinality of the
claim.** If the claim says six and the artifact shows one, the claim is
`n=1, generalized`, and it should be written that way until the other five are
checked. Nothing is wrong with acting on `n=1` — it is often the right call. It
is wrong to *record* it as `n=6, unanime`, because every downstream reader then
treats a hypothesis as a measurement.

### 2. A premise hardens as it travels

The generalization moved ticket -> plan -> code comment, gaining authority at
each hop and losing its provenance. By the time it reached
`guardrails.py`, it read as an established property of the SDK:

> the SDK does not resume generation while a tool result is still outstanding

That sentence is falsified by 67 production events observed inside 177 real
dispatch-to-result windows. It was never measured; it was inferred from the one
trace and then written in the imperative voice that code comments use.

**Cite provenance inside the artifact that carries the claim.** "Measured over
177 pairs on 2026-09-03" and "inferred from one trace in the ticket body" are
both acceptable; they are simply not the same sentence, and the reader four
months from now cannot reconstruct which one you meant.

### 3. Prefer the probe that can fail

Two probes were run while investigating this. The first returned zero and looked
like a clean negative. It was vacuous: the `[tool]` marker it keyed on does not
appear in most logs, so the probe could not have returned anything else. The
second carried a **positive control** — count the dispatch-to-result pairs the
probe claims to be scanning — and reported `177 pairs, 67 production events`.

A probe with no positive control cannot distinguish "the thing is absent" from
"my regex is wrong". Run both controls in the same invocation, and print the
control, not just the answer.

### 4. This generalizes past logs

The same shape appears wherever a small named set gets a shared property:
"all four callers already validate this", "every consumer parses it opaquely",
"none of these tests touch the network". Each is a claim with a cardinality, and
each is usually asserted from one or two examples the author actually opened.

## See also

- `docs/solutions/tooling-decisions/liveness-signals-are-not-all-production-signals.md`
  — the specific technical learning this incident sharpened (§2b, §2c, §2d).
- The plan's "Correction de la prémisse" section in
  `docs/plans/2026-09-02-001-fix-145-idle-waiting-is-not-idling-plan.md`, which
  keeps both what was believed and what was measured, deliberately. The gap
  between them is the most useful artifact the ticket produced.
