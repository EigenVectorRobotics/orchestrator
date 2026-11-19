"""API style: the executor runs the task loop and a model makes one decision per turn (as HomeBody does).

The loop is a session inside the executor: it reads the open task, asks the decider for the next command (plan, run a
skill, done, give_up), gives it to the task desk, and puts the reply in the history of the next prompt. The desk
applies the same limits, step gate and recording as for a session outside (task_desk.py). The model only decides;
it has no tools of its own.

CodexDecider runs `codex exec` once per decision with the operator's ChatGPT login (no API key), read-only, with a
JSON schema for the answer; the prompt, the image and the reply of each turn stay in the run's scratch folder.
"""

import json
import time
import shutil
import tempfile
import threading
import subprocess

from pathlib import Path
from typing import Callable, Protocol

from .task_desk import task_summary


OPS = ('plan', 'run', 'write_skill', 'done', 'give_up')          # the desk's ops that the model may use

# Strict structured output: every field is required, unused ones are empty, and free-form inputs travel as JSON text
DECISION_SCHEMA = {
    'type':                 'object',
    'additionalProperties': False,
    'required':             ['reason', 'op', 'subgoals', 'skill', 'inputs_json', 'subgoal', 'applies', 'yaml', 'code'],
    'properties': {
        'reason':      {'type': 'string', 'description': 'why this is the next step, in one or two sentences'},
        'op':          {'type': 'string', 'enum': list(OPS)},
        'subgoals':    {'type': 'array', 'items': {'type': 'string'}, 'description': 'plan: the subgoals in order'},
        'skill':       {'type': 'string', 'description': 'run: the skill name'},
        'inputs_json': {'type': 'string', 'description': 'run: the inputs as a JSON object, e.g. {"goal": "{call_panel}"}'},
        'subgoal':     {'type': 'string', 'description': 'run: the subgoal of the plan this step is for'},
        'applies':     {'type': 'string', 'description': 'run: the id of a known claim this step follows (C-0012), or empty'},
        'yaml':        {'type': 'string', 'description': 'write_skill: the contract, as YAML text'},
        'code':        {'type': 'string', 'description': 'write_skill: for a code skill, the Python with def run(skills, inputs)'},
    },
}

SYSTEM = '''You are the task agent of a Unitree G1 humanoid robot. Each turn you make ONE decision, then the robot does it
and you see the result in the next turn. Decisions (field op):
- plan: write or rewrite the task plan as subgoals. Plan first, and again when the plan no longer fits.
- run: run one skill with its inputs, for one subgoal. A place is written {place_key}; an earlier step's output is
  {step<n>_<port>}. A person may refuse a step; then choose another way.
- write_skill: add a skill, or a new version of one you wrote. Put the contract in yaml. Use it when the stack has
  a capability that no listed skill reaches, or when the same few steps keep going together.
- done: the task is done (give the evidence in reason). give_up: it cannot be done (say why). A capability the
  stack has that no skill reaches is a reason to write_skill, not a reason to give up.
Use only the skills listed. Check the result of each step before the next one; do not repeat a failed step unchanged.
'''

HISTORY_TURNS = 12         # the last turns in the prompt; older ones are summarized by count
DETAILED_TURNS = 2         # of those, the newest that keep every part of their result: an older turn's detail has
                           # been acted on already, and a prompt is built again for every decision of the task
MAX_DECISION_CHARS = 1500  # a past decision is cut to this: write_skill carries a whole contract and its code
MAX_SECTION_CHARS = 600    # and each part of a result, on its own, so a long failure cannot push out the message
MAX_TRACE_CHARS = 200      # a trace line is one sentence (a validation line is about 60), and 12 ride in one reply
MAX_BRIEF_CHARS = 200      # an older turn: what it did, and how its result ended
PLUMBING = ('id', 'op', 'ok', 'trace')    # of a reply; the agent reads every other part, new ones included


class DeciderError(RuntimeError):
    pass


class Decider(Protocol):
    def decide(self, prompt: str, image: bytes | None, work: Path) -> dict: ...


def decision_text(decision: dict, detailed: bool = True) -> str:
    """A past decision: the whole of it for a recent turn, because write_skill carries a contract the agent may be
    repairing. For an older turn, what it did and what it did it with: the inputs are how the agent knows it has
    tried this already, and SYSTEM tells it not to repeat a failed step unchanged"""
    if detailed:
        return json.dumps(decision)[:MAX_DECISION_CHARS]

    did = ' '.join(str(decision[key]) for key in ('op', 'skill', 'inputs_json') if decision.get(key))

    return did[:MAX_BRIEF_CHARS]


def result_lines(reply: dict, detailed: bool = True) -> list[str]:
    """One turn's result as labelled lines, each cut on its own: every part of the reply but the plumbing, so a
    field added to StepResult reaches the agent with no change here, then what the robot logged while it ran.
    An older turn keeps one line: how it ended, which is what the agent still needs from it"""
    def text(value) -> str:
        return value if isinstance(value, str) else json.dumps(value, default=str)

    if not detailed:
        # the status first and always: a step can fail with a message that sounds like success (press_button says
        # 'pressed' and its check refuses it), and these lines are what the agent judges a repeat against
        ended = ' '.join(str(reply[key]) for key in ('status', 'message') if reply.get(key))

        return [f'  {ended[:MAX_BRIEF_CHARS]}' if ended else f'  ok: {reply.get("ok")}']

    lines  = [f'  {key}: {text(value)[:MAX_SECTION_CHARS]}' for key, value in reply.items()
              if value and key not in PLUMBING]
    lines += [f'  trace: {line[:MAX_TRACE_CHARS]}' for line in (reply.get('trace') or [])]

    return lines or [f'  ok: {reply.get("ok")}']


def build_prompt(opened: dict, catalog: str, history: list[tuple[dict, dict]], known: str = '') -> str:
    """SYSTEM says how to decide; task_summary carries what the robot is (the runbook), what is known, and how to
    write a skill (the contract grammar). known: the knowledge store's guide with its claims, in place of the open
    task's guide for a session, because this model has no tools to read them"""
    lines = [SYSTEM, task_summary(opened | ({'known': known} if known else {})), '', 'Skills:', catalog, '']
    older = len(history) - HISTORY_TURNS

    if older > 0:
        lines.append(f'({older} earlier turns not shown)')

    for turn, (decision, reply) in enumerate(history[-HISTORY_TURNS:], max(older, 0) + 1):
        detailed = turn > len(history) - DETAILED_TURNS
        lines.append(f'Turn {turn}: {decision_text(decision, detailed)}')
        lines += result_lines(reply, detailed)

    lines.append('' if history else 'No turn yet: start with a plan.')
    lines.append('The image is the latest camera frame. Decide the next step.')

    return '\n'.join(lines)


def to_command(decision: dict, run_id: str) -> dict:
    """The task desk's command for a decision: the desk checks it as it checks a session's"""
    inputs = json.loads(decision.get('inputs_json') or '{}') if decision.get('op') == 'run' else {}

    return {**decision, 'run_id': run_id, 'inputs': inputs}


class TaskLoop:
    def __init__(self, desk, decider: Decider, catalog: Callable[[], str], image: Callable[[], bytes | None],
                 tracer, retry_s: float = 10.0):
        self.desk    = desk
        self.decider = decider
        self.catalog = catalog
        self.image   = image
        self.tracer  = tracer
        self.retry_s = retry_s

    def start(self, opened: dict):
        threading.Thread(target=self.run, args=(opened,), daemon=True).start()

    def run(self, opened: dict):
        """Decide and act until the task this loop opened is closed"""
        run_id  = opened['run_id']
        history = []
        work    = Path(opened.get('scratch') or tempfile.mkdtemp(prefix=f'task_{run_id}_')) / 'decisions'
        is_open = lambda: getattr(self.desk.task, 'run_id', None) == run_id

        while is_open():
            try:
                folder = work / f'{len(history) + 1:03d}'
                folder.mkdir(parents=True, exist_ok=True)

                with self.desk.thinking():               # the desk's idle check: the model is at work
                    decision = self.decider.decide(build_prompt(opened, self.catalog(), history, self.desk.known(claims=True)),
                                                   self.image(), folder)

                if not is_open():                        # cancelled while the model thought: its answer is late
                    return

                try:
                    reply = self.desk.command(to_command(decision, run_id))
                except json.JSONDecodeError as error:
                    reply = {'ok': False, 'message': f'bad decision: inputs_json is not JSON: {error}'}

                history.append((decision, reply))
            except Exception as error:                   # e.g. no network: a quiet loop makes the desk say so
                if not is_open():                        # the trace is stamped with the open task's run: not ours
                    return

                self.tracer.emit('decider_error', f'{type(error).__name__}: {error}', level='warn', run_id=run_id)
                time.sleep(self.retry_s)


class CodexDecider:
    """One read-only, tool-free `codex exec` per decision, with the operator's login (HomeBody's way)"""

    # Codex features that could act or browse; only the ones this Codex version knows are turned off
    DISABLE = ('shell_tool', 'unified_exec', 'code_mode', 'code_mode_host', 'view_image', 'apps', 'plugins',
               'remote_plugin', 'multi_agent', 'multi_agent_v2', 'hooks', 'browser_use', 'browser_use_external',
               'computer_use', 'image_generation', 'skill_search', 'memories', 'goals', 'shell_snapshot', 'sleep_tool',
               'in_app_browser', 'in_app_local_automation', 'browser_use_full_cdp_access')

    def __init__(self, model: str = 'gpt-6-astra', effort: str = 'high', timeout_s: float = 180.0,
                 executable: str = 'codex'):
        self.model      = model
        self.effort     = effort
        self.timeout_s  = timeout_s
        self.executable = shutil.which(executable) or executable
        self._disabled  = None

    def features_off(self) -> list[str]:
        if self._disabled is None:                      # a failed list is not kept: no decision runs with its tools on
            listed = self._run([self.executable, 'features', 'list'], timeout_s=30.0)
            known  = {line.split()[0] for line in listed.stdout.splitlines() if line.strip()}

            if listed.returncode != 0 or 'shell_tool' not in known:
                raise DeciderError(f'codex features list failed, so its tools cannot be turned off: {listed.stderr[-300:]}')

            self._disabled = [feature for feature in self.DISABLE if feature in known]

        return self._disabled

    def command(self, work: Path, image: Path | None) -> list[str]:
        command = [self.executable, 'exec', '--ephemeral', '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check',
                   '--sandbox', 'read-only', '--cd', str(work), '--output-schema', str(work / 'schema.json'),
                   '--output-last-message', str(work / 'decision.json'), '--json', '--color', 'never',
                   '--model', self.model, '-c', 'web_search="disabled"', '-c', f'model_reasoning_effort="{self.effort}"',
                   '-c', 'approval_policy="never"']

        for feature in self.features_off():
            command += ['--disable', feature]

        if image is not None:
            command += ['--image', str(image)]

        return command + ['-']

    def decide(self, prompt: str, image: bytes | None, work: Path) -> dict:
        (work / 'schema.json').write_text(json.dumps(DECISION_SCHEMA, indent=1))
        (work / 'decision.json').unlink(missing_ok=True)            # never an older answer
        (work / 'prompt.md').write_text(prompt)
        frame = None

        if image is not None:
            frame = work / 'frame.jpg'
            frame.write_bytes(image)

        result = self._run(self.command(work, frame), stdin=prompt)
        (work / 'stdout.jsonl').write_text(result.stdout)

        if result.returncode != 0 or not (work / 'decision.json').exists():
            raise DeciderError(f'codex exited {result.returncode}: {_codex_error(result.stdout) or result.stderr[-300:]}')

        try:
            return json.loads((work / 'decision.json').read_text())
        except json.JSONDecodeError as error:
            raise DeciderError(f'codex gave no JSON decision: {error}') from error

    def _run(self, command: list[str], stdin: str = '', timeout_s: float | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout_s or self.timeout_s)
        except FileNotFoundError as error:
            raise DeciderError(f'no codex here: {error}') from error
        except subprocess.TimeoutExpired as error:
            raise DeciderError(f'codex took longer than {error.timeout:.0f} s') from error


def _codex_error(stdout: str) -> str:
    """The message of the last error event in codex's --json output"""
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get('type') in ('turn.failed', 'error'):
            error = event.get('error') or event
            return str(error.get('message', error))[:500]

    return ''
