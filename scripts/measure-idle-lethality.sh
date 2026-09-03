#!/usr/bin/env bash
# measure-idle-lethality.sh — AC6 of cpp#145.
#
# WHY a RATE and not a count. The acceptance criterion refuses a raw count on
# purpose: "sessions killed by idle_timeout" falls when the loop simply runs
# less, which is not an improvement. What the fix must move is the SHARE of
# terminated sessions that the watchdog killed. So this reports
# `killed / terminated`, and prints both terms so the reader can see whether a
# lower rate came from fewer kills or from more sessions.
#
# WHY this marker and not `[tool]`. Tool calls are counted with
# `user message (tool result) received`, validated at 143 occurrences across
# the eight sessions of the founding measurement. The `[tool]` marker counts
# zero on sessions that made thirty-three calls, and the false conclusion it
# produced ("policy denials are freezing sessions") was published and then
# withdrawn. Do not switch markers without re-validating against a session
# whose real count is known.
#
# Usage:
#   scripts/measure-idle-lethality.sh [log-dir] [days]
#
#   log-dir  directory of claude-pilot stderr logs (default /var/log/claude-pilot)
#   days     only consider logs modified within this many days (default 7)

set -euo pipefail

LOG_DIR="${1:-/var/log/claude-pilot}"
DAYS="${2:-7}"

if [ ! -d "$LOG_DIR" ]; then
  echo "error: log directory not found: $LOG_DIR" >&2
  exit 2
fi

mapfile -t LOGS < <(find "$LOG_DIR" -maxdepth 1 -name '*.stderr' -mtime "-${DAYS}" -print | sort)

if [ "${#LOGS[@]}" -eq 0 ]; then
  echo "error: no *.stderr logs in $LOG_DIR within ${DAYS}d — nothing to measure" >&2
  exit 3
fi

terminated=0
idle_killed=0
awaiting_tool=0
awaiting_model=0
rate_limited=0
tool_calls_killed=0
tool_calls_survived=0

for log in "${LOGS[@]}"; do
  # A session counts as TERMINATED once it has reached any end state: a
  # guardrail abort, or the SDK's own [done]/result line. A session still
  # running is excluded from both terms — including it would depress the rate
  # for a reason that has nothing to do with the fix.
  guardrail_line=$(grep -m1 '\[guardrail\]' "$log" 2>/dev/null || true)
  done_line=$(grep -m1 '\[done\]' "$log" 2>/dev/null || true)
  [ -z "$guardrail_line" ] && [ -z "$done_line" ] && continue

  terminated=$((terminated + 1))
  calls=$(grep -c 'user message (tool result) received' "$log" 2>/dev/null || true)
  calls=${calls:-0}

  case "$guardrail_line" in
    *idle_timeout*)    idle_killed=$((idle_killed + 1));    tool_calls_killed=$((tool_calls_killed + calls)) ;;
    *awaiting_tool*)   awaiting_tool=$((awaiting_tool + 1)); tool_calls_killed=$((tool_calls_killed + calls)) ;;
    *awaiting_model*)  awaiting_model=$((awaiting_model + 1)); tool_calls_killed=$((tool_calls_killed + calls)) ;;
    *rate_limited*)    rate_limited=$((rate_limited + 1));  tool_calls_survived=$((tool_calls_survived + calls)) ;;
    *)                 tool_calls_survived=$((tool_calls_survived + calls)) ;;
  esac
done

if [ "$terminated" -eq 0 ]; then
  echo "error: no terminated sessions in the window — the rate is undefined, not zero" >&2
  exit 3
fi

pct() { awk -v n="$1" -v d="$2" 'BEGIN { printf "%.1f%%", (d ? 100*n/d : 0) }'; }
avg() { awk -v n="$1" -v d="$2" 'BEGIN { printf "%.1f", (d ? n/d : 0) }'; }

survived=$((terminated - idle_killed - awaiting_tool - awaiting_model))

echo "window:              ${DAYS}d of ${LOG_DIR} (${#LOGS[@]} logs)"
echo "terminated sessions: ${terminated}"
echo
echo "idle_timeout:        ${idle_killed}/${terminated}  ($(pct "$idle_killed" "$terminated"))   <- AC6 headline rate"
echo "awaiting_tool:       ${awaiting_tool}/${terminated}  ($(pct "$awaiting_tool" "$terminated"))"
echo "awaiting_model:      ${awaiting_model}/${terminated}  ($(pct "$awaiting_model" "$terminated"))"
echo "rate_limited:        ${rate_limited}/${terminated}  ($(pct "$rate_limited" "$terminated"))"
echo
echo "avg tool calls, watchdog-killed sessions: $(avg "$tool_calls_killed" "$((idle_killed + awaiting_tool + awaiting_model))")"
echo "avg tool calls, surviving sessions:       $(avg "$tool_calls_survived" "$survived")"
echo
echo "baseline before cpp#145 (2026-09-01): idle_timeout 5/10 (50.0%);"
echo "killed sessions averaged 21 tool calls against 13.8 for survivors — the"
echo "killed ones were the ones doing the most work."
