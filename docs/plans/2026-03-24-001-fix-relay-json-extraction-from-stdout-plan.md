---
title: "fix: relay parser should extract JSON from stdout instead of requiring bare JSON"
type: fix
status: completed
date: 2026-03-24
---

# fix: relay parser should extract JSON from stdout instead of requiring bare JSON

## Problem

When mika-dev responds to a permission request with `{"action": "allow"}`, claude-pilot's relay parser in `src/transport.ts:49` rejects it as invalid JSON:

```
[retry] Invalid JSON from command: {"action": "allow"}
```

`invokeCommand()` does `JSON.parse(stdout.trim())`, expecting stdout to contain **only** the JSON object. But `mika ask` outputs the full assistant response, which may include:
- Preamble text (e.g., `"Sure, here's the response:"`)
- Markdown code fences wrapping the JSON
- Trailing thinking/reflection text
- Multiple lines with JSON embedded

This causes repeated retries → fallback to `denied` (non-interactive auto-deny) → all tool calls blocked despite mika-dev correctly approving them. The problem is especially acute with non-Anthropic models (mika-dev currently runs on DeepSeek) that reliably wrap output in extra text.

## Proposed Fix

Replace `JSON.parse(stdout.trim())` with a JSON extraction function that finds and parses the first valid JSON object from stdout.

### `src/transport.ts`

Replace the naive `JSON.parse(stdout.trim())` call (line 49) with a helper that:

1. Finds the first `{` in stdout
2. Attempts `JSON.parse` from that position using incremental bracket-matching to find the matching `}`
3. Falls back to the `indexOf`/`lastIndexOf` approach if bracket matching fails
4. Keeps Zod validation (`PilotResponseSchema.safeParse`) as the final gate — extraction is best-effort, schema validation is strict

```typescript
// src/transport.ts — new helper
function extractJson(raw: string): unknown {
  // Fast path: entire string is valid JSON
  const trimmed = raw.trim();
  try {
    return JSON.parse(trimmed);
  } catch {
    // Continue to extraction
  }

  // Find first '{' and try parsing from there
  const start = raw.indexOf('{');
  if (start === -1) {
    throw new Error("no JSON object found in output");
  }

  // Try from first '{' to each subsequent '}' (shortest valid object first)
  let depth = 0;
  let inString = false;
  let escape = false;
  for (let i = start; i < raw.length; i++) {
    const ch = raw[i];
    if (escape) {
      escape = false;
      continue;
    }
    if (ch === '\\' && inString) {
      escape = true;
      continue;
    }
    if (ch === '"') {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (ch === '{') depth++;
    if (ch === '}') {
      depth--;
      if (depth === 0) {
        try {
          return JSON.parse(raw.slice(start, i + 1));
        } catch {
          // Malformed segment — keep scanning
          break;
        }
      }
    }
  }

  // Last resort: first '{' to last '}'
  const end = raw.lastIndexOf('}');
  if (end > start) {
    return JSON.parse(raw.slice(start, end + 1));
  }

  throw new Error("no JSON object found in output");
}
```

Then replace line 49:

```typescript
// Before:
const parsed = JSON.parse(stdout.trim());

// After:
const parsed = extractJson(stdout);
```

### Why bracket-matching instead of simple `indexOf`/`lastIndexOf`

The issue's proposed fix (`indexOf('{')` to `lastIndexOf('}')`) works for simple cases but breaks when:
- The response JSON contains nested objects (e.g., `{"action": "answer", "answers": {"q": "a"}}`)
- Trailing text contains a stray `}` character

Bracket-matching finds the first complete, balanced JSON object — more precise and handles nested responses correctly. The `lastIndexOf` fallback is kept as a safety net.

### Logging

Add a verbose log when extraction is used (not the fast path), so operators know JSON was extracted from noisy output:

```typescript
// In the extractJson call site in invokeCommand():
if (verbose && stdout.trim() !== JSON.stringify(parsed)) {
  logVerbose(`extracted JSON from noisy stdout (${stdout.length} bytes)`);
}
```

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Clean JSON (current happy path) | Fast path succeeds, no change in behavior |
| JSON with preamble text | Bracket-matching extracts first `{...}` object |
| JSON in markdown code fence | Works — finds `{` inside fences |
| Nested JSON (`{"action":"answer","answers":{...}}`) | Bracket-matching handles depth correctly |
| No JSON in output at all | Throws `"no JSON object found in output"` → retry flow |
| Multiple JSON objects in output | Extracts first complete one (correct — it's the response) |
| Truncated JSON | Bracket-matching fails, lastIndexOf fallback attempted, then throws |
| Stray `}` in trailing text | Bracket-matching ignores it (depth tracking), lastIndexOf might include it but Zod catches invalid shape |

## Security

- No new attack surface: the extracted JSON still passes through `PilotResponseSchema.safeParse()` (Zod discriminated union validation)
- `JSON.parse` itself is safe against prototype pollution in modern Node.js
- Input is stdout from a command claude-pilot already controls (execFile, scrubbed env)

## Acceptance Criteria

- [x] `extractJson()` helper added to `src/transport.ts` with bracket-matching extraction
- [x] `invokeCommand()` uses `extractJson(stdout)` instead of `JSON.parse(stdout.trim())`
- [x] Verbose log emitted when JSON is extracted from noisy output
- [x] Fast path preserved: clean JSON strings parse without scanning
- [x] Zod validation unchanged — `PilotResponseSchema.safeParse()` remains the schema gate
- [x] `npx tsc --noEmit` passes
- [x] `npm run build` succeeds

## Files

| File | Change |
|------|--------|
| `src/transport.ts:47-54` | Replace `JSON.parse(stdout.trim())` with `extractJson(stdout)`, add `extractJson()` helper |

## Sources

- Issue: [#11](https://github.com/senara-solutions/claude-pilot/issues/11)
- Related brainstorm (diagnostics context): `docs/brainstorms/2026-03-18-fix-relay-and-monitoring-brainstorm.md`
