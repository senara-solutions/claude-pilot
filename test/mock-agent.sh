#!/bin/bash
# Mock agent that auto-allows everything
# Reads stdin (PilotEvent JSON) and outputs PilotResponse JSON
cat > /dev/null  # consume stdin
echo '{"action": "allow"}'
