"""run_python for the agent: a child Python process under sandbox_guard, with resource limits and no ROS discovery"""

import os
import signal
import functools
import itertools
import subprocess

from pathlib import Path
from dataclasses import dataclass, field

from skill_registry import DenyList


GUARD_PATH  = Path(__file__).with_name('sandbox_guard.py')
CODE_FOLDER = '.code'
# Folders the agent's code must not read: the other orchestrator workspace on the NUC, whose hand-written contracts
# skill discovery must find on its own. fnmatch patterns on the real path; * crosses /
HIDDEN      = ['*/orchestrator_ws', '*/orchestrator_ws/*']


@dataclass
class SandboxConfig:
    scratch_dir:   str = str(Path.home() / 'orch_knowledge' / 'scratch')
    python:        str = '/usr/bin/python3'
    python_paths:  list[str] = field(default_factory=lambda: ['/opt/ros/jazzy/lib/python3.12/site-packages'])  # pinocchio, coal
    library_paths: list[str] = field(default_factory=lambda: ['/opt/ros/jazzy/lib', '/opt/ros/jazzy/lib/x86_64-linux-gnu'])
    timeout_s:     float = 60.0
    memory_mb:     int = 4096
    max_output:    int = 20000      # characters of stdout and of stderr returned to the model
    hidden:        list[str] = field(default_factory=lambda: list(HIDDEN))


_run_numbers = itertools.count(1)


@functools.cache
def _denied_modules() -> str:
    return ':'.join(DenyList.load().modules)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    return text[:limit // 2] + f'\n... [{len(text) - limit} characters cut] ...\n' + text[-limit // 2:]


def sandbox_env(config: SandboxConfig, scratch: Path) -> dict:
    """The environment of a guarded child: no ROS discovery, the deny list's modules, the limits sandbox_guard applies"""
    return {
        'PATH':                          '/usr/bin:/bin',
        'HOME':                          str(scratch),
        'AGENT_SCRATCH':                 str(scratch),
        'AGENT_PYTHONPATH':              ':'.join(config.python_paths),
        'LD_LIBRARY_PATH':               ':'.join(config.library_paths),
        'ROS_AUTOMATIC_DISCOVERY_RANGE': 'OFF',          # even if ROS loads, it finds no one
        'OMP_NUM_THREADS':               '2',
        'PYTHONDONTWRITEBYTECODE':       '1',
        'AGENT_MEMORY_MB':               str(config.memory_mb),     # sandbox_guard applies the limits itself
        'AGENT_CPU_S':                   str(int(config.timeout_s) + 5),
        'AGENT_DENIED_MODULES':          _denied_modules(),     # the deny list's: the Unitree SDK, raw DDS
        'AGENT_HIDDEN':                  ':'.join(config.hidden),
    }


def run_python(code: str, config: SandboxConfig) -> dict:
    """Run the code; return stdout, stderr, exit_code and timed_out"""
    scratch = Path(config.scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)

    code_path = scratch / CODE_FOLDER / f'run_{os.getpid()}_{next(_run_numbers)}.py'     # hidden from the agent's file list
    code_path.parent.mkdir(exist_ok=True)
    code_path.write_text(code)

    environment = sandbox_env(config, scratch)
    process = subprocess.Popen(
        [config.python, '-I', str(GUARD_PATH), str(code_path)],
        cwd=scratch,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True
    )

    try:
        stdout, stderr = process.communicate(timeout=config.timeout_s)
        timed_out      = False
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        timed_out      = True

    return {
        'stdout':    _clip(stdout, config.max_output),
        'stderr':    _clip(stderr, config.max_output),
        'exit_code': process.returncode,
        'timed_out': timed_out,
    }
