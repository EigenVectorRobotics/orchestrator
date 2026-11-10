"""The child side of a code skill: run <agent_dir>/code/<module>.py and send every skill call to the executor.

agent_guard runs this file, so the rules of the agent sandbox hold: no ROS, no network, no new processes, and writes
only in the scratch directory. The code reaches the robot only through `skills`, which the executor answers.

Protocol, one JSON object per line: this file writes {"call": ...}, {"done": ...} or {"failed": ...} on the fd in
CODE_FD_REQUESTS, and reads the reply of a call on its stdin.
"""

import os
import sys
import json
import runpy
import traceback

REQUESTS = os.fdopen(int(os.environ['CODE_FD_REQUESTS']), 'w', buffering=1)
REPLIES  = sys.stdin


class Result:
    """What one skill call did. `if result:` is true when it succeeded"""

    def __init__(self, reply: dict):
        self.ok      = bool(reply.get('ok'))
        self.message = reply.get('message', '')
        self.outputs = reply.get('outputs') or {}

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f'Result(ok={self.ok}, message={self.message!r}, outputs={self.outputs})'


class Skills:
    """The only way out of the sandbox: every call is a skill the executor already loaded"""

    def run(self, skill: str, **inputs) -> Result:
        _send({'call': {'skill': skill, 'inputs': inputs}})
        line = REPLIES.readline()

        if not line:
            raise RuntimeError('the executor closed the link')

        return Result(json.loads(line))


def _send(message: dict):
    REQUESTS.write(json.dumps(message, default=str) + '\n')


def main():
    module = runpy.run_path(os.environ['CODE_SKILL_FILE'])
    run    = module.get('run')

    if not callable(run):
        _send({'failed': {'message': 'the code file needs def run(skills, inputs)'}})
        return

    outputs = run(Skills(), json.loads(os.environ['CODE_SKILL_INPUTS']))

    _send({'done': {'outputs': outputs if isinstance(outputs, dict) else {}}})


if __name__ == '__main__':
    try:
        main()
    except Exception:                       # the model sees its own traceback, as it would from run_python
        _send({'failed': {'message': traceback.format_exc(limit=4)}})
        sys.exit(1)
