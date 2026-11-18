"""What the executor and the agent bridge agree on, in one place: the phases and their decisions, the decision fields
and their schema, the limits the executor enforces, and the link's framing.

The executor (agent.py, agent_push.py, stance.py) and the bridge on the laptop (mission_control/agent_bridge.py and
agent_prompts.py) both import this module, so a decision, a field or a limit is written once. It imports only the
standard library: the bridge loads it from its checkout without ROS.

Each call also carries PROTOCOL and the limits the executor enforces (limits()). The bridge refuses a call of another
protocol, so a laptop and a NUC on different checkouts fail the call with a plain reason instead of drifting apart;
the prompt states the limits of the call, so a limit changed on the NUC alone still reaches the model.
"""

import struct


# ========== The link ==========

PROTOCOL   = 1                          # raise it when a call or an answer changes shape; both ends must match
HEADER     = struct.Struct('!II')       # the JSON length, then the length of the binary after it
MAX_JSON   = 16_000_000
MAX_BINARY = 64_000_000
PING_S     = 5.0                        # both ends ping this often


# ========== Phases and decisions ==========

PHASE_SKILL = {                         # the agent skill each phase belongs to
    'judge':        'press_judge',
    'diagnose':     'manipulation_recovery',
    'stance_check': 'manipulation_recovery',
    'confirm':      'operator_confirm',
}
PHASE_DECISIONS = {                     # the decisions each phase may end with
    'diagnose':     ('reposition', 'switch_arm', 'offset_retry', 'retry', 'ask_help'),    # no D step ends the task: ask_help
    'stance_check': ('continue', 'reposition', 'abort'),       # button_in_view chooses the arm
    'judge':        ('pressed', 'not_pressed'),
    'confirm':      ('confirmed', 'not_confirmed'),
}
GOING_ON      = {'reposition', 'switch_arm', 'offset_retry', 'retry', 'continue', 'pressed', 'confirmed'}   # the tree goes on
MODEL_PHASES  = ('judge', 'diagnose', 'stance_check')        # the bridge asks the model; confirm goes to the operator
STANCE_PHASES = ('diagnose', 'stance_check')                 # the call carries the stance
PRESS_PHASES  = ('judge', 'diagnose')                        # the call carries the last press


# ========== Decision fields ==========

FIELD_TYPES = {                         # beside decision and reason
    'dx': 'number', 'dy': 'number', 'dyaw': 'number',        # reposition: the stance in the base-goal frame
    'use_left_arm': 'boolean',                               # switch_arm
    'ox': 'number', 'oy': 'number', 'oz': 'number',          # offset_retry: the whole press offset
}
SKILL_FIELDS = {
    'press_judge':           (),
    'manipulation_recovery': ('dx', 'dy', 'dyaw', 'use_left_arm', 'ox', 'oy', 'oz'),
    'operator_confirm':      (),
}
DECISION_FIELDS = ('decision', 'reason', *FIELD_TYPES)


def schema(skill: str, decisions) -> dict:
    """The JSON schema of one answer: one of decisions, a reason, and the skill's fields"""
    properties = {'decision': {'type': 'string', 'enum': list(decisions)}, 'reason': {'type': 'string'}}
    properties.update({name: {'type': FIELD_TYPES[name]} for name in SKILL_FIELDS[skill]})

    return {'type': 'object', 'properties': properties, 'required': ['decision', 'reason']}


# ========== Limits the executor enforces ==========

REPOSITION_LIMITS_M  = {'forward': 0.15, 'back': 0.60, 'side': 0.25}   # from the base goal; it stands near the panel
MAX_PRESS_OFFSET_M   = 0.05             # per axis: an offset_retry corrects millimetres; a larger miss needs another decision
STANCE_CHANGED_M     = 0.03             # the press target moved this much in pelvis since a passed check_reach: the stance changed
STANCE_TOLERANCE_M   = 0.06             # farther than this from the target stance, the executor walks there again by itself
STANCE_TOLERANCE_RAD = 0.15             # or turned more than this from it (8.6 deg)
WALK_MIN_M           = 0.10             # navigate_to ends a shorter walk without a step
APPROACH_BACK_M      = 0.15             # so a stance closer than WALK_MIN_M walks out this far behind it first, then onto it


def limits() -> dict:
    """The limits as a call carries them, so the prompt states what the executor will check"""
    return {
        'reposition_m':        dict(REPOSITION_LIMITS_M),
        'press_offset_max_m':  MAX_PRESS_OFFSET_M,
        'stance_changed_m':    STANCE_CHANGED_M,
        'stance_tolerance_m':  STANCE_TOLERANCE_M,
        'stance_tolerance_rad': STANCE_TOLERANCE_RAD,
        'walk_min_m':          WALK_MIN_M,
        'approach_back_m':     APPROACH_BACK_M,
    }
