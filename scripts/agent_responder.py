#!/usr/bin/env python3
import sys
import json
import time
import shlex
import argparse
import subprocess

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_session as session


USAGE = '''Answer press_judge and manipulation_recovery calls with a lean Claude session, one per call.

  agent_responder.py serve           answer calls until stopped
  agent_responder.py run TREE.xml    send the tree (a path on the robot), answer its calls, stop when it ends

Each call gets a fresh `claude -p` with a short prompt of its own skill: the frames for a judge, the failure, the recent
history and the executor's stance for a recovery. Calls of other skills (operator_confirm) stay open for a person.
Each answer, with its prompt, time and cost, is kept in <local>/<call id>/responder.json.'''

HANDLED         = ('press_judge', 'manipulation_recovery')
JUDGE_FRAMES    = ('press', 'release', 'baseline')
CALL_TIMEOUT_S  = 120.0
WAIT_SLICE_S    = 15                    # a wait this long, then a look whether the tree ended

JUDGE_SYSTEM = '''You judge whether a humanoid robot's fingertip pressed an elevator button, from camera frames: press \
(the fingertip at its closest point to the button), release (0.5 s later) and baseline (before the push), when kept. \
Read them all at once.
Pressed: in the press frame the fingertip covers or touches the centre of the requested button's face. A touch on \
the rim only is not a press. Floor buttons light a blue ring at once: a ring lit in press or release that is not lit \
in baseline is strong evidence of a press, and a fingertip on a floor button's face with no ring is not a press. The \
call panel does not light, so judge it by where the fingertip sits; the release frame often shows it when the hand \
covers the button in press. The depth number is measured against the arm's goal, not against the panel, so never \
judge from it alone.
Not pressed: the fingertip is on the rim, beside the button, on another button, or short of the panel with a visible \
gap, or the button is out of view. If you cannot see the fingertip at all, not_pressed.
Reason in two sentences: which frame you judged and where the fingertip sits on the button.'''

RECOVERY_SYSTEM = '''You recover a failed button press of a humanoid robot at an elevator panel. You get the call, the \
failure, the recent history and the executor's stance numbers. Answer with one allowed decision and a reason of two or \
three sentences that cites the numbers you used.
- continue (stance_check only): the stance is fine; the press runs and seeds its own track. Choose it when the button \
sits well inside the camera image, or when the track simply did not seed. The camera looks down, so a button at or \
within a few pixels of the bottom edge means the robot stands too close: reposition back instead.
- reposition: walk to dx, dy (m) and dyaw (rad) from the base goal, in its frame (x forward, y left). dx > 0 stands \
past the base goal, toward the panel, and is often needed: the base goal is a map place, and the panel can be nearer or \
farther than it says. The robot is a humanoid: its steps land a few centimetres off, so walking again often fixes a \
poor stance. Reposition whenever the numbers show the target too far, too close or off to the side. Take the stance \
from stance.match (same button), else stance.reach_match (the reach of a press that worked at another button), else \
stance.reference.stance_from_base_goal, else stance.to_base_goal, else your own estimate: a target too far ahead means a \
larger dx, too close a smaller one. Say which in the reason. navigate_to does not walk to a goal within 0.10 m of the \
robot: only for such a small correction, first reposition about 0.30 m back from the stance you want, and walk onto it \
at the next call.
- switch_arm: press with the other arm (use_left_arm). Choose it when the arm used cannot reach, no walking stance \
fixes it, and stance.other_arm shows the other arm pressed a target within 0.05 m of this one (target_offset_m). \
Without such a press, never assume which arm reaches what.
- offset_retry: the press reached its goal and missed by millimetres, in the button plane or along the normal (it \
stopped short: residual_m x negative). ox, oy, oz (m) = the offset in force minus residual_m, per axis, each within 0.05.
- retry: a transient failure, such as a track that did not seed, with nothing to change.
- ask_help: the evidence is missing or contradictory; a person presses.
- abort: the task cannot go on.
"target out of reach" from move_to_pose means its inverse kinematics missed by more than 5 mm; it does not say whether \
the target is too close or too far. Use the stance and the reference for that.'''


def main():
    parser = argparse.ArgumentParser(description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default='vector@vector-nuc')
    parser.add_argument('--env', default='~/orchestrator_ws/env.sh')
    parser.add_argument('--local', type=Path, default=Path('~/agent_calls').expanduser())
    parser.add_argument('--model', default='claude-sonnet-5-5')
    parser.add_argument('--effort', default='low')
    parser.add_argument('command', choices=('serve', 'run'))
    parser.add_argument('tree', nargs='?')
    arguments = parser.parse_args()
    arguments.local.mkdir(parents=True, exist_ok=True)

    if arguments.command == 'run':
        if not arguments.tree:
            parser.error('run needs the tree file on the robot')

        newest = latest_run(arguments)
        remote(arguments, f'source {arguments.env} >/dev/null 2>&1; nohup ros2 run orchestrator send_tree {shlex.quote(arguments.tree)} '
                          f'> /tmp/agent_responder_send.txt 2>&1 &')
        print(f'sent {arguments.tree}', flush=True)
        serve(arguments, until=lambda: tree_ended(arguments, newest))
    else:
        serve(arguments)


def serve(arguments, until=None):
    # (1) One frame watcher for the whole session, so no frame saved between two waits is missed
    watcher  = session.prefetch_frames(arguments.host, arguments.local / 'captures')
    answered = ''

    try:
        while until is None or not until():
            # (2) The next call, in slices, so the end of the tree is noticed
            arguments.command, arguments.args, arguments.answered = 'wait', ['--timeout', str(WAIT_SLICE_S)], answered
            status, call = session.fetch_call(arguments, prefetching=True, sync=False)

            if status != 0 or call is None:
                continue

            request  = call['request']
            answered = request['id']

            # (3) Answer it, or leave it for a person
            if request['skill'] not in HANDLED:
                print(f'call {request["id"]} {request["skill"]}: left for a person', flush=True)
                continue

            answer(arguments, call)
    finally:
        watcher.terminate()


def answer(arguments, call: dict):
    request = call['request']
    folder  = arguments.local / request['id']
    folder.mkdir(parents=True, exist_ok=True)
    start   = time.time()

    if request['skill'] == 'press_judge':
        prompt = judge_prompt(request, call['frames'])
        system = JUDGE_SYSTEM
        images = [call['frames'][name] for name in JUDGE_FRAMES if name in call['frames']]
    else:
        camera = folder / 'camera.jpg'

        if 'Phase: stance_check' in request['task']:
            session.copy_from(arguments.host, [snapshot(arguments)], camera)

        prompt = recovery_prompt(request, camera if camera.is_file() else None)
        system = RECOVERY_SYSTEM
        images = [camera] if camera.is_file() else []

    decision, meta = ask_claude(arguments, system, prompt, schema(request), folder, bool(images))
    sent           = send(arguments, decision) if decision else 'no decision: the call stays open'
    log            = {'skill': request['skill'], 'prompt': prompt, 'decision': decision, 'sent': sent,
                      'seconds': round(time.time() - start, 1), **meta}
    (folder / 'responder.json').write_text(json.dumps(log, indent=1))
    print(f'call {request["id"]} {request["skill"]}: {decision and decision["decision"]} in {log["seconds"]} s '
          f'(${meta.get("cost_usd", 0):.3f}); {sent}', flush=True)


def judge_prompt(request: dict, frames: dict) -> str:
    press   = request.get('press') or {}
    capture = press.get('capture') or {}
    button  = next((line.split(':', 1)[1].strip(' .') for line in request['task'].splitlines() if line.startswith('Button:')), '')
    shown   = [f'{name} {frames[name]}' for name in JUDGE_FRAMES if name in frames]

    return (f'Button: {button}. Arm: {"left" if press.get("left_arm") else "right"}. '
            f'Frames: {", ".join(shown) or "none kept"}. '
            f'Deepest point {capture.get("max_depth_m")} m along the button normal, {capture.get("lateral_at_max_m")} m sideways.')


def recovery_prompt(request: dict, camera: Path | None) -> str:
    failure = request.get('failure') or {}
    call    = failure.get('checked_call') or failure
    goals   = [sample['current_goal']['pose']['position'] for sample in call.get('feedback') or []
               if isinstance(sample, dict) and sample.get('current_goal')]
    lines   = [request['task'].strip(), '', f'Failure: {failure.get("skill")} {failure.get("status")}: {failure.get("message")}']

    if failure.get('checked_call'):
        lines.append(f'Checked press: {call.get("skill")} {call.get("status")}: {call.get("message")}')

    lines += [
        f'Press inputs {call.get("inputs")}, outputs {call.get("outputs")}',
        f'Last press goal in foot_world: {goals[-1] if goals else "none"}',
        press_line(request.get('press')),
        f'Offset in force (m): {offset_in_force(request.get("history") or [])}',
        '', 'History, last 10:',
        *[f'- {record.get("skill")} {record.get("status")}: {str(record.get("message"))[:100]}'
          + (f' (decision {record["decision"]})' if record.get('decision') else '') for record in (request.get('history') or [])[-10:]],
        '', f'Stance (executor): {json.dumps(request.get("stance"))}',
    ]

    if camera is not None:
        lines.append(f'Camera image now: {camera.name}')

    return '\n'.join(lines)


def press_line(press: dict | None) -> str:
    capture = (press or {}).get('capture') or {}

    if not capture:
        return 'Press capture: none (the press did not run, or kept no frames)'

    return (f'Press capture: deepest {capture.get("max_depth_m")} m along the button normal (0 is the goal, negative is short), '
            f'{capture.get("lateral_at_max_m")} m sideways there, residual_m (x in, y left, z up) {capture.get("residual_m")}')


def offset_in_force(history: list[dict]) -> list:
    offsets = [record.get('inputs') or {} for record in history if record.get('skill') == 'set_press_offset']
    last    = offsets[-1] if offsets else {}

    if isinstance(last, str):
        last = json.loads(last.replace("'", '"'))

    return [float(last.get(axis, 0.0)) for axis in 'xyz']


def schema(request: dict) -> dict:
    properties = {'decision': {'enum': request['decisions']}, 'reason': {'type': 'string'}}

    if request['skill'] == 'manipulation_recovery':
        properties.update({name: {'type': 'number'} for name in ('dx', 'dy', 'dyaw', 'ox', 'oy', 'oz')})
        properties['use_left_arm'] = {'type': 'boolean'}

    return {'type': 'object', 'properties': properties, 'required': ['decision', 'reason']}


def ask_claude(arguments, system: str, prompt: str, answer_schema: dict, folder: Path, images: bool) -> tuple[dict | None, dict]:
    tools   = ['--tools', 'Read', '--allowedTools', 'Read', '--add-dir', str(folder)] if images else ['--tools', '']
    command = ['claude', '-p', '--setting-sources', '', '--strict-mcp-config', '--no-session-persistence',
               '--model', arguments.model, '--effort', arguments.effort, *tools, '--system-prompt', system,
               '--json-schema', json.dumps(answer_schema), '--output-format', 'json', prompt]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=CALL_TIMEOUT_S, cwd=folder)
        reply  = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        return None, {'error': str(error)[:300]}

    usage = reply.get('usage') or {}
    meta  = {'cost_usd': reply.get('total_cost_usd', 0.0), 'turns': reply.get('num_turns'), 'claude_ms': reply.get('duration_ms'),
             'tokens_in': sum(usage.get(key) or 0 for key in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')),
             'tokens_out': usage.get('output_tokens')}

    if reply.get('is_error') or not reply.get('structured_output'):
        return None, {**meta, 'error': str(reply.get('result'))[:300]}

    return reply['structured_output'], meta


def send(arguments, decision: dict) -> str:
    flags = ['--reason', decision['reason']]

    if decision['decision'] == 'reposition':
        flags += [f'--{key}={decision.get(key, 0.0)}' for key in ('dx', 'dy', 'dyaw')]

    if decision['decision'] == 'offset_retry':
        flags += [f'--{key}={decision.get(key, 0.0)}' for key in ('ox', 'oy', 'oz')]

    if decision['decision'] == 'switch_arm':
        flags.append('--left-arm' if decision.get('use_left_arm') else '--right-arm')

    result = session.remote_cli(arguments, ['decide', decision['decision'], *flags, '--send'])
    lines  = (result.stdout + result.stderr).strip().splitlines()

    return lines[-1] if lines else f'decide exited {result.returncode}'


def snapshot(arguments) -> str:
    path = f'/tmp/agent_responder_{int(time.time() * 1000)}.jpg'
    session.remote_cli(arguments, ['image', 'camera', path])

    return path


def remote(arguments, command: str) -> str:
    result = subprocess.run(['ssh', *session.SSH, arguments.host, f'bash -lc {shlex.quote(command)}'], capture_output=True, text=True)

    return result.stdout.strip()


def latest_run(arguments) -> str:
    return remote(arguments, 'ls -td ~/orch_runs/*/runs/*/ 2>/dev/null | head -1')


def tree_ended(arguments, before: str) -> bool:
    newest = latest_run(arguments)

    return bool(newest) and newest != before and remote(arguments, f'test -f {newest}outcome.json && echo yes') == 'yes'


if __name__ == '__main__':
    main()
