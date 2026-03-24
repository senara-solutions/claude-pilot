---
title: Relay parser rejects valid JSON wrapped in preamble text
category: integration-issues
date: 2026-03-24
tags: [transport, json-parsing, relay, deepseek, non-anthropic-models]
severity: high
modules: [transport.ts, permissions.ts]
issue: https://github.com/senara-solutions/claude-pilot/issues/11
pr: https://github.com/senara-solutions/claude-pilot/pull/12
---

# Relay parser rejects valid JSON wrapped in preamble text

## Problem

When mika-dev (running on DeepSeek) responds to permission requests, claude-pilot's relay parser rejects the response with `Invalid JSON from command: {"action": "allow"}`. Despite mika-dev correctly producing the JSON, the full stdout includes preamble text (e.g., "Sure, here's the response:"), markdown code fences, or trailing commentary. `JSON.parse(stdout.trim())` fails because it expects stdout to be *only* the JSON object.

This causes: retry with error feedback -> second failure -> fallback to auto-deny (non-interactive mode) -> all tool calls blocked despite correct approvals.

## Root Cause

`transport.ts:49` did `JSON.parse(stdout.trim())`, which is a strict contract: the entire stdout must be valid JSON. Non-Anthropic models (DeepSeek, etc.) reliably wrap output in conversational text, violating this contract.

## Solution

Replaced `JSON.parse(stdout.trim())` with `extractJson()` -- a bracket-matching JSON extractor:

```typescript
function extractJson(raw: string): { value: unknown; extracted: boolean } {
  // Fast path: entire string is valid JSON (zero overhead for clean output)
  const trimmed = raw.trim();
  try {
    return { value: JSON.parse(trimmed), extracted: false };
  } catch {
    // Continue to extraction
  }

  // Find first '{' and bracket-match to find the complete object
  const start = raw.indexOf("{");
  if (start === -1) throw new Error("no JSON object found in output");

  let depth = 0, inString = false, escape = false;
  for (let i = start; i < raw.length; i++) {
    const ch = raw[i];
    if (escape) { escape = false; continue; }
    if (ch === "\\" && inString) { escape = true; continue; }
    if (ch === '"') { inString = !inString; continue; }
    if (inString) continue;
    if (ch === "{") depth++;
    if (ch === "}") { depth--; if (depth === 0) {
      try { return { value: JSON.parse(raw.slice(start, i + 1)), extracted: true }; }
      catch { break; }
    }}
  }
  throw new Error("no JSON object found in output");
}
```

Key design decisions:
- **Fast path preserved** -- clean JSON parses with zero overhead (the common case)
- **Bracket-matching over indexOf/lastIndexOf** -- handles nested objects (`{"action":"answer","answers":{"q":"a"}}`) correctly; simple indexOf/lastIndexOf would grab stray `}` in trailing text
- **No lastIndexOf fallback** -- code review (4/4 reviewers) agreed the fallback was less precise and a correctness hazard. If bracket-matching fails, throw and let the retry mechanism handle it
- **Zod validation remains the gate** -- `extractJson` is best-effort extraction; `PilotResponseSchema.safeParse()` is the strict contract enforcement
- **Returns `{ value, extracted }` flag** -- avoids fragile `JSON.stringify(parsed)` comparison for verbose logging

## Prevention

- When integrating with external commands that use LLMs, always parse stdout defensively -- models wrap output in conversational text
- Postel's law at process boundaries: be liberal in what you accept (extraction), strict in what you validate (Zod schema)
- The external command (`mika ask`) should ideally output only JSON to stdout and everything else to stderr, but the parser should be resilient regardless
