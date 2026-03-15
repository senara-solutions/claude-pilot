#!/bin/bash
# Mock agent that logs to a file and auto-allows
cat > /tmp/claude-pilot-test-event.json
echo '{"action": "allow"}'
