"""Code as policy: a skill whose body is Python that calls other skills (docs/skill-discovery.md).

The code runs in the agent sandbox, so it reaches no ROS of its own. Every call it makes comes back here and runs
through the same runner as any other skill, so the deny list, the contracts, the step gate and the recorder all
still see it. A skill the gate list names (gate.yaml) asks the operator again, inside the code.
"""

import os
import json
import signal
import threading
import subprocess

from pathlib import Path

from skill_registry import Contract, GateList, Registry, code_file

from .behavior_tree import Status, TreeError
from .agent_sandbox import GUARD_PATH, SandboxConfig, sandbox_env
from .skill_leaf import DoneHandle


CHILD_PATH = Path(__file__).with_name('code_child.py')
POLL_S     = 0.05
HALTED     = 'the code skill was halted'
MAX_DEPTH  = 3             # code skills inside code skills: enough to build on a policy, not enough to run away


def _approved(contract, inputs, reasons):
    return True, ''


class CodeRunner:
    """Starts code skills; it holds what one needs: the library, the runner under it, and the operator's gate"""

    def __init__(self, registry: Registry, sandbox: SandboxConfig, code_dir: str, approve=_approved):
        self.registry   = registry
        self.sandbox    = sandbox
        self.code_dir   = Path(code_dir)
        self.gate       = GateList.load()
        self.approve    = approve          # (contract, inputs, reasons) -> (approved, reason)
        self.build_leaf = None             # set once the leaf factory exists: a call inside the code is a leaf too

    def path_of(self, contract: Contract) -> Path:
        return code_file(contract.name, self.code_dir)

    def start(self, contract: Contract, inputs: dict, context, chain: str = ''):
        """chain: the path of leaves down to this call, so a policy can see which policies it is already inside"""
        path = self.path_of(contract)

        if not path.is_file():
            return DoneHandle(False, f'no code file {path}')

        return CodeHandle(self, contract, inputs, context, chain or contract.name)

    def code_skills_in(self, chain: str) -> list[str]:
        """The code skills of a chain of leaves, oldest first"""
        named = [self.registry.get(part) for part in chain.split('/')]

        return [contract.name for contract in named if contract is not None and contract.interface.transport == 'code']


class CodeHandle:
    """One run of a code skill. The child does the deciding; this side does every call it asks for"""

    def __init__(self, code: CodeRunner, contract: Contract, inputs: dict, context, chain: str):
        self.status  = Status.RUNNING
        self.message = ''
        self.outputs = {}

        self._code     = code
        self._contract = contract
        self._context  = context
        self._inputs   = inputs
        self._chain    = chain                      # every leaf down to this one, e.g. approach_panel/press_button
        self._callers  = code.code_skills_in(chain)  # the code skills it is inside, this one last
        self._stop     = threading.Event()
        self._starting = threading.Lock()   # cancel must not race the child's start
        self._process  = None
        self._requests = None
        self._replies  = None
        self._path     = code.path_of(contract)
        self._log      = Path(code.sandbox.scratch_dir) / f'{contract.name}.log'   # what the code printed
        self._thread   = threading.Thread(target=self._run, daemon=True)

        self._thread.start()

    def _run(self):
        """Starting a child takes tens of milliseconds, so it happens here and not on the thread that ticks the tree"""
        try:
            self._start_child()
        except OSError as error:
            return self._finish(False, f'the code could not start: {error}')

        self._pump()

    def _start_child(self):
        scratch = Path(self._code.sandbox.scratch_dir)
        scratch.mkdir(parents=True, exist_ok=True)

        requests_read, requests_write = os.pipe()        # the child asks; this side answers on its stdin

        try:
            self._start_process(scratch, requests_write)
        except BaseException:                   # a start that fails leaves no pipe open in a node that runs for weeks
            os.close(requests_read)
            raise
        finally:
            os.close(requests_write)            # the child holds its own copy

        self._requests = os.fdopen(requests_read, 'r')
        self._replies  = self._process.stdin

    def _start_process(self, scratch: Path, requests_write: int):
        environment = sandbox_env(self._code.sandbox, scratch) | {
            'CODE_SKILL_FILE':   str(self._path),
            'CODE_SKILL_INPUTS': json.dumps(self._inputs, default=str),
            'CODE_FD_REQUESTS':  str(requests_write),
        }

        # What the code prints goes to a file, not a pipe: a pipe nobody reads fills up and stops the code
        with self._log.open('w') as output, self._starting:
            self._process = subprocess.Popen(
                [self._code.sandbox.python, '-I', str(GUARD_PATH), str(CHILD_PATH)],
                cwd=scratch,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                pass_fds=(requests_write,),
            )

            if self._stop.is_set():
                self._kill()

    # ========== The tree's side ==========

    def poll(self) -> Status:
        return self.status

    def cancel(self):
        self._stop.set()

        with self._starting:
            self._kill()

    # ========== The child's side ==========

    def _pump(self):
        try:
            for line in self._requests:
                if self._stop.is_set():
                    break

                message = json.loads(line)

                if 'call' in message:
                    self._answer(self._call(message['call']))
                elif 'done' in message:
                    self.outputs = message['done'].get('outputs') or {}
                    return self._finish(True, 'the code ended')
                elif 'failed' in message:
                    return self._finish(False, message['failed'].get('message', 'the code failed'))
        except Exception as error:              # whatever an inner call throws, the skill must end, not hang
            return self._finish(False, f'{type(error).__name__} while running the code: {error}')
        finally:
            self._close()

        if self.status == Status.RUNNING:
            self._finish(False, self._why_it_stopped())

    def _answer(self, reply: dict):
        try:
            self._replies.write(json.dumps(reply, default=str) + '\n')
            self._replies.flush()
        except (OSError, ValueError):
            self._stop.set()

    def _call(self, call: dict) -> dict:
        """One skill the code asked for; it runs as a leaf, so its inputs, timeout and record are a tree's"""
        name     = str(call.get('skill', ''))
        contract = self._code.registry.get(name)

        if contract is None:
            return {'ok': False, 'message': f'unknown skill {name}; it is not loaded or the deny list refused it'}

        if contract.interface.transport == 'code':
            if name in self._callers:
                return {'ok': False, 'message': f'{name} is already running ({" -> ".join(self._callers)}): '
                                                'a code skill cannot call itself'}

            if len(self._callers) >= MAX_DEPTH:
                return {'ok': False, 'message': f'{MAX_DEPTH} code skills deep ({" -> ".join(self._callers)}): '
                                                f'call plain skills from here, or let {self._contract.name} end first'}

        if reasons := self._code.gate.check(contract):
            approved, why = self._code.approve(contract, call.get('inputs') or {}, reasons)

            if not approved:
                return {'ok': False, 'message': f'the operator refused {name}: {why}'}

        return self._run_inner(name, call.get('inputs') or {})

    def _run_inner(self, name: str, inputs: dict) -> dict:
        """Tick one leaf until it ends. The leaf reads the places, keeps the timeout and writes the record"""
        try:
            leaf = self._code.build_leaf(name, f'{self._chain}/{name}', inputs, self._context)
        except TreeError as error:
            return {'ok': False, 'message': str(error)}

        while (status := leaf.tick()) == Status.RUNNING:
            if self._stop.is_set():
                leaf.halt()

                return {'ok': False, 'message': HALTED}

            self._stop.wait(POLL_S)

        return {'ok': status == Status.SUCCESS, 'message': leaf.message, 'outputs': leaf.outputs}

    # ========== Ending ==========

    def _why_it_stopped(self) -> str:
        if self._stop.is_set():
            return HALTED

        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill()

        printed = self._log.read_text(errors='replace').strip() if self._log.is_file() else ''

        return f'the code ended without a result (exit {self._process.returncode})' + (f': {printed[-800:]}' if printed else '')

    def _finish(self, success: bool, message: str):
        self.message = message
        self.status  = Status.SUCCESS if success else Status.FAILURE

        self._kill()

    def _kill(self):
        if self._process is None or self._process.poll() is not None:
            return

        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except OSError:
            pass

    def _close(self):
        for stream in (self._requests, self._replies):
            if stream is not None:
                stream.close()
