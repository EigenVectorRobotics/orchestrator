from ..lib.walk import APPROACH_BACK_M, WALK_MIN_M       # limits() carries them: the prompt says how a near stance is reached

PHASES = {                              # the decisions each phase may end with
    'diagnose':     ('reposition', 'switch_arm', 'offset_retry', 'retry', 'ask_help'),    # no D step ends the task: ask_help
    'stance_check': ('continue', 'reposition', 'abort'),       # button_in_view chooses the arm
}
GOING_ON    = {'reposition', 'switch_arm', 'offset_retry', 'retry', 'continue'}   # the tree goes on
ANSWERED_BY = 'model'
FIELDS      = {                         # beside decision and reason
    'dx': 'number', 'dy': 'number', 'dyaw': 'number',        # reposition: the stance in the base-goal frame
    'use_left_arm': 'boolean',                               # switch_arm
    'ox': 'number', 'oy': 'number', 'oz': 'number',          # offset_retry: the whole press offset
}

# The limits the executor enforces; each call carries them, so the prompt states what will be checked
REPOSITION_LIMITS_M  = {'forward': 0.15, 'back': 0.60, 'side': 0.25}   # from the base goal; it stands near the panel
MAX_PRESS_OFFSET_M   = 0.05             # per axis: an offset_retry corrects millimetres; a larger miss needs another decision
STANCE_CHANGED_M     = 0.03             # the press target moved this much in pelvis since a passed check_reach: the stance changed
STANCE_TOLERANCE_M   = 0.06             # farther than this from the target stance, the executor walks there again by itself
STANCE_TOLERANCE_RAD = 0.15             # or turned more than this from it (8.6 deg)


def limits() -> dict:
    """The limits as a call carries them"""
    return {
        'reposition_m':         dict(REPOSITION_LIMITS_M),
        'press_offset_max_m':   MAX_PRESS_OFFSET_M,
        'stance_changed_m':     STANCE_CHANGED_M,
        'stance_tolerance_m':   STANCE_TOLERANCE_M,
        'stance_tolerance_rad': STANCE_TOLERANCE_RAD,
        'walk_min_m':           WALK_MIN_M,
        'approach_back_m':      APPROACH_BACK_M,
    }
