"""The orchestrator's topic names and QoS profiles, in one place.

The nodes (planner, executor, recorder, registry clients) and the command-line tools import them from here, so a
name or a profile is changed once. This module imports only rclpy's QoS types: the recorder and the tools do not
pull in the executor's modules to learn a topic name.
"""

from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy


# ========== Topics ==========

TASK_TOPIC             = '/orchestrator/task'               # a sentence (or {"task", "blind"}) for the planner
PLAN_TOPIC             = '/orchestrator/plan'               # the planner's last plan, latched
TREE_TOPIC             = '/orchestrator/tree'               # a tree for the executor to run
TREE_BLIND_TOPIC       = '/orchestrator/tree_blind'         # a tree that must run with no cloud
CANCEL_TOPIC           = '/orchestrator/cancel'             # Empty: halt the running tree
STATUS_TOPIC           = '/orchestrator/status'             # the executor's state, latched
TRACE_TOPIC            = '/orchestrator/trace'              # every event, as JSON
LOG_TOPIC              = '/orchestrator/log'                # the same events as rcl_interfaces Log, for Foxglove
RECORDER_STATUS_TOPIC  = '/orchestrator/recorder/status'    # the recorder's {ready, reason, free_gb, session}, latched
AGENT_REQUEST_TOPIC    = '/orchestrator/agent/request'      # each agent call, open and closed
AGENT_REPLY_TOPIC      = '/orchestrator/agent/reply'        # the executor's answer to each decision
PLANNER_REQUEST_TOPIC  = '/orchestrator/planner/request'    # the external planner backend's open call
PLANNER_ANSWER_TOPIC   = '/orchestrator/planner/answer'     # a session's answer to it (planner_cli)
SPEAK_TOPIC            = '/speak'                           # unitree_interface's text to speech
SKILLS_LIVE_TOPIC      = '/skills/live'                     # the registry's live skills, latched (skill_registry)


# ========== QoS ==========

COMMAND_QOS = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
LATCHED_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
SENSOR_QOS  = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
TRACE_QOS   = QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE)
