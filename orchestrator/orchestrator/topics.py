"""The orchestrator's own topic names, in one place. No ROS here: the task desk and its tests use them without ROS.
The QoS profiles are in qos.py"""

TASK_TOPIC   = '/orchestrator/task'                 # a task sentence in, from a session, a script or Mission Control
TASK_OPTIONS = ('blind', 'discovery')                # the options of a task request; TaskDesk.open takes them by name
STATUS_TOPIC = '/orchestrator/status'               # the executor's state, latched
CANCEL_TOPIC = '/orchestrator/cancel'               # cancel the open task, and halt its running step
NOTE_TOPIC   = '/orchestrator/note'                 # an operator's note for the task agent: JSON run_id, text
MAX_NOTE     = 500                                  # characters of a note; TaskDesk.note cuts a longer one
STOP_STEP_TOPIC = '/orchestrator/stop_step'         # halt one running step, the task stays open: JSON run_id, step, reason
TRACE_TOPIC  = '/orchestrator/trace'                # every executor step, as JSON
LOG_TOPIC    = '/orchestrator/log'                  # the same steps, as lines for the Foxglove Log panel

OPEN_TOPIC    = '/orchestrator/task/open'           # the open task, latched (task_desk.py)
COMMAND_TOPIC = '/orchestrator/task/command'        # a command from the task agent
REPLY_TOPIC   = '/orchestrator/task/reply'          # one reply for each command

STEP_REQUEST_TOPIC  = '/orchestrator/step_request'          # the step gate asks a person (step_gate.py)
STEP_DECISION_TOPIC = '/orchestrator/step_decision'         # the person's decision
STEP_REPLY_TOPIC    = '/orchestrator/step_decision/reply'   # whether the gate took it

RECORDER_TOPIC = '/orchestrator/recorder/status'    # the recorder's state, latched (run_recorder.py)

LIVE_SKILLS_TOPIC = '/skills/live'                 # the skills whose interface is in the ROS graph, latched (registry_node)
OUTLINE_TOPIC = '/track_3d/mask_outlines'           # track_anything: the mask outlines of a frame, only while subscribed
