"""Before a task: are the stacks up (alive), and start one (command). No ROS here: the runner passes in the graph.

  alive    each listed topic has a publisher, each listed service exists, each listed URL answers
  command  the contract's fixed shell command, run with bash -lc on the executor's machine; exit code 0 is success
"""

import subprocess
import urllib.request

from typing import Callable


URL_TIMEOUT_S = 3.0
OUTPUT_CHARS  = 300


def url_answers(
    url: str,
    timeout_s: float = URL_TIMEOUT_S,
) -> bool:
    """The URL answers with a 2xx status within timeout_s."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


def judge_alive(
    spec: dict,
    published: set[str],
    services: set[str],
    answers: Callable[[str], bool],
) -> tuple[bool, str, dict]:
    """Which listed topics, services and URLs are missing; success when none is."""
    missing = {
        'topics':   [topic for topic in spec.get('topics', []) if topic not in published],
        'services': [service for service in spec.get('services', []) if service not in services],
        'http':     [url for url in spec.get('http', []) if not answers(url)],
    }
    missing = {kind: names for kind, names in missing.items() if names}
    total   = sum(len(spec.get(kind, [])) for kind in ('topics', 'services', 'http'))

    if not missing:
        return True, f'all {total} up', {}

    text = '; '.join(f'{kind}: {", ".join(names)}' for kind, names in missing.items())

    return False, f'missing {text}', {'missing': missing}


def run_command(
    command: str,
    timeout_s: float,
) -> tuple[bool, str, dict]:
    """Run the fixed command; success when it exits 0 within timeout_s."""
    try:
        done = subprocess.run(['bash', '-lc', command], capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, f'no exit within {timeout_s:g} s', {}

    output = (done.stdout + done.stderr).strip()[-OUTPUT_CHARS:]

    return done.returncode == 0, f'exit {done.returncode}' + (f': {output}' if output else ''), {'exit_code': done.returncode}
