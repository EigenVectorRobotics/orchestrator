"""Start the agent's session from Mission Control: a Claude Code or a Codex session, in tmux on this host.

One task is one session (the executor holds the state), so a start replaces the agent of that kind that runs.
Each kind has its own tmux server and folder:

    tmux -L task       the task agent, in $EIGEN_VECTOR_WS/agent            attach: tmux -L task attach -t task
    tmux -L discovery  the discovery agent, in $EIGEN_VECTOR_WS/agent_discovery

The session asks for the open task (agent_cli task wait) and runs with no permission prompts; the folder's deny
rules (scripts/link_agent_dir.sh) still apply. The first start of Claude Code on a host asks two questions (trust
this folder, bypass mode): attach and answer them once. No ROS here.
"""

import time
import shlex
import threading
import subprocess

from pathlib import Path
from typing import Callable


PROMPT   = 'Do the open task: agent_cli task wait.'
TOOLS    = {                            # the tool -> its flags before the prompt: no sandbox, no approval prompts
    'claude': ['--dangerously-skip-permissions'],
    'codex':  ['-s', 'danger-full-access', '-a', 'never', '-c', 'model_reasoning_summary="detailed"'],
}
KINDS    = ('task', 'discovery')
TMUX_S   = 5.0                          # seconds for one tmux or shell command
STATE_S  = 2.0                          # seconds the page's view of the sessions is kept: each open page asks every second


class AgentSessions:
    """The agent sessions of this host, by kind. tool '' starts none: Mission Control then shows no Start button"""

    def __init__(self, tool: str, program: str, folders: dict, env_file: Path | None = None,
                 run: Callable = subprocess.run, clock: Callable[[], float] = time.monotonic):
        if tool and tool not in TOOLS:
            raise ValueError(f'agent must be one of {", ".join(TOOLS)}, or empty for none; not {tool!r}')

        self.tool     = tool
        self.program  = program or tool         # found on the PATH of the session's own shell, at each start
        self.folders  = {kind: Path(folder).expanduser() for kind, folder in folders.items()}
        self.env_file = env_file
        self._run     = run
        self._clock   = clock
        self._asking  = threading.Lock()         # one thread asks tmux; the others take the last answer
        self._starting = threading.Lock()        # one start at a time: two clicks must not stop each other's session
        self._running = (None, {})               # (when, kind -> a session runs) as last asked

    def state(self) -> dict | None:
        """The tool and which kinds run a session now; None when this host starts no agent"""
        if not self.tool:
            return None

        when, running = self._running

        if (when is None or self._clock() - when > STATE_S) and self._asking.acquire(blocking=False):
            try:
                running       = {kind: self._tmux(kind, 'has-session', '-t', kind).returncode == 0 for kind in KINDS}
                self._running = (self._clock(), running)
            finally:
                self._asking.release()

        return {'tool': self.tool, 'running': running}

    def start(self, body: dict) -> tuple[int, dict]:
        """A new session of the kind, for the open task: it replaces the program of the session that runs"""
        if not self.tool:
            return 404, {'ok': False, 'message': 'this host starts no agent (launch argument agent is empty)'}

        kind   = 'discovery' if body.get('discovery') else 'task'
        folder = self.folders[kind]

        if not folder.is_dir():
            return 409, {'ok': False, 'message': f'no folder {folder}: run scripts/link_agent_dir.sh on this host'}

        env = f'. {shlex.quote(str(self.env_file))}; ' if self.env_file and self.env_file.is_file() else ''

        with self._starting:
            found = self._shell(f'{env}command -v {shlex.quote(self.program)}')

            if found.returncode != 0 or not found.stdout.strip():
                return 409, {'ok': False, 'message': f'no {self.program} on the PATH of a login shell on this host: '
                                                     f'install it, or set the launch argument agent_program'}

            program = found.stdout.strip().splitlines()[-1]
            command = shlex.join(['bash', '-lc', f'{env}{shlex.join([program, *TOOLS[self.tool], PROMPT])}; exec bash'])  # the window stays
            # one task, one session: a running session gets the new program in its pane; tmux keeps its server, so a
            # new session never meets a server that is still going away
            if self._tmux(kind, 'has-session', '-t', kind).returncode == 0:
                answer = self._tmux(kind, 'respawn-pane', '-k', '-t', kind, '-c', str(folder), command)
            else:
                answer = self._tmux(kind, 'new-session', '-d', '-s', kind, '-x', '200', '-y', '50', '-c', str(folder), command)

            self._running = (None, {})           # the page sees the new session at its next poll

        if answer.returncode != 0:
            return 500, {'ok': False, 'message': f'tmux did not start the {self.tool} session: {answer.stderr.strip() or answer.returncode}'}

        return 200, {'ok': True, 'message': f'{program} started for the {kind}: tmux -L {kind} attach -t {kind}. '
                                            f'Its first start on a host asks questions there: attach and answer them'}

    def _tmux(self, kind: str, *words: str) -> subprocess.CompletedProcess:
        return self._command(['tmux', '-L', kind, *words])

    def _shell(self, script: str) -> subprocess.CompletedProcess:
        return self._command(['bash', '-lc', script])

    def _command(self, words: list[str]) -> subprocess.CompletedProcess:
        try:
            return self._run(words, capture_output=True, text=True, timeout=TMUX_S)
        except (OSError, subprocess.TimeoutExpired) as error:
            return subprocess.CompletedProcess(words, 1, '', str(error))
