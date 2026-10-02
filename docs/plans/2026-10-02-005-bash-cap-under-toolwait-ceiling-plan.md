# Keep the Bash max-timeout cap under claude-pilot's tool-wait ceiling (cpp#276 post-merge follow-up)

cpp#276 set `BASH_MAX_TIMEOUT_MS=1_800_000` (30 min). MPC's post-merge gate (served 2317c69) flagged that this **equals** claude-pilot's own `toolWaitCeilingMs` (1800 s): a Bash command allowed to run exactly to the 30-min cap would race the tool-wait guardrail. Lower the cap strictly below the ceiling.

## Change
`src/claude_pilot/agent.py` — `_CLI_FORCE_FOREGROUND_ENV["BASH_MAX_TIMEOUT_MS"]`: `"1800000"` → `"1680000"` (28 min). Comment updated with the ceiling rationale. `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` unchanged. `BASH_DEFAULT_TIMEOUT_MS` intentionally NOT set (short commands keep the CLI's 2-min default) — confirmed deliberate.

## Acceptance criteria
- [x] AC1. `BASH_MAX_TIMEOUT_MS == "1680000"` (28 min) in the env the pilot passes to `ClaudeAgentOptions`.
- [x] AC2. The value is strictly `< 1800 * 1000` (under the toolWaitCeilingMs), asserted in the env-presence test.
- [x] AC3. `BASH_DEFAULT_TIMEOUT_MS` absent (intended) — the 2-min CLI default stays for short commands.
- [x] AC4. `tier1.py` + `permissions.py` 0-line diff; non-admission; no classifier change. Full suite green; ruff/mypy/verify-pipeline green.

## Fire-Disposition
Non-admission config tweak (a single env value + its test + doc note). Push auto; PR gated MPC — created then immediately `gh pr ready <N> --undo` to hold it in draft (the engine auto-merges a born-ready QA-green PR otherwise). SSC does not merge.
