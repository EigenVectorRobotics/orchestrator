"""agent_cli: answer the executor's agent calls from a session, e.g. Claude Code over ssh on the NUC.

  agent_cli wait [--timeout S]          block until a call is open; print it
  agent_cli show [--full]               the current call; --full adds the failure with its feedback, and the history
  agent_cli tf TARGET SOURCE            the pose of SOURCE in TARGET
  agent_cli topic TOPIC                 the next message on TOPIC (not images or point clouds)
  agent_cli topics                      the topic list
  agent_cli image camera|overlay OUT    save the latest image to OUT (overlay: the mask outlines on a frame)
  agent_cli python FILE                 run FILE in the agent sandbox (numpy, scipy, pinocchio, coal)
  agent_cli decide DECISION --reason R [--dx --dy --dyaw] [--ox --oy --oz] [--left-arm | --right-arm] [--send]
                                        without --send, only show what would be sent
  agent_cli say TEXT                    speak TEXT through the robot's speaker (/speak)

Task mode (docs/task-mode.md): the session does a task step by step.
  agent_cli task wait [--timeout S]     block until a task is open; print it (the sentence, places, what is known)
  agent_cli task show                   the open task, or how the last one ended
  agent_cli skills [--dir DIR]          the skills that run can run now, from the executor (with an open task);
                                        --dir reads a contracts folder instead, without ROS
  agent_cli plan SUBGOAL [SUBGOAL ...]  save the task plan (again, to change it)
  agent_cli run SKILL [KEY=VALUE ...] --subgoal S
                                        run one skill as the next step; wait for its end and print the result.
                                        A value is read as JSON when it can be ({"x": 1, ...}, 3, true), else as
                                        text; {key} is a blackboard key, e.g. {call_panel} or {step1_detections}
  agent_cli write-skill FILE            save the contract in FILE (YAML) as a skill: checked, then loaded at once
  agent_cli interface NAME|TYPE         a topic, service or action by name (/route_planner/navigate_to_goal), or a
                                        type (std_srvs/srv/Trigger): its types and their fields
  agent_cli done --reason R             the task is done
  agent_cli give-up --reason R          the task cannot be done

The step gate (on the real robot): a person approves each step, at a terminal; the session cannot.
  agent_cli step wait [--timeout S]     block until a step waits for approval; print it
  agent_cli step show                   the step that waits, or the last decision
  agent_cli step approve|refuse [--reason R]
                                        only at a terminal (a tty)

Run it in the same ROS environment as the executor. It publishes only to the executor (decisions, task commands,
step decisions) and speech (say); it never calls a skill's own interface.
"""

import sys
import json
import subprocess
import time
import uuid
import argparse
import threading

from pathlib import Path

import rclpy

from rclpy.action import get_action_names_and_types
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import String
from sensor_msgs.msg import Image
from ament_index_python.packages import get_package_share_directory

from .images import ImageError, image_to_jpeg
from .agent_ros import RosReader, RosReadError
from .press_capture import FrameBuffer, stamp_seconds
from .agent_sandbox import SandboxConfig, run_python
from .agent_external import DECISION_TOPIC, REPLY_TOPIC, REQUEST_TOPIC
from .task_agent import (COMMAND_TOPIC, OPEN_TOPIC, STEP_DECISION_TOPIC, STEP_REPLY_TOPIC, STEP_REQUEST_TOPIC,
                         REPLY_TOPIC as TASK_REPLY_TOPIC)


LATCHED      = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
COMMAND      = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
CAMERA_TOPIC = '/camera/camera/color/image_raw'
SPEAK_TOPIC  = '/speak'                 # unitree_interface's TTS, remapped by its launch file
MAX_SPEECH   = 200                      # characters: one or two short sentences


def _spin_until(node, done, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s

    while not done() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)

    return done()


def _is_open(message: dict) -> bool:
    return message['state'] == 'open'


def _latest(node, topic: str, timeout_s: float, until=None) -> dict | None:
    """The latest latched message on topic (a call, a task or a step request), or None; with until, only one for
    which until(message) holds"""
    holder = []
    done   = lambda: bool(holder) and (until is None or until(holder[-1]))
    node.create_subscription(String, topic, lambda message: holder.append(json.loads(message.data)), LATCHED)

    return holder[-1] if _spin_until(node, done, timeout_s) else None


def _request_reply(node, topic: str, reply_topic: str, message: dict, timeout_s: float) -> dict | None:
    """Publish message on topic; return the reply with its id, or None (the reason is printed)"""
    replies      = []
    publisher    = node.create_publisher(String, topic, COMMAND)
    subscription = node.create_subscription(String, reply_topic, lambda reply: replies.append(json.loads(reply.data)), COMMAND)

    # the reply is volatile: it is lost if it comes before this node is matched to the reply publisher
    if not _spin_until(node, lambda: publisher.get_subscription_count() > 0 and subscription.get_publisher_count() > 0, 3.0):
        print(f'no executor listens on {topic} and answers on {reply_topic}', file=sys.stderr)
        return None

    publisher.publish(String(data=json.dumps(message)))
    matching = lambda: next((reply for reply in replies if reply.get('id') == message['id']), None)

    if not _spin_until(node, lambda: matching() is not None, timeout_s):
        print(f'sent, but no reply from the executor within {timeout_s:.0f} s', file=sys.stderr)
        return None

    return matching()


def _summary(request: dict, full: bool) -> str:
    lines = [
        f'call {request["id"]} ({request["state"]}), skill {request["skill"]}, opened {time.time() - request["opened"]:.0f} s ago',
        request['task'],
        f'skill document: {Path(get_package_share_directory("skill_registry")) / "agent_skills" / (request["skill"] + ".md")}',
    ]
    failure = request.get('failure')
    label   = 'failure'

    if failure and failure.get('checked_call'):
        lines.append(f'failure: the check {failure["skill"]} failed ({failure["message"]}) after {failure["checks"]} succeeded')
        failure, label = failure['checked_call'], 'checked call'

    if failure:
        feedback  = failure.get('feedback', [])
        distances = [sample.get('distance_to_goal') for sample in feedback if isinstance(sample.get('distance_to_goal'), (int, float))]
        lines    += [
            f'{label}: {failure["skill"]} {failure["status"]} after {failure["elapsed_s"]} s: {failure["message"]}',
            f'  inputs {json.dumps(failure["inputs"])}, outputs {json.dumps(failure["outputs"])}',
            f'  feedback: {len(feedback)} samples' + (f', distance_to_goal first {distances[0]:.3f} min {min(distances):.3f} last {distances[-1]:.3f} m' if distances else '')
            + (', tracking_lost seen' if any(sample.get('tracking_lost') for sample in feedback) else ''),
        ]

        if feedback and feedback[-1].get('current_goal'):
            goal = feedback[-1]['current_goal']
            lines.append(f'  last current_goal in {goal["header"]["frame_id"]}: {json.dumps(goal["pose"]["position"])}')
    else:
        lines.append('failure: none recorded')

    lines.append(f'history: {len(request.get("history") or [])} records')

    press = request.get('press')

    if press:
        capture = press.get('capture') or {}
        lines.append(f'press: {"left" if press.get("left_arm") else "right"} arm, deepest {capture.get("max_depth_m")} m along '
                     f'the button normal, {capture.get("lateral_at_max_m")} m sideways there, '
                     f'residual (x in, y left, z up) {capture.get("residual_m")} m')
        lines += [f'  frame {name}: {path}' for name, path in sorted((press.get('frames') or {}).items())]
    elif 'press' in request:
        lines.append('press: no frames were kept')

    if full:
        lines += ['--- failure', json.dumps(failure, indent=1), '--- history', json.dumps(request.get('history'), indent=1)]

    return '\n'.join(lines)


def _decide(node, arguments) -> int:
    request = _latest(node, REQUEST_TOPIC, 3.0)

    if request is None or request['state'] != 'open':
        print('no open call', file=sys.stderr)
        return 1

    if arguments.decision not in request['decisions']:
        print(f'{arguments.decision} is not allowed now; allowed: {request["decisions"]}', file=sys.stderr)
        return 1

    decision = {'id': request['id'], 'decision': arguments.decision, 'reason': arguments.reason}

    for key in ('dx', 'dy', 'dyaw', 'ox', 'oy', 'oz'):
        if getattr(arguments, key) is not None:
            decision[key] = getattr(arguments, key)

    if arguments.left_arm is not None:
        decision['use_left_arm'] = arguments.left_arm

    print(json.dumps(decision, indent=1))

    if not arguments.send:
        print('not sent: add --send once the decision is approved')
        return 0

    reply = _request_reply(node, DECISION_TOPIC, REPLY_TOPIC, decision, 5.0)

    if reply is None:
        return 1

    print(('accepted: ' if reply['accepted'] else 'rejected: ') + reply['message'])

    return 0 if reply['accepted'] else 1


def _say(node, text: str) -> int:
    """One sentence to the robot's speaker; unitree_interface speaks every message at once, so keep them short and few"""
    text = ' '.join(text.split())

    if not text or len(text) > MAX_SPEECH:
        print(f'say needs 1 to {MAX_SPEECH} characters, not {len(text)}', file=sys.stderr)
        return 1

    publisher = node.create_publisher(String, SPEAK_TOPIC, COMMAND)

    if not _spin_until(node, lambda: publisher.get_subscription_count() > 0, 3.0):
        print(f'nobody subscribes to {SPEAK_TOPIC}', file=sys.stderr)
        return 1

    publisher.publish(String(data=text))
    _spin_until(node, lambda: False, 0.2)              # let it go out before the node ends
    print(f'said: {text}')

    return 0


def _place_text(info: dict) -> str:
    """map_info's kind, floor and note of a place; empty for a places file"""
    text = ', '.join(part for part in (info.get('kind'), info.get('floor')) if part)

    return (f'({text})' if text else '') + (f': {info["note"]}' if info.get('note') else '')


def _task_summary(task: dict) -> str:
    if task['state'] != 'open':
        return f'no open task; the last one ({task["run_id"]}) ended: {task.get("end")}'

    places = ''.join(f'\n  {{{key}}} {_place_text(info)}' for key, info in (task.get('places') or {}).items())
    lines  = [
        f'task {task["run_id"]}' + (' (blind)' if task.get('blind') else '') + f': {task["task"]}',
        'places on the blackboard:' + (places or ' none'),
        f'scratch folder: {task.get("scratch")}',
        f'limits: {task.get("limits")}',
    ]

    return '\n'.join(lines + ([task['known']] if task.get('known') else []))


def _value(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _task_command(node, command: dict, timeout_s: float,
                  show=lambda reply: json.dumps({key: value for key, value in reply.items() if key != 'id'}, indent=1, default=str)) -> int:
    """One command for the open task; prints show(the executor's reply)"""
    task = _latest(node, OPEN_TOPIC, 3.0)

    if task is None or task['state'] != 'open':
        print('no open task; run agent_cli task wait', file=sys.stderr)
        return 1

    command = {'id': uuid.uuid4().hex[:8], 'run_id': task['run_id'], **command}
    reply   = _request_reply(node, COMMAND_TOPIC, TASK_REPLY_TOPIC, command, timeout_s)

    if reply is None:
        return 1

    print(show(reply))

    return 0 if reply.get('ok') else 1


def _step_summary(request: dict) -> str:
    return (f'step {request["step"]} of task {request["run_id"]} ({request["state"]}'
            + (f': {request["reason"]}' if request.get('reason') else '') + f'), for "{request["subgoal"]}":\n'
            f'  {request["skill"]} {json.dumps(request["inputs"])}')


def _step(node, arguments) -> int:
    """The step gate, for the person at the robot"""
    wait    = arguments.action == 'wait'
    request = _latest(node, STEP_REQUEST_TOPIC, arguments.timeout if wait else 3.0, _is_open if wait else None)

    if request is None or (arguments.action != 'show' and not _is_open(request)):
        print('no step waits for approval', file=sys.stderr)
        return 1

    print(_step_summary(request))

    if arguments.action in ('wait', 'show'):
        return 0

    reply = _request_reply(node, STEP_DECISION_TOPIC, STEP_REPLY_TOPIC, {'id': request['id'], 'reason': arguments.reason,
                                                                         'approve': arguments.action == 'approve'}, 5.0)

    if reply is None:
        return 1

    print(reply['message'])
    return 0 if reply['accepted'] else 1


def _graph_types(node, name: str) -> list[tuple[str, str]]:
    """(label, type) for each topic, service and action called name in the graph now"""
    found = []

    for kind, listing in (('topic', node.get_topic_names_and_types()), ('service', node.get_service_names_and_types()),
                          ('action', get_action_names_and_types(node))):
        found += [(f'{kind} {name}', ros_type) for graph_name, types in listing if graph_name == name for ros_type in types]

    return found


def _interface(node, name: str) -> int:
    """Read only: the types of a ROS name in the graph, or one type, with their definitions"""
    found = [('type', name)]

    if name.startswith('/'):
        def look() -> bool:                             # until discovery has it, 1 s at most
            found[:] = _graph_types(node, name)
            return bool(found)

        _spin_until(node, look, 1.0)

    if not found:
        print(f'{name} is not in the ROS graph now', file=sys.stderr)
        return 1

    for label, ros_type in found:
        shown = subprocess.run(['ros2', 'interface', 'show', ros_type], capture_output=True, text=True, timeout=20)
        print(f'--- {label}: {ros_type}')
        print(shown.stdout.rstrip() or shown.stderr.rstrip())

    return 0


def _skills(directory: Path) -> int:
    from skill_registry import DEFAULT_ESSENTIAL_DEVICES, Registry
    from .planner import describe_catalog

    print(describe_catalog(Registry.from_directory(directory), set(DEFAULT_ESSENTIAL_DEVICES)))

    return 0


def main():
    parser   = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    wait = commands.add_parser('wait')
    wait.add_argument('--timeout', type=float, default=3600.0)
    commands.add_parser('show').add_argument('--full', action='store_true')
    tf = commands.add_parser('tf')
    tf.add_argument('target')
    tf.add_argument('source')
    commands.add_parser('topic').add_argument('topic')
    commands.add_parser('topics')
    image = commands.add_parser('image')
    image.add_argument('source', choices=['camera', 'overlay'])
    image.add_argument('out', type=Path)
    python = commands.add_parser('python')
    python.add_argument('file', type=Path)
    python.add_argument('--scratch', default=SandboxConfig.scratch_dir)
    python.add_argument('--timeout', type=float, default=60.0)
    decide = commands.add_parser('decide')
    decide.add_argument('decision')
    decide.add_argument('--reason', required=True)
    decide.add_argument('--dx', type=float)
    decide.add_argument('--dy', type=float)
    decide.add_argument('--dyaw', type=float)
    for axis in 'xyz':
        decide.add_argument(f'--o{axis}', type=float, help=f'offset_retry: the press offset in {axis}, metres')
    arm = decide.add_mutually_exclusive_group()
    arm.add_argument('--left-arm', dest='left_arm', action='store_true', default=None)
    arm.add_argument('--right-arm', dest='left_arm', action='store_false')
    decide.add_argument('--send', action='store_true', help='publish the decision; without it, only print it')
    commands.add_parser('say').add_argument('text')
    task = commands.add_parser('task')
    task.add_argument('action', choices=['wait', 'show'])
    task.add_argument('--timeout', type=float, default=3600.0)
    skills = commands.add_parser('skills')
    skills.add_argument('--dir', type=Path)
    step = commands.add_parser('step')
    step.add_argument('action', choices=['wait', 'show', 'approve', 'refuse'])
    step.add_argument('--reason', default='')
    step.add_argument('--timeout', type=float, default=3600.0)
    commands.add_parser('plan').add_argument('subgoals', nargs='+')
    commands.add_parser('write-skill').add_argument('file', type=Path)
    commands.add_parser('interface').add_argument('name')
    run = commands.add_parser('run')
    run.add_argument('skill')
    run.add_argument('inputs', nargs='*', help='KEY=VALUE')
    run.add_argument('--subgoal', required=True)
    run.add_argument('--timeout', type=float, default=3600.0,
                     help='the wait includes the step gate; the task ends by max_task_s first, and that refuses the step')
    for name in ('done', 'give-up'):
        commands.add_parser(name).add_argument('--reason', required=True)
    arguments = parser.parse_args()

    if arguments.command == 'skills' and arguments.dir:     # a folder: no ROS needed
        sys.exit(_skills(arguments.dir))

    if arguments.command == 'step' and arguments.action in ('approve', 'refuse') and not sys.stdin.isatty():
        print('a step is approved or refused by a person at a terminal, not by a session', file=sys.stderr)
        sys.exit(1)

    if arguments.command == 'python':                   # no ROS needed
        result = run_python(arguments.file.read_text(), SandboxConfig(scratch_dir=arguments.scratch, timeout_s=arguments.timeout))
        print(result['stdout'], end='')
        print(result['stderr'], end='', file=sys.stderr)
        sys.exit(124 if result['timed_out'] else result['exit_code'])

    rclpy.init()
    node = rclpy.create_node('agent_cli')

    try:
        sys.exit(_run(node, arguments))
    except (RosReadError, ImageError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


def _run(node, arguments) -> int:
    if arguments.command == 'decide':
        return _decide(node, arguments)

    if arguments.command == 'say':
        return _say(node, arguments.text)

    if arguments.command in ('task', 'wait', 'show'):         # task wait|show, or wait|show for a call
        is_task = arguments.command == 'task'
        wait    = (arguments.action if is_task else arguments.command) == 'wait'
        latest  = _latest(node, OPEN_TOPIC if is_task else REQUEST_TOPIC, arguments.timeout if wait else 3.0,
                          _is_open if wait else None)

        if latest is None:
            print(f'no {"task" if is_task else "call"} opened', file=sys.stderr)
            return 1

        print(_task_summary(latest) if is_task else _summary(latest, getattr(arguments, 'full', False)))
        return 0

    if arguments.command == 'step':
        return _step(node, arguments)

    if arguments.command == 'skills':
        return _task_command(node, {'op': 'skills'}, 10.0, lambda reply: reply.get('catalog') or reply.get('message'))

    if arguments.command == 'write-skill':
        return _task_command(node, {'op': 'write_skill', 'yaml': arguments.file.read_text()}, 30.0)

    if arguments.command == 'interface':
        return _interface(node, arguments.name)

    if arguments.command == 'plan':
        return _task_command(node, {'op': 'plan', 'subgoals': arguments.subgoals}, 10.0)

    if arguments.command == 'run':
        if not all('=' in pair for pair in arguments.inputs):
            print('inputs are KEY=VALUE', file=sys.stderr)
            return 1

        inputs = {key: _value(value) for key, value in (pair.split('=', 1) for pair in arguments.inputs)}
        return _task_command(node, {'op': 'run', 'skill': arguments.skill, 'inputs': inputs, 'subgoal': arguments.subgoal}, arguments.timeout)

    if arguments.command in ('done', 'give-up'):
        return _task_command(node, {'op': arguments.command.replace('-', '_'), 'reason': arguments.reason}, 10.0)

    # Reads wait in this thread while an executor spins the node in another, as in the executor node
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spinner  = threading.Thread(target=executor.spin, daemon=True)
    spinner.start()

    try:
        frames = FrameBuffer()                          # for the overlay: its outlines name a frame by stamp
        reader = RosReader(node, lambda: image_to_jpeg(reader._once(CAMERA_TOPIC, Image, 3.0)), frame_at=frames.at)
        time.sleep(0.5)                                 # discovery

        if arguments.command == 'image' and arguments.source == 'overlay':
            node.create_subscription(Image, CAMERA_TOPIC, lambda message: frames.add(stamp_seconds(message.header.stamp), image_to_jpeg(message)),
                                     reader._qos_for(CAMERA_TOPIC))

        if arguments.command == 'tf':
            print(json.dumps(reader.tf(arguments.target, arguments.source, 3.0), indent=1))
        elif arguments.command == 'topic':
            print(json.dumps(reader.topic_once(arguments.topic, 3.0), indent=1))
        elif arguments.command == 'topics':
            print(json.dumps(reader.topic_list(), indent=1))
        elif arguments.command == 'image':
            data, media_type = reader.image(arguments.source)
            arguments.out.write_bytes(data)
            print(f'{arguments.out} ({media_type}, {len(data)} bytes)')
    finally:
        executor.shutdown()                             # stop spinning before the node goes
        spinner.join(timeout=2.0)

    return 0


if __name__ == '__main__':
    main()
