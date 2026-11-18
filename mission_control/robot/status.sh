#!/usr/bin/env bash
# What runs now: the tmux sessions, the executor's state, the agent bridge link and the last log lines.
source "$(dirname "$0")/common.sh"

echo "executor: $(executor_state)"
nuc <<'REMOTE'
echo "tmux: $(tmux ls 2>/dev/null | cut -d: -f1 | tr '\n' ' ')"
grep -E 'connected to the agent bridge|agent bridge link dropped' ~/orchestrator.log 2>/dev/null | tail -1 | sed 's/^/bridge: /'
echo "--- last lines of ~/orchestrator.log"
tail -15 ~/orchestrator.log 2>/dev/null
REMOTE
