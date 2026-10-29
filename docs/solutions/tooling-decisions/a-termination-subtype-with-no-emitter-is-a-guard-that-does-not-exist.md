---
title: "A termination subtype with no emitter is a guard that does not exist"
date: 2026-10-29
module: claude_pilot.agent
component: sdk-guardrails
problem_type: logic_error
category: tooling-decisions
severity: high
tags: [guardrails, max-budget-usd, sdk-options, silent-noop, claude-pilot-197, mika-2496]
applies_when: "a config field names a limit and the code handles its termination subtype, but nothing passes the limit on"
---

# A termination subtype with no emitter is a guard that does not exist

## What happened

`maxBudgetUsd` was parsed, validated, defaulted and displayed in the startup
line. `error_max_budget_usd` was in `SDK_TERMINATION_SUBTYPES` and mapped to
`status=terminated`. Every piece around the dollar cap existed except the one
that hands it to the CLI: `_sdk_guardrail_kwargs` ended in `pass`, under a
comment claiming application-level guardrails still applied. None did.

## Why it stayed hidden

Each layer looked finished on its own. The CLI flag was accepted, the subtype
was handled, the startup line printed `maxBudget=$40`. Only the edge between
the config and `ClaudeAgentOptions` was missing, and no test crossed it.

## The fix and how it is proven

Pass `max_budget_usd` when the cap is > 0. The test drives `run_agent`,
captures the options given to `ClaudeSDKClient`, and builds the CLI command
from them: the proof is `--max-budget-usd` on the command line, not a helper's
return value.

## The premise that had to hold

The CLI's cap reads its own cost ledger, an API-equivalent estimate computed
under subscription auth too. The cap bounds notional spend, not a bill.
