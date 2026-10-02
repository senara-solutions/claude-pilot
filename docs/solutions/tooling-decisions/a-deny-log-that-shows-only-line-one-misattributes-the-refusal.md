---
title: "A deny log that shows only line 1 misattributes the refusal — make the refused segment explicit"
date: 2026-10-02
last_updated: 2026-10-02
module: claude_pilot.ui, claude_pilot.permissions
component: permission-classifier
problem_type: design_decision
category: tooling-decisions
severity: high
tags: [permissions, policy, observability, logging, policy-deny, multi-line-command, segment-wise, claude-pilot-262, claude-pilot-256, claude-pilot-151, mika-1097, misleading-signal, dispatch-lib]
applies_when: "a log line summarises a decision taken over a compound/multi-line input, and a human or a downstream parser will act on that line"
---

# A deny log that shows only line 1 misattributes the refusal

## Context

claude-pilot's `can_use_tool` gate refuses a Bash call and logs it as
`[policy:deny] Bash: <detail> [rule_id] (terminal|non-terminal)`. The `detail`
came from `_summarize_input`: `str(command)[:200]`, scrubbed — **raw newlines
kept**. For a single-line command that is fine. For a multi-line script it is
actively misleading, and the decision it summarises is taken *per segment*
(`_bash_allow_is_chain_safe`, per-segment tier3, `_destination_veto_reason`),
not on line 1.

Three failure modes compounded:

1. **`grep '[policy:deny]'` returned only the first physical line.** The lines
   2..n of the command rode into the log without a tag and without a timestamp,
   so any log scrape — including the dispatch-lib reader (mika#1097) — saw only
   `cd <WT>`.
2. **The `rule_id` and the `(terminal)/(non-terminal)` suffix (cpp#151) landed
   on the LAST physical line**, i.e. not on the line the grep matched, or were
   lost entirely to the 200-char truncation.
3. **No field named the refused segment.** A reader seeing `cd <WT>` had no way
   to know the refusal was actually about `make build` three lines down.

This is exactly how cpp#256 nearly happened. 57ad9d76 logged "15 refusals of
`cd <WT> && …`". Read literally, that looks like `cd`-into-worktree is being
refused — a security boundary worth extending. A verbatim replay from the
transcript showed the truth: those were `cd <WT>⏎…` scripts whose faulty line
was `make` / `cargo run` / `env` / `sed -i`, elsewhere in the compound. Prime,
2026-10-01: *"We nearly signed a security boundary on a misleading log."*

## The lesson

**A log line that summarises a per-segment decision must name the segment it
decided on. If it can only show the start of the input, it will be read as a
claim about the start — and a decision taken on line 3 will be attributed to
line 1.** The fix is not to make the decision smarter; the decision was
correct. The fix is to make the *observable* carry what the decision already
knew: which segment, and why.

Three concrete obligations fall out of it, and they generalise beyond this gate:

- **One decision = one physical line.** If a downstream `grep` is a known
  consumer, newlines in the payload must be escaped (here `\n` → `⏎`) so the
  command, its cause, its rule_id and its lethality travel together. A field
  that can be pushed onto an untagged continuation line is a field that will be
  dropped.
- **Truncation must prefer the load-bearing fragment.** A fixed `[:200]` window
  anchored at the *start* drops the one segment the line exists to explain. When
  the faulty segment falls outside the window, show it preferentially.
- **Keep the full input recoverable without heuristics.** A short
  `sha256(full_command)[:12]` on the line rejoins it to the transcript
  (`~/.mika/data/pilot-transcripts/<id>.jsonl`) deterministically — no
  timestamp-window guessing.

## How it was done (without touching the decision)

The decision path is untouched. A **separate, read-only diagnostic** re-walks
the segments purely for the log:

- `_segment_refusal_cause(policy, seg, cwd)` classifies one segment in the
  decision's own priority order — containment veto (`dest-veto:`) → `tier3` →
  (tier1-safe or clean policy-allow = not an offender) → `default-deny` (no
  rule) / `chain-unsafe` (an explicit deny rule). It mirrors the two "continue"
  cases of `_bash_allow_is_chain_safe`'s loop, so a segment it calls an offender
  is one that path would have refused.
- `_diagnose_refused_bash` returns the FIRST offender + a count of how many.
- `_bash_deny_log_fields` packages the display detail (segment-preferring on
  truncation), the diagnostic, and the hash — wrapped in a `try/except` that
  degrades to the plain detail, so **the observability code can never crash or
  change the refusal**.
- `ui.log_policy_deny` escapes newlines and appends `segment="…" cause=…
  sha256:…` *after* the detail and *before* the unchanged `[rule_id]` +
  lethality suffix.

### Why the diagnostic must not be the decision

The byte-identical invariant (cpp#262 AC5) is the whole point: the bug was that
a *log* nearly moved a *policy*. If the diagnostic re-walk fed back into
admission or lethality, a bug in the explainer could silently change what gets
refused or what kills the run. So the diagnostic is pure read, the four deny
sites change only their `log_policy_deny(...)` arguments, and a test snapshots
`is_tier1_auto_approve` / `is_tier3_dangerous` /
`is_tier3_dangerous_for_lethality` / `_denial_is_terminal` across a broad sample
before and after exercising the diagnostic — they do not move.

## Downstream consumers

The `[policy:deny] <tool>: <one line>` prefix and the one-physical-line shape
are **preserved**; the new fields append. In-repo, no parser reads the deny
line's internals (`measure-idle-lethality.sh` greps the `[guardrail]`/`[done]`
tags only). Cross-repo, `dispatch-lib.sh` (mika#1097) re-reads `[policy:deny]`
from the `.stderr` and Signal S anchors `^dispatch-lib: ` — the prefix and shape
it expects are intact, so the escaping and the appended fields do not break its
read. **That out-of-repo file must still be verified by MPC** when this lands.

## See also

- `docs/solutions/tooling-decisions/a-mid-turn-stall-is-not-an-idle-name-it-distinctly.md`
  (same family: a signal that lied about what it measured).
- cpp#256 (the admission extension this log nearly justified), cpp#259 (the
  turn-counter that lied), cpp#151 (the lethality suffix, preserved here).
