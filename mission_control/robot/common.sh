# Shared by the robot scripts: run on the laptop, act on the NUC over ssh.
# NUC: the ssh target (default vector@vector-nuc; on the robot wire: NUC=vector@192.168.123.5).
set -euo pipefail

NUC="${NUC:-vector@vector-nuc}"
SITE="${SITE:-stanley}"
ENV_SH='source ~/orchestrator_ws/env.sh'

# nuc: run the bash script on stdin on the NUC, with the given arguments
nuc() { ssh -o ConnectTimeout=5 "$NUC" bash -s -- "$@"; }

# executor_state: the executor's latched state (idle, running, success, failure, rejected, ...), or "down"
executor_state() {
    nuc <<'REMOTE'
source ~/orchestrator_ws/env.sh
out=$(timeout 5 ros2 topic echo --once --qos-durability transient_local --qos-reliability reliable /orchestrator/status std_msgs/msg/String 2>/dev/null || true)
state=$(printf '%s' "$out" | grep -o '"state": *"[a-z_]*"' | head -1 | sed 's/.*"\([a-z_]*\)"$/\1/')
echo "${state:-down}"
REMOTE
}
