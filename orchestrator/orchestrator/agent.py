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
from dataclasses import dataclass, field, replace
from concurrent.futures import ThreadPoolExecutor

from skill_registry import Contract

from .behavior_tree import Context, Status
from .knowledge import KnowledgeStore, Sighting
from .agent_sandbox import SandboxConfig, run_python


# The decisions each phase may end with, and the ones that let the tree go on
PHASE_DECISIONS = {
    'diagnose':   ('reposition', 'switch_arm', 'offset_retry', 'retry', 'ask_help', 'abort'),
    'view_check': ('continue', 'abort'),
    'judge':      ('pressed', 'not_pressed'),
}
GOING_ON = {'reposition', 'switch_arm', 'offset_retry', 'retry', 'continue', 'pressed'}
MAX_PRESS_OFFSET_M = 0.03               # an offset_retry corrects millimetres; a larger miss needs another decision

MAX_READ_BYTES = 200_000
MAX_TOOL_TEXT  = 40_000
SCRATCH_LISTED = 20                     # file names of a run's scratch folder shown in the task
MAX_SPEECH     = 200                    # characters said at once
SPEECH_GAP_S   = 3.0                    # unitree_interface speaks each message at once, so space them out


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

    if phase == 'judge':
        specs.insert(0, {
            'name':         'get_press',
            'description':  'The last press, as the executor kept it: the saved frames (baseline at the pre-grasp, press '
                            'at the fingertip\'s closest point to the button, release 0.5 s after the retreat began, and '
                            'their crops around the button), the phase times, the deepest point along the button normal '
                            'and the sideways distance there, and the arm.',
            'input_schema': {'type': 'object', 'properties': {}},
        })

    return specs


def _tool_specs(phase: str) -> list[dict]:
    return [
        {
            'name':         'get_failure',
            'description':  'The last failure of this tree: a failed action call (skill, inputs, message, outputs, '
                            'elapsed time, every feedback sample {t: seconds since the goal was sent, ...fields}), or a '
                            'failed check such as button_lit, whose checked_call is the action call it checked.',
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
                            'the last press kept for button_lit (at the pre-grasp, at the deepest push, after the release).',
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


def decision_outputs(decision: dict, inputs: dict, last_failure: dict | None) -> dict:
    """What the tree gets: decision, reason, goal (base_goal, moved for a reposition), left_arm, offset"""
    failed_call   = (last_failure or {}).get('checked_call') or last_failure or {}     # a failed check: the call it checked
    failed_inputs = failed_call.get('inputs', {})
    base_goal     = inputs.get('base_goal')
    outputs       = {
        'decision': decision['decision'],
        'reason':   decision['reason'],
        'left_arm': bool(failed_inputs.get('left_arm', False)),
    }

    if base_goal is not None:
        outputs['goal'] = base_goal

    if decision['decision'] == 'reposition':
        offset            = {key: float(decision.get(key, 0.0)) for key in ('dx', 'dy', 'dyaw')}
        outputs['offset'] = offset
        outputs['goal']   = offset_goal(base_goal, offset['dx'], offset['dy'], offset['dyaw'])

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
                 skill: str = '', run_id: str | None = None, scratch_dir: str | None = None):
        self.sandbox  = replace(config.sandbox, scratch_dir=scratch_dir) if scratch_dir else config.sandbox
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
        self.roots    = [Path(root).resolve() for root in [*config.read_roots, self.sandbox.scratch_dir]]

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
        result = run_python(code, self.sandbox)
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

            system  = (Path(self.config.skills_dir) / f'{contract.interface.skill}.md').read_text()
            known   = self.config.knowledge.prompt_section(observations=True) if self.config.knowledge else ''
            system += f'\n\n{known}\n' if known else ''
            session = AgentSession(phase, inputs, evidence, self.ros, self.config, self.on_event, self.speak,
                                   skill=contract.interface.skill, run_id=context.run_id, scratch_dir=context.scratch_dir)
            task    = _task(phase, inputs) + (_scratch_note(Path(context.scratch_dir)) if context.scratch_dir else '')

            self.on_event('agent_start', f'{contract.name} ({phase}) on {self.backend.name}', phase=phase)
            self.backend.run(system, task, tool_specs(phase), session.call,
                             self.config.max_turns, stop.is_set, self.on_event, skill=contract.interface.skill)

            if session.decision is None:
                raise AgentError('the agent ended without a decision')

            return decision_outputs(session.decision, inputs, evidence['last_failure'])

        return AgentHandle(self._pool, work)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


def _scratch_note(scratch: Path) -> str:
    """Tells the agent where its run's scratch folder is, and what earlier calls in this run left there (not the
    sandbox's own code files, which are hidden)"""
    names = sorted(path.name + '/' * path.is_dir() for path in scratch.iterdir() if not path.name.startswith('.'))
    shown = ', '.join(names[:SCRATCH_LISTED]) + (f' and {len(names) - SCRATCH_LISTED} more' if len(names) > SCRATCH_LISTED else '')

    return (f'\n\nYour scratch folder for this run is {scratch}: write notes there (plan, what you checked, what '
            f'you expect) for later calls in this run. ' + (f'Earlier calls left: {shown}.' if names else 'It is empty.'))


def _task(phase: str, inputs: dict) -> str:
    lines = [f'Phase: {phase}.']

    if phase == 'judge':
        lines.append('press_button reported success. Look at the frames of the press (get_press; the press frame is '
                     'the fingertip at its closest point to the button) and judge whether the button was pressed, '
                     'following the skill document, and end with decide: pressed or not_pressed.')
    elif phase == 'diagnose':
        lines.append('The press did not work: press_button failed, or it succeeded and its check (button_lit) failed; '
                     'get_failure says which. Work out why, following "How to work", and end with decide.')
    else:
        lines.append('The walk to the stance you chose has arrived. Do the view check in "After the reposition walk", '
                     'and end with decide.')

    if inputs.get('target_description'):
        lines.append(f'Button: {inputs["target_description"]}.')

    if inputs.get('base_goal') is not None:
        goal = inputs['base_goal']
        lines.append(f'Base goal (map): x {goal["x"]:.3f} m, y {goal["y"]:.3f} m, yaw {goal.get("yaw", 0.0):.3f} rad. '
                     'A reposition offset is relative to it, not to where the robot stands.')

    lines.append(f'Allowed decisions: {", ".join(PHASE_DECISIONS[phase])}.')

    return '\n'.join(lines)
