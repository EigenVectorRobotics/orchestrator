"""Agent skills: an LLM with read-only tools, guided by a skill document, that decides how a tree recovers.

The tree calls an agent skill like any other leaf (AgentLeaf in skill_leaf). The agent reads the failure the tree
recorded, the history, files, TF, topics and camera images, runs its own checks in a sandbox, and ends with one call
to decide. It never publishes and never moves the robot: the tree acts on the decision.
"""

import json
import math
import time
import threading

from typing import Callable, Protocol
from pathlib import Path
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

from skill_registry import Contract

from .behavior_tree import Context, Status
from .knowledge import KnowledgeStore, Sighting
from .agent_sandbox import SandboxConfig, run_python
from . import stance


# The decisions each phase may end with, and the ones that let the tree go on
PHASE_DECISIONS = {
    'diagnose':     ('reposition', 'switch_arm', 'offset_retry', 'retry', 'ask_help', 'abort'),
    'stance_check': ('continue', 'reposition', 'abort'),       # button_in_view chooses the arm
    'judge':      ('pressed', 'not_pressed'),
    'confirm':    ('confirmed', 'not_confirmed'),
}
GOING_ON = {'reposition', 'switch_arm', 'offset_retry', 'retry', 'continue', 'pressed', 'confirmed'}
MAX_PRESS_OFFSET_M = 0.05               # an offset_retry corrects millimetres; a larger miss needs another decision
RULE_MISS_M        = (0.005, 0.050)     # a first miss this far from the goal in the button plane is corrected without the agent
RULE_SHORT_M       = 0.003              # a press that stopped this far short along the button normal, aimed well, goes deeper
RULE_DEPTH_MARGIN_M = 0.005             # pushed this much past the shortfall
STANCE_TOLERANCE_M   = 0.06             # farther than this from the base goal, the executor sends the base goal again
STANCE_TOLERANCE_RAD = 0.15             # or turned more than this from it (8.6 deg)
STANCE_RULE_MAX      = 2                # base goals re-sent per press before the agent decides
REPOSITION_LIMITS_M  = {'forward': 0.15, 'back': 0.60, 'side': 0.25}   # from the base goal; it stands near the panel
REACH_RULE_MAX       = 2                # reach moves per press before the agent decides
STANCE_CHANGED_M     = 0.03             # the press target moved this much in pelvis since a passed check_reach: the stance changed

MAX_READ_BYTES = 200_000
MAX_TOOL_TEXT  = 40_000
MAX_SPEECH     = 200                    # characters said at once
SPEECH_GAP_S   = 3.0                    # unitree_interface speaks each message at once, so space them out

STANCE_PHASES = ('diagnose', 'stance_check')
PRESS_PHASES  = ('judge', 'diagnose')       # a diagnosis after a judged miss needs the press's depth and residual
PRESS_FRAME   = 'foot_world'        # press_button's goal frame (move_to_pose planning_frame)

# What the robot says when an agent call opens and when it is decided, so people near it know what comes next
PHASE_SPEECH    = {
    'diagnose':     'The press did not work. Checking why.',
    'stance_check': 'I cannot see the button well from here. Checking where I stand.',
    'judge':        'Checking whether the button was pressed.',
}
DECISION_SPEECH = {
    'reposition':   'Moving to a better spot.',
    'switch_arm':   'Switching to my other arm.',
    'offset_retry': 'Correcting my aim and pressing again.',
    'retry':        'Trying the press again.',
    'abort':        'I cannot press this button. Stopping the task.',
    'continue':     'My stance looks fine. Looking at the button again from here.',
    'pressed':      'The button is pressed.',
    'not_pressed':  'I missed the button.',
}


class AgentError(RuntimeError):
    pass


@dataclass
class ToolOutput:
    text:       str
    image:      bytes | None = None
    media_type: str = 'image/jpeg'
    is_error:   bool = False
    final:      bool = False        # decide succeeded: the agent's turn is over


class RosAccess(Protocol):
    """Read-only access to the robot, from the executor's node"""

    def tf(self, target_frame: str, source_frame: str, timeout_s: float) -> dict: ...
    def topic_once(self, topic: str, timeout_s: float) -> dict: ...
    def topic_list(self) -> list[dict]: ...
    def image(self, source: str) -> tuple[bytes, str]: ...


class AgentBackend(Protocol):
    name: str

    def run(
        self,
        system: str,
        task: str,
        tools: list[dict],
        call_tool: Callable[[str, dict], ToolOutput],
        max_turns: int,
        should_stop: Callable[[], bool],
        on_event: Callable[..., None],
        skill: str = ''
    ): ...


@dataclass
class AgentConfig:
    skills_dir: str
    read_roots: list[str] = field(default_factory=list)     # read_file sees only these trees (and the scratch dir)
    max_turns:  int = 30
    sandbox:    SandboxConfig = field(default_factory=SandboxConfig)
    knowledge:  KnowledgeStore | None = None                 # note writes here; its facts and beliefs go in the prompt


# ========== Tools ==========

def tool_specs(phase: str) -> list[dict]:
    """The tools in a neutral form: name, description, JSON schema. decide lists only this phase's decisions"""
    specs = _tool_specs(phase)

    if phase in PRESS_PHASES:
        specs.insert(0, {
            'name':         'get_press',
            'description':  'The last press, as the executor kept it: the saved frames (baseline at the pre-grasp, press '
                            'at the fingertip\'s closest point to the button, release 0.5 s after the retreat began, and '
                            'their crops around the button), the phase times, the deepest point along the button normal '
                            'and the sideways distance there, and the arm.',
            'input_schema': {'type': 'object', 'properties': {}},
        })

    if phase in STANCE_PHASES:
        specs.insert(0, {
            'name':         'get_stance',
            'description':  'Where the robot stands, computed by the executor: its pose from the base goal, the press target '
                            'in pelvis and the arm, the last press on this button that was judged pressed (its target in '
                            'pelvis and its stance), and match: the reposition (dx, dy, dyaw from the base goal) that puts '
                            'the target where that press had it, and whether navigate_to would walk it.',
            'input_schema': {'type': 'object', 'properties': {}},
        })

    return specs


def _tool_specs(phase: str) -> list[dict]:
    return [
        {
            'name':         'get_failure',
            'description':  'The last failure of this tree: a failed action call (skill, inputs, message, outputs, '
                            'elapsed time, every feedback sample {t: seconds since the goal was sent, ...fields}), or a '
                            'failed check such as press_judge, whose checked_call is the action call it checked.',
            'input_schema': {'type': 'object', 'properties': {}},
        },
        {
            'name':         'get_history',
            'description':  'Every finished action call of this tree and your earlier decisions, oldest first, without feedback.',
            'input_schema': {'type': 'object', 'properties': {}},
        },
        {
            'name':         'read_file',
            'description':  'Read a text file, or list a directory, on the robot computer. Lines are numbered from 1.',
            'input_schema': {
                'type':       'object',
                'properties': {
                    'path':   {'type': 'string', 'description': 'absolute path'},
                    'offset': {'type': 'integer', 'description': 'first line, default 1'},
                    'limit':  {'type': 'integer', 'description': 'number of lines, default all that fit'},
                },
                'required':   ['path'],
            },
        },
        {
            'name':         'ros_read',
            'description':  'Read-only ROS. op tf: the latest transform of source_frame in target_frame. op topic_once: '
                            'the next message on a topic (not images or point clouds; use get_image). op topic_list: '
                            'the topics and their types.',
            'input_schema': {
                'type':       'object',
                'properties': {
                    'op':           {'type': 'string', 'enum': ['tf', 'topic_once', 'topic_list']},
                    'target_frame': {'type': 'string'},
                    'source_frame': {'type': 'string'},
                    'topic':        {'type': 'string'},
                    'timeout_s':    {'type': 'number', 'description': 'default 2'},
                },
                'required':   ['op'],
            },
        },
        {
            'name':         'get_image',
            'description':  'An image. camera: the latest color frame. overlay: track_anything\'s overlay with the '
                            'tracked mask and the tracker state. press_baseline, press_press, press_release: the frames '
                            'the last press kept (at the pre-grasp, at the deepest push, after the release).',
            'input_schema': {
                'type':       'object',
                'properties': {'source': {'type': 'string', 'enum': ['camera', 'overlay', 'press_baseline', 'press_press', 'press_release']}},
                'required':   ['source'],
            },
        },
        {
            'name':         'say',
            'description':  'Say one short sentence through the robot\'s speaker, so the people around know what you '
                            'are doing: what you check, what you found, what you will try. At most 200 characters, '
                            'and at least 3 s after the last one.',
            'input_schema': {
                'type':       'object',
                'properties': {'text': {'type': 'string'}},
                'required':   ['text'],
            },
        },
        {
            'name':         'run_python',
            'description':  'Run Python 3 with numpy, scipy, pinocchio and coal; returns stdout, stderr and the exit '
                            'code. No ROS, no network, no new processes; files can be written only in the scratch '
                            'directory, which is also on sys.path for your own helpers. Print what you need.',
            'input_schema': {
                'type':       'object',
                'properties': {'code': {'type': 'string'}},
                'required':   ['code'],
            },
        },
        {
            'name':         'note',
            'description':  'Keep one thing you found, for later calls and runs: an observation in the knowledge store. '
                            'It goes up to a belief and a fact only when other runs and sources confirm it, and only the '
                            'operator makes a fact. Note what is true of a place or a skill (e.g. "the call panel does not '
                            'light when pressed", or the press offset that worked), not this call\'s decision. With a key '
                            'and a value, a later note with the same about and key confirms or contradicts it.',
            'input_schema': {
                'type':       'object',
                'properties': {
                    'text':        {'type': 'string', 'description': 'one sentence'},
                    'claim':       {'type': 'string', 'description': 'the id of a claim in "What is known" that this confirms or contradicts'},
                    'contradicts': {'type': 'boolean', 'description': 'with claim: true if what you found disagrees with it'},
                    'about':       {'type': 'object', 'additionalProperties': {'type': 'string'},
                                    'description': 'what it is about, as kind: name, e.g. {"place": "freight_elevator_call_panel"} '
                                                   '(the name the blackboard uses), {"skill": "press_button"}, {"device": "thor"}, '
                                                   '{"object": "freight_elevator"}; leave out for something general'},
                    'key':         {'type': 'string', 'description': 'for a value, e.g. lights_when_pressed or press_offset'},
                    'value':       {'description': 'a number, a list of numbers, true/false, or a word'},
                    'evidence':    {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1,
                                    'description': 'what shows it, e.g. "record:3" (get_history index), "frame:press", "E-019"'},
                    'valid_for_s': {'type': 'number', 'description': 'for something that changes, how long it holds'},
                },
                'required':   ['text', 'evidence'],
            },
        },
        {
            'name':         'decide',
            'description':  'End your turn with your decision. Call it exactly once.',
            'input_schema': {
                'type':       'object',
                'properties': {
                    'decision':     {'type': 'string', 'enum': list(PHASE_DECISIONS[phase])},
                    'reason':       {'type': 'string', 'description': 'two to four sentences: evidence, computation, conclusion'},
                    'dx':           {'type': 'number', 'description': 'reposition: metres forward along the base goal yaw'},
                    'dy':           {'type': 'number', 'description': 'reposition: metres to the left of the base goal'},
                    'dyaw':         {'type': 'number', 'description': 'reposition: radians, counterclockwise'},
                    'use_left_arm': {'type': 'boolean', 'description': 'switch_arm: the arm for the next press'},
                    'ox':           {'type': 'number', 'description': 'offset_retry: the whole press offset to use, metres along the button normal (positive deeper)'},
                    'oy':           {'type': 'number', 'description': 'offset_retry: metres sideways in the button frame (positive left)'},
                    'oz':           {'type': 'number', 'description': 'offset_retry: metres up'},
                },
                'required':   ['decision', 'reason'],
            },
        },
    ]


WALK_MIN_M      = 0.10      # navigate_to ends a shorter walk without a step (E-002/E-016)
APPROACH_BACK_M = 0.15      # a short reposition walks out this far behind its goal, then onto it


def offset_goal(base: dict, dx: float, dy: float, dyaw: float) -> dict:
    """base moved by (dx, dy) in its own frame (x along its yaw) and turned by dyaw"""
    yaw = float(base.get('yaw', 0.0))

    goal = {
        'x':     float(base['x']) + math.cos(yaw) * dx - math.sin(yaw) * dy,
        'y':     float(base['y']) + math.sin(yaw) * dx + math.cos(yaw) * dy,
        'yaw':   math.atan2(math.sin(yaw + dyaw), math.cos(yaw + dyaw)),
        'frame': base.get('frame', 'map'),
    }

    if 'z' in base:                     # the same storey as the base goal
        goal['z'] = float(base['z'])

    return goal


def rule_offset(last_failure: dict | None, history: list[dict], residual: list | None,
                press_offset: list | None = None) -> dict | None:
    """offset_retry without the agent: the first diagnosis after a press that reached its goal and missed the button
    by RULE_MISS_M in its plane, or hit it in the plane but stopped over RULE_SHORT_M short along its normal. The new
    offset is the offset in force minus the residual, as the agent would set it, and RULE_DEPTH_MARGIN_M deeper if short.
    press_offset: the offset the press used, from its capture; without it, the last set_press_offset since the last
    press judged pressed (0 when there is none), which misses an offset set before that press"""
    failure = last_failure or {}
    pressed = [index for index, record in enumerate(history) if record.get('skill') == 'press_judge' and record.get('decision') == 'pressed']
    since   = history[pressed[-1] + 1:] if pressed else history

    if failure.get('skill') != 'press_judge' or not failure.get('checked_call') or residual is None or len(residual) != 3:
        return None

    if any(record.get('skill') == 'manipulation_recovery' and record.get('phase') == 'diagnose' for record in since):
        return None

    miss  = math.hypot(residual[1], residual[2])
    short = residual[0] < -RULE_SHORT_M

    if not (RULE_MISS_M[0] < miss < RULE_MISS_M[1] or (miss <= RULE_MISS_M[0] and short)):
        return None

    offsets   = [record.get('inputs', {}) for record in since if record.get('skill') == 'set_press_offset']
    in_force  = ([float(value) for value in press_offset] if press_offset is not None and len(press_offset) == 3 else
                 [float(offsets[-1].get(axis, 0.0)) if offsets else 0.0 for axis in 'xyz'])
    corrected = [round(force - miss_axis, 4) for force, miss_axis in zip(in_force, residual)]
    corrected[0] = round(corrected[0] + (RULE_DEPTH_MARGIN_M if short else 0.0), 4)

    if max(abs(value) for value in corrected) > MAX_PRESS_OFFSET_M:
        return None

    found = f'missed by {miss * 1000:.1f} mm in the button plane' if miss > RULE_MISS_M[0] else 'hit the button in its plane'
    depth = f' and stopped {-residual[0] * 1000:.1f} mm short (pushing {RULE_DEPTH_MARGIN_M * 1000:g} mm past it)' if short else ''

    return {
        'decision': 'offset_retry',
        'reason':   f'rule: the press reached its goal, {found}{depth} '
                    f'(residual {[round(value * 1000, 1) for value in residual]} mm); offset {[round(v * 1000, 1) for v in in_force]} '
                    f'-> {[round(v * 1000, 1) for v in corrected]} mm',
        'ox': corrected[0], 'oy': corrected[1], 'oz': corrected[2],
    }


def rule_stance(evidence: dict | None, history: list[dict]) -> dict | None:
    """reposition onto the target stance without the agent, when the robot stands off it by more than the stance tolerance.
    The target is the offset of the last reposition since the last press judged pressed, else the base goal. At most
    STANCE_RULE_MAX times; then the agent decides"""
    off     = (evidence or {}).get('stance_from_base_goal')
    pressed = [index for index, record in enumerate(history) if record.get('skill') == 'press_judge' and record.get('decision') == 'pressed']
    since   = history[pressed[-1] + 1:] if pressed else history
    sent    = [record for record in since if record.get('skill') == 'manipulation_recovery' and record.get('rule')
               and str(record.get('reason', '')).startswith('rule: the robot stands')]
    moved   = [record.get('offset') for record in since if record.get('skill') == 'manipulation_recovery'
               and record.get('decision') == 'reposition' and isinstance(record.get('offset'), dict)]

    if off is None or len(sent) >= STANCE_RULE_MAX:
        return None

    # The agent may have asked to stand off the base goal on purpose (a back-off for the camera): that stance is the target
    target   = {key: float((moved[-1] if moved else {}).get(key, 0.0)) for key in ('dx', 'dy', 'dyaw')}
    forward  = off['forward_m'] - target['dx']
    left     = off['left_m'] - target['dy']
    yaw      = math.atan2(math.sin(off['yaw_rad'] - target['dyaw']), math.cos(off['yaw_rad'] - target['dyaw']))
    distance = math.hypot(forward, left)

    if distance <= STANCE_TOLERANCE_M and abs(yaw) <= STANCE_TOLERANCE_RAD:
        return None

    goal = 'the base goal' if not moved else f'the target stance (dx {target["dx"]:+.3f}, dy {target["dy"]:+.3f})'

    return {
        'decision': 'reposition',
        'reason':   f'rule: the robot stands {distance:.3f} m and {math.degrees(yaw):+.1f} deg off {goal} '
                    f'(forward {forward:+.3f}, left {left:+.3f}): sending it again',
        **target,
    }


def rule_reach(evidence: dict | None, history: list[dict], last_failure: dict | None) -> dict | None:
    """reposition by the IK miss without the agent: a failed check_reach at this stance whose miss walking can fix
    within REPOSITION_LIMITS_M. At most REACH_RULE_MAX times since the last press judged pressed; then the agent decides"""
    move    = (evidence or {}).get('reach_move') or {}
    reach   = (evidence or {}).get('reach') or {}
    pressed = [index for index, record in enumerate(history) if record.get('skill') == 'press_judge' and record.get('decision') == 'pressed']
    since   = history[pressed[-1] + 1:] if pressed else history
    sent    = [record for record in since if record.get('skill') == 'manipulation_recovery' and record.get('rule')
               and str(record.get('reason', '')).startswith('rule: the IK check')]

    if (last_failure or {}).get('skill') != 'check_reach' or not move.get('fixable') or not move.get('within_limits'):
        return None

    if len(sent) >= REACH_RULE_MAX:
        return None

    miss = reach['pelvis_error_m']

    return {
        'decision': 'reposition',
        'reason':   f'rule: the IK check missed by x {miss["x"] * 1000:+.0f} y {miss["y"] * 1000:+.0f} z {miss["z"] * 1000:+.0f} mm '
                    f'in pelvis (arm end minus target): stepping {move["step_pelvis_m"]} m in pelvis, to dx {move["dx"]:+.3f} '
                    f'dy {move["dy"]:+.3f} dyaw {move["dyaw"]:+.3f} from the base goal',
        'dx': move['dx'], 'dy': move['dy'], 'dyaw': move['dyaw'],
    }


def stance_unchanged(evidence: dict | None) -> str:
    """Why a diagnosis may not reposition: the press ran from a stance check_reach passed, and its target has not moved
    in pelvis since. Empty when a reposition is open"""
    reach = (evidence or {}).get('reach') or {}
    moved = reach.get('target_moved_since_check_m')

    if not reach.get('reachable') or moved is None or moved['norm'] >= STANCE_CHANGED_M:
        return ''

    return (f'check_reach passed from this stance ({reach.get("message", "")}), and the press target has moved only '
            f'{moved["norm"] * 1000:.0f} mm in pelvis since (under {STANCE_CHANGED_M * 1000:.0f} mm): the stance did not '
            'change, so walking does not fix this press. Look at tracking, the target, aim or a step instead')


def decision_outputs(decision: dict, inputs: dict, last_failure: dict | None, robot: dict | None = None) -> dict:
    """What the tree gets: decision, reason, goal (base_goal, moved for a reposition), approach, walk_in, left_arm, offset"""
    failed_call   = (last_failure or {}).get('checked_call') or last_failure or {}     # a failed check: the call it checked
    failed_inputs = failed_call.get('inputs', {})
    base_goal     = inputs.get('base_goal')
    outputs       = {
        'decision': decision['decision'],
        'reason':   decision['reason'],
        'left_arm': bool(failed_inputs.get('left_arm', False)),
        'walk_in':  'no',
    }

    if base_goal is not None:
        outputs['goal'] = base_goal

    if decision['decision'] == 'reposition':
        offset            = {key: float(decision.get(key, 0.0)) for key in ('dx', 'dy', 'dyaw')}
        outputs['offset'] = offset
        outputs['goal']   = offset_goal(base_goal, offset['dx'], offset['dy'], offset['dyaw'])
        outputs['approach'] = outputs['goal']

        if robot is not None and math.hypot(outputs['goal']['x'] - robot['x'], outputs['goal']['y'] - robot['y']) < WALK_MIN_M:
            outputs['walk_in']  = 'yes'
            outputs['approach'] = offset_goal(outputs['goal'], -APPROACH_BACK_M, 0.0, 0.0)

    if decision['decision'] == 'switch_arm':
        outputs['left_arm'] = bool(decision['use_left_arm'])

    if decision['decision'] == 'offset_retry':
        outputs.update({f'offset_{axis}': float(decision[f'o{axis}']) for axis in 'xyz'})

    return outputs


def _json(value, whole: bool = False) -> str:
    text = json.dumps(value, indent=1, default=str)

    return text if whole or len(text) <= MAX_TOOL_TEXT else text[:MAX_TOOL_TEXT] + f'\n... [{len(text) - MAX_TOOL_TEXT} characters cut]'


class AgentSession:
    """One agent call: the evidence it was given, its tools, and its decision once made"""

    def __init__(self, phase: str, inputs: dict, evidence: dict, ros: RosAccess | None, config: AgentConfig, on_event,
                 speak: Callable[[str], None] | None = None, clock: Callable[[], float] = time.monotonic,
                 skill: str = '', run_id: str | None = None):
        self.skill    = skill
        self.run_id   = run_id
        self.speak    = speak
        self.clock    = clock
        self.spoke_at = -math.inf
        self.phase    = phase
        self.inputs   = inputs
        self.evidence = evidence
        self.ros      = ros
        self.config   = config
        self.on_event = on_event
        self.decision = None
        self.roots    = [Path(root).resolve() for root in [*config.read_roots, config.sandbox.scratch_dir]]

    def call(self, name: str, arguments: dict) -> ToolOutput:
        self.on_event('agent_tool', f'{name} {_brief(arguments)}', tool=name, arguments=arguments)

        handler = getattr(self, f'_tool_{name}', None)

        if handler is None:
            return ToolOutput(f'unknown tool {name}', is_error=True)

        try:
            output = handler(**arguments)
        except TypeError as error:
            output = ToolOutput(f'bad arguments for {name}: {error}', is_error=True)
        except Exception as error:                      # a tool failure goes back to the model, not up the stack
            output = ToolOutput(f'{name} failed: {error}', is_error=True)

        if output.is_error:
            self.on_event('agent_tool', f'{name} error: {output.text[:300]}', level='warn', tool=name)

        return output

    # ----- the tools -----

    def _tool_get_failure(self, whole: bool = False) -> ToolOutput:
        failure = self.evidence.get('last_failure')

        return ToolOutput(_json(failure, whole) if failure else 'no action has failed in this tree')

    def _tool_get_press(self, whole: bool = False) -> ToolOutput:
        press = self.evidence.get('press')

        return ToolOutput(_json(press, whole) if press else 'no press frames were kept')

    def _tool_get_stance(self, whole: bool = False) -> ToolOutput:
        found = self.evidence.get('stance')

        return ToolOutput(_json(found, whole) if found else 'no stance: TF or the base goal was not available')

    def _tool_get_history(self, whole: bool = False) -> ToolOutput:
        return ToolOutput(_json(self.evidence.get('history', []), whole))

    def _tool_read_file(self, path: str, offset: int = 1, limit: int | None = None) -> ToolOutput:
        real = Path(path).resolve()

        if not any(real == root or root in real.parents for root in self.roots):
            return ToolOutput(f'{path} is outside the readable trees: {", ".join(map(str, self.roots))}', is_error=True)

        if real.is_dir():
            entries = sorted(f'{entry.name}/' if entry.is_dir() else entry.name for entry in real.iterdir())
            return ToolOutput('\n'.join(entries))

        data = real.read_bytes()[:MAX_READ_BYTES]

        if b'\0' in data[:4096]:
            return ToolOutput(f'{path} is a binary file ({real.stat().st_size} bytes)', is_error=True)

        lines = data.decode(errors='replace').splitlines()
        start = max(offset, 1) - 1
        end   = len(lines) if limit is None else start + limit
        body  = '\n'.join(f'{number}\t{line}' for number, line in enumerate(lines[start:end], start=start + 1))

        return ToolOutput(body[:MAX_TOOL_TEXT] or '(empty)')

    def _tool_ros_read(self, op: str, target_frame: str = '', source_frame: str = '', topic: str = '', timeout_s: float = 2.0) -> ToolOutput:
        if self.ros is None:
            return ToolOutput('ROS access is not available here', is_error=True)

        if op == 'tf':
            return ToolOutput(_json(self.ros.tf(target_frame, source_frame, timeout_s)))

        if op == 'topic_once':
            return ToolOutput(_json(self.ros.topic_once(topic, timeout_s)))

        if op == 'topic_list':
            return ToolOutput(_json(self.ros.topic_list()))

        return ToolOutput(f'unknown op {op}', is_error=True)

    def _tool_get_image(self, source: str) -> ToolOutput:
        if self.ros is None:
            return ToolOutput('ROS access is not available here', is_error=True)

        data, media_type = self.ros.image(source)

        return ToolOutput(f'{source} image, {len(data)} bytes', image=data, media_type=media_type)

    def _tool_say(self, text: str) -> ToolOutput:
        text = ' '.join(text.split())

        if self.speak is None:
            return ToolOutput('there is no speaker here', is_error=True)

        if not text or len(text) > MAX_SPEECH:
            return ToolOutput(f'say 1 to {MAX_SPEECH} characters, not {len(text)}', is_error=True)

        waited = self.clock() - self.spoke_at

        if waited < SPEECH_GAP_S:
            return ToolOutput(f'you spoke {waited:.1f} s ago; wait {SPEECH_GAP_S - waited:.1f} s', is_error=True)

        self.speak(text)
        self.spoke_at = self.clock()
        self.on_event('agent_say', text)

        return ToolOutput(f'said: {text}')

    def _tool_run_python(self, code: str) -> ToolOutput:
        result = run_python(code, self.config.sandbox)
        text   = f'exit code {result["exit_code"]}' + (' (timed out)' if result['timed_out'] else '')
        text  += f'\n--- stdout\n{result["stdout"]}' + (f'\n--- stderr\n{result["stderr"]}' if result['stderr'] else '')

        return ToolOutput(text, is_error=result['exit_code'] != 0)

    def _tool_note(self, text: str, evidence: list, about: dict | None = None, key: str = '', value=None,
                   valid_for_s: float | None = None, claim: str = '', contradicts: bool = False) -> ToolOutput:
        if self.config.knowledge is None:
            return ToolOutput('there is no knowledge store here', is_error=True)

        if not text.strip() or not evidence:
            return ToolOutput('a note needs a sentence and at least one piece of evidence', is_error=True)

        store    = self.config.knowledge
        sighting = Sighting(by=f'agent:{self.skill}', time=time.time(), run=self.run_id, evidence=list(evidence), value=value)

        if claim:
            change = (store.contradict if contradicts else store.confirm)(claim, sighting)
            found  = store.get(claim)
        else:
            found, change = store.note(text, sighting, about, key or None, valid_for_s)

        self.on_event('knowledge', f'{found.id} {change}: {found.text}', claim=found.id, change=change, tier=found.tier)

        return ToolOutput(f'{found.id}: {change} ({found.tier})')

    def _tool_decide(self, decision: str, reason: str, dx: float | None = None, dy: float | None = None,
                     dyaw: float | None = None, use_left_arm: bool | None = None, ox: float | None = None,
                     oy: float | None = None, oz: float | None = None) -> ToolOutput:
        if self.decision is not None:
            return ToolOutput('you have already decided', is_error=True)

        allowed = PHASE_DECISIONS[self.phase]

        if decision not in allowed:
            return ToolOutput(f'decision must be one of {list(allowed)} in phase {self.phase}', is_error=True)

        if not reason.strip():
            return ToolOutput('give a reason', is_error=True)

        if decision == 'reposition':
            if self.inputs.get('base_goal') is None:
                return ToolOutput('this call has no base_goal, so a reposition cannot be carried out; choose another decision', is_error=True)

            if dx is None or dy is None:
                return ToolOutput('reposition needs dx and dy (dyaw defaults to 0)', is_error=True)

            if self.phase == 'diagnose' and (unchanged := stance_unchanged(self.evidence.get('stance'))):
                return ToolOutput(unchanged, is_error=True)

            if dx > REPOSITION_LIMITS_M['forward'] or dx < -REPOSITION_LIMITS_M['back'] or abs(dy) > REPOSITION_LIMITS_M['side']:
                return ToolOutput(f'a reposition stays within {REPOSITION_LIMITS_M["forward"]:g} m past the base goal, '
                                  f'{REPOSITION_LIMITS_M["back"]:g} m behind it and {REPOSITION_LIMITS_M["side"]:g} m to the side: '
                                  'the base goal stands close to the panel, so more puts the robot into it. A target that '
                                  'looks farther than that is a wrong detection, not a wrong stance: choose another decision',
                                  is_error=True)

        if decision == 'switch_arm' and use_left_arm is None:
            return ToolOutput('switch_arm needs use_left_arm', is_error=True)

        if decision == 'offset_retry':
            if ox is None or oy is None or oz is None:
                return ToolOutput('offset_retry needs ox, oy and oz: the whole press offset to use, in metres', is_error=True)

            if max(abs(ox), abs(oy), abs(oz)) > MAX_PRESS_OFFSET_M:
                return ToolOutput(f'offset_retry corrects at most {MAX_PRESS_OFFSET_M * 1000:g} mm per axis; a larger '
                                  'miss is a wrong target or a wrong stance: choose another decision', is_error=True)

        self.decision = {
            'decision':     decision,
            'reason':       reason.strip(),
            'dx':           dx or 0.0,
            'dy':           dy or 0.0,
            'dyaw':         dyaw or 0.0,
            'use_left_arm': use_left_arm,
            'ox':           ox,
            'oy':           oy,
            'oz':           oz,
        }
        self.on_event('agent_decision', f'{decision}: {reason.strip()}', decision=self.decision)

        return ToolOutput(f'decision recorded: {decision}', final=True)


def _brief(arguments: dict) -> str:
    text = ', '.join(f'{key}={value!r}' for key, value in arguments.items())

    return text if len(text) <= 200 else text[:200] + '...'


# ========== Running a call ==========

class AgentHandle:
    """One running agent call, polled by the leaf. Cancelling stops it at its next turn"""

    def __init__(self, pool: ThreadPoolExecutor, work: Callable[[threading.Event], dict]):
        self.status    = Status.RUNNING
        self.message   = ''
        self.outputs   = {}
        self._stop     = threading.Event()
        self._future   = pool.submit(work, self._stop)

    def poll(self) -> Status:
        if self.status != Status.RUNNING or not self._future.done():
            return self.status

        try:
            self.outputs = self._future.result()
        except Exception as error:                      # the backend, the network, or no decision
            self.status, self.message = Status.FAILURE, f'agent failed: {error}'
            return self.status

        decision     = self.outputs['decision']
        self.status  = Status.SUCCESS if decision in GOING_ON else Status.FAILURE
        self.message = f'{decision}: {self.outputs["reason"]}'

        return self.status

    def cancel(self):
        self._stop.set()


class Agent:
    """Runs agent skills for the executor: one call at a time, each in a thread"""

    def __init__(self, backend: AgentBackend, config: AgentConfig, ros: RosAccess | None = None, on_event=None,
                 speak: Callable[[str], None] | None = None, press_evidence: Callable[[], dict | None] | None = None):
        self.press_evidence = press_evidence    # the last press's saved frames and numbers, for the judge phase
        self.backend  = backend
        self.config   = config
        self.ros      = ros
        self.speak    = speak
        self.on_event = on_event or (lambda kind, text, **data: None)
        self._pool    = ThreadPoolExecutor(max_workers=1)

    def start(self, contract: Contract, inputs: dict, context: Context) -> AgentHandle:
        phase    = inputs.get('phase', 'diagnose')
        evidence = json.loads(json.dumps({'last_failure': context.last_failure, 'history': context.history}, default=str))

        def work(stop: threading.Event) -> dict:
            if phase == 'judge':                # waits until the press's frames are chosen, then saves them
                evidence['press'] = self.press_evidence() if self.press_evidence else None

            if phase == 'diagnose' and (evidence['last_failure'] or {}).get('skill') == 'press_judge' and self.press_evidence:
                evidence['press'] = self.press_evidence()      # the judged press: its frames are chosen already

            if phase in STANCE_PHASES:
                try:
                    evidence['stance'] = self._stance(inputs, evidence)
                except Exception as error:              # a missing transform leaves the call without a stance, not failed
                    evidence['stance'] = {'error': str(error)}

            if phase == 'diagnose' and (rule := self._rule(evidence)) is not None:
                self.on_event('agent_rule', rule['reason'], phase=phase)
                return {**decision_outputs(rule, inputs, evidence['last_failure'], self._robot()), 'rule': True}

            if phase in STANCE_PHASES and (rule := rule_stance(evidence.get('stance'), evidence['history'])) is not None:
                self.on_event('agent_rule', rule['reason'], phase=phase)
                self._say('I am not standing where I should. Walking to my spot again.')
                return {**decision_outputs(rule, inputs, evidence['last_failure'], self._robot()), 'rule': True}

            if phase == 'stance_check' and (rule := rule_reach(evidence.get('stance'), evidence['history'], evidence['last_failure'])):
                self.on_event('agent_rule', rule['reason'], phase=phase)
                self._say('The button is just out of my reach. Stepping closer to it.')
                return {**decision_outputs(rule, inputs, evidence['last_failure'], self._robot()), 'rule': True}

            system  = (Path(self.config.skills_dir) / f'{contract.interface.skill}.md').read_text()
            known   = self.config.knowledge.prompt_section(observations=True) if self.config.knowledge else ''
            system += f'\n\n{known}\n' if known else ''
            session = AgentSession(phase, inputs, evidence, self.ros, self.config, self.on_event, self.speak,
                                   skill=contract.interface.skill, run_id=context.run_id)

            self.on_event('agent_start', f'{contract.name} ({phase}) on {self.backend.name}', phase=phase)
            self._say(PHASE_SPEECH.get(phase))
            self.backend.run(system, _task(phase, inputs), tool_specs(phase), session.call,
                             self.config.max_turns, stop.is_set, self.on_event, skill=contract.interface.skill)

            if session.decision is None:
                raise AgentError('the agent ended without a decision')

            self._say(DECISION_SPEECH.get(session.decision['decision']))

            if phase == 'judge' and session.decision['decision'] == 'pressed':
                self._keep_reference(inputs, evidence.get('press') or {})

            return decision_outputs(session.decision, inputs, evidence['last_failure'], self._robot())

        return AgentHandle(self._pool, work)

    def _rule(self, evidence: dict) -> dict | None:
        failure = evidence.get('last_failure') or {}

        if failure.get('skill') != 'press_judge' or not self.press_evidence:
            return None

        press = self.press_evidence() or {}
        rule  = rule_offset(failure, evidence.get('history') or [], (press.get('capture') or {}).get('residual_m'),
                            press.get('press_offset_m'))

        if rule is not None:
            self._say('The press just missed the button. Correcting my aim and pressing again.')

        return rule

    def _stance(self, inputs: dict, evidence: dict) -> dict | None:
        """get_stance: the robot against the base goal and against the last press on this button that worked"""
        if self.ros is None:
            return None

        failure = evidence.get('last_failure') or {}
        goal    = stance.last_goal(failure)
        call    = failure.get('checked_call') or failure
        robot   = stance.pose_2d(self.ros.tf('map', 'pelvis', 1.0))
        button  = inputs.get('target_description', '')

        return stance.evidence(inputs.get('base_goal'), robot,
                               stance.transform_point(self.ros.tf('pelvis', PRESS_FRAME, 1.0), goal) if goal else None,
                               stance.transform_point(self.ros.tf('map', PRESS_FRAME, 1.0), goal) if goal else None,
                               (call.get('inputs') or {}).get('left_arm'), button, stance.load_references(),
                               stance.latest_reach(failure, evidence.get('history')), REPOSITION_LIMITS_M)

    def _robot(self) -> dict | None:
        if self.ros is None:
            return None

        try:
            return stance.pose_2d(self.ros.tf('map', 'pelvis', 1.0))
        except Exception:                               # no transform: the tree walks straight to the goal
            return None

    def _keep_reference(self, inputs: dict, press: dict):
        """A press judged pressed becomes the reference for later presses on this button"""
        if self.ros is None or not press.get('goal'):
            return

        try:
            stance.save_reference({
                'button':        inputs.get('target_description', ''),
                'left_arm':      bool(press.get('left_arm')),
                'robot':         stance.pose_2d(self.ros.tf('map', 'pelvis', 1.0)),
                'target_pelvis': stance.transform_point(self.ros.tf('pelvis', PRESS_FRAME, 1.0), press['goal']),
                'target_map':    stance.transform_point(self.ros.tf('map', PRESS_FRAME, 1.0), press['goal']),
            })
        except Exception as error:                      # a missing transform costs the reference, never the press
            self.on_event('agent_tool', f'press reference not kept: {error}', level='warn')

    def _say(self, text: str | None):
        if text and self.speak:
            self.speak(text)
            self.on_event('agent_say', text)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


def _task(phase: str, inputs: dict) -> str:
    lines = [f'Phase: {phase}.']

    if phase == 'judge':
        lines.append('press_button reported success. Look at the frames of the press (get_press; the press frame is '
                     'the fingertip at its closest point to the button) and judge whether the button was pressed, '
                     'following the skill document, and end with decide: pressed or not_pressed.')
    elif phase == 'diagnose':
        lines.append('The press did not work: press_button failed, or it succeeded and press_judge said not_pressed; '
                     'get_failure says which. The press ran only after button_in_view and check_reach passed: get_stance '
                     'reach. Work out why, following "How to work", and end with decide.')
    elif phase == 'confirm':
        lines += [f'Question: {inputs.get("question", "")}',
                  'The operator answers in the agent bridge: confirmed when they say yes, not_confirmed when they say no. '
                  'Without a reply the call times out.']
    elif phase == 'stance_check':
        lines.append('The walk to the panel has arrived and nothing has been pressed yet. button_in_view or check_reach '
                     'failed: get_failure says which and what it measured; get_stance reach_move is the reposition the IK '
                     'miss asks for. Check the stance, following "Before the first press", and end with decide.')

    if inputs.get('target_description'):
        lines.append(f'Button: {inputs["target_description"]}.')

    if inputs.get('base_goal') is not None:
        goal = inputs['base_goal']
        lines.append(f'Base goal (map): x {goal["x"]:.3f} m, y {goal["y"]:.3f} m, yaw {goal.get("yaw", 0.0):.3f} rad. '
                     'A reposition offset is relative to it, not to where the robot stands.')

    lines.append(f'Allowed decisions: {", ".join(PHASE_DECISIONS[phase])}.')

    return '\n'.join(lines)
