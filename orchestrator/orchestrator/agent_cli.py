"""agent_cli: a session (e.g. Claude Code over ssh on the NUC) does a task with it, and looks at the robot.

  agent_cli tf TARGET SOURCE            the pose of SOURCE in TARGET
  agent_cli topic TOPIC                 the next message on TOPIC (not images or point clouds)
  agent_cli topics                      the topic list
  agent_cli image camera|overlay OUT    save the latest image to OUT (overlay: the mask outlines on a frame)
  agent_cli python FILE                 run FILE in the agent sandbox (numpy, scipy, pinocchio, coal)
  agent_cli say TEXT                    speak TEXT through the robot's speaker (/speak)

Task mode (docs/task-mode.md): the session does a task step by step.
  agent_cli task wait [--timeout S]     block until a task is open; print it (the sentence, places, what is known)
  agent_cli task show                   the open task, or how the last one ended
  agent_cli skills [--dir DIR]          the skills that run can run now, from the executor (with an open task);
                                        --dir reads a skills folder instead, without ROS
  agent_cli plan SUBGOAL [SUBGOAL ...]  save the task plan (again, to change it)
  agent_cli run SKILL [KEY=VALUE ...] --subgoal S
                                        run one skill as the next step; wait for its end and print the result.
                                        A value is read as JSON when it can be ({"x": 1, ...}, 3, true), else as
                                        text; {key} is a blackboard key, e.g. {call_panel} or {step1_detections}
  agent_cli write-skill FILE [--code F] save the contract in FILE (YAML) as a skill: checked, then loaded at once;
                                        a code skill (transport code) takes its Python with --code
  agent_cli interface NAME|TYPE         a topic, service or action by name (/route_planner/navigate_to_goal), or a
                                        type (std_srvs/srv/Trigger): its types and their fields
  agent_cli done --reason R             the task is done
  agent_cli give-up --reason R          the task cannot be done
  agent_cli run ... --applies ID        the step follows that claim of the knowledge store (again for each claim)
  agent_cli grade ID useful|misleading|wrong|not-applicable --reason R
                                        stage a grade of a claim that a step followed (the last one wins; also after
                                        the task ends): useful and wrong are evidence; misleading and not-applicable
                                        say its text or tags are too wide
  agent_cli commit                      after the task ends: its staged grades go into the knowledge store

The step gate (on the real robot): a person approves each step, at a terminal; the session cannot.
  agent_cli step wait [--timeout S]     block until a step waits for approval; print it
  agent_cli step show                   the step that waits, or the last decision
  agent_cli step approve|refuse [--reason R]
                                        only at a terminal (a tty)

Run it in the same ROS environment as the executor. It publishes only to the executor (task commands, step
decisions) and speech (say); it never calls a skill's own interface.
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
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import String
from sensor_msgs.msg import Image

from .images import FrameBuffer, ImageError, image_to_jpeg, stamp_seconds
from .agent_ros import RosReader, RosReadError
from .sandbox import SandboxConfig, run_python
from .knowledge import GRADES
from .task_desk import task_summary
from .qos import COMMAND_QOS, LATCHED_QOS
from .topics import COMMAND_TOPIC, OPEN_TOPIC, REPLY_TOPIC, STEP_DECISION_TOPIC, STEP_REPLY_TOPIC, STEP_REQUEST_TOPIC


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
    node.create_subscription(String, topic, lambda message: holder.append(json.loads(message.data)), LATCHED_QOS)

    return holder[-1] if _spin_until(node, done, timeout_s) else None


def _request_reply(node, topic: str, reply_topic: str, message: dict, timeout_s: float) -> dict | None:
    """Publish message on topic; return the reply with its id, or None (the reason is printed)"""
    replies      = []
    publisher    = node.create_publisher(String, topic, COMMAND_QOS)
    subscription = node.create_subscription(String, reply_topic, lambda reply: replies.append(json.loads(reply.data)), COMMAND_QOS)

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


def _say(node, text: str) -> int:
    """One sentence to the robot's speaker; unitree_interface speaks every message at once, so keep them short and few"""
    text = ' '.join(text.split())

    if not text or len(text) > MAX_SPEECH:
        print(f'say needs 1 to {MAX_SPEECH} characters, not {len(text)}', file=sys.stderr)
        return 1

    publisher = node.create_publisher(String, SPEAK_TOPIC, COMMAND_QOS)

    if not _spin_until(node, lambda: publisher.get_subscription_count() > 0, 3.0):
        print(f'nobody subscribes to {SPEAK_TOPIC}', file=sys.stderr)
        return 1

    publisher.publish(String(data=text))
    _spin_until(node, lambda: False, 0.2)              # let it go out before the node ends
    print(f'said: {text}')

    return 0


def _value(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _task_command(node, command: dict, timeout_s: float,
                  show=lambda reply: json.dumps({key: value for key, value in reply.items() if key != 'id'}, indent=1, default=str),
                  ended: bool = False) -> int:
    """One command for the open task (ended: or the task that ended last); prints show(the executor's reply)"""
    task = _latest(node, OPEN_TOPIC, 3.0)

    if task is None or task['state'] != 'open' and not ended:
        print('no open task; run agent_cli task wait', file=sys.stderr)
        return 1

    command = {'id': uuid.uuid4().hex[:8], 'run_id': task['run_id'], **command}
    reply   = _request_reply(node, COMMAND_TOPIC, REPLY_TOPIC, command, timeout_s)

    if reply is None:
        return 1

    print(show(reply))

    return 0 if reply.get('ok') else 1


def _step_summary(request: dict) -> str:
    return (f'step {request.get("step")} of task {request.get("run_id")} ({request.get("state")}'
            + (f': {request["reason"]}' if request.get('reason') else '') + f'), for "{request.get("subgoal", "")}":\n'
            f'  {request.get("skill")} {json.dumps(request.get("inputs", {}))}'
            + ('\n  this call is inside a code skill, which was approved already' if request.get('in_code') else ''))


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
                                                                         'approve': arguments.action == 'approve',
                                                                         'by': 'agent_cli'}, 5.0)

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
    from .tasks import describe_catalog

    print(describe_catalog(Registry.from_directory(directory), set(DEFAULT_ESSENTIAL_DEVICES)))

    return 0


def main():
    parser   = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

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
    write_skill = commands.add_parser('write-skill')
    write_skill.add_argument('file', type=Path)
    write_skill.add_argument('--code', type=Path, help='a code skill: the Python with def run(skills, inputs)')
    commands.add_parser('interface').add_argument('name')
    run = commands.add_parser('run')
    run.add_argument('skill')
    run.add_argument('inputs', nargs='*', help='KEY=VALUE')
    run.add_argument('--subgoal', required=True)
    run.add_argument('--applies', action='append', default=[], help='the id of a known claim this step follows; '
                     'again for each claim. Grade each one after the step (agent_cli grade)')
    run.add_argument('--timeout', type=float, default=3600.0,
                     help='the wait includes the step gate; the task ends by max_task_s first, and that refuses the step')
    grade = commands.add_parser('grade')
    grade.add_argument('claim')
    grade.add_argument('grade', choices=list(GRADES))
    grade.add_argument('--reason', required=True)
    commands.add_parser('commit')
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
    if arguments.command == 'say':
        return _say(node, arguments.text)

    if arguments.command == 'task':
        wait   = arguments.action == 'wait'
        latest = _latest(node, OPEN_TOPIC, arguments.timeout if wait else 3.0, _is_open if wait else None)

        if latest is None:
            print('no task opened', file=sys.stderr)
            return 1

        print(task_summary(latest))
        return 0

    if arguments.command == 'step':
        return _step(node, arguments)

    if arguments.command == 'skills':
        return _task_command(node, {'op': 'skills'}, 10.0, lambda reply: reply.get('catalog') or reply.get('message'))

    if arguments.command == 'write-skill':
        return _task_command(node, {'op': 'write_skill', 'yaml': arguments.file.read_text(),
                                    'code': arguments.code.read_text() if arguments.code else ''}, 30.0)

    if arguments.command == 'interface':
        return _interface(node, arguments.name)

    if arguments.command == 'plan':
        return _task_command(node, {'op': 'plan', 'subgoals': arguments.subgoals}, 10.0)

    if arguments.command == 'run':
        if not all('=' in pair for pair in arguments.inputs):
            print('inputs are KEY=VALUE', file=sys.stderr)
            return 1

        inputs = {key: _value(value) for key, value in (pair.split('=', 1) for pair in arguments.inputs)}
        return _task_command(node, {'op': 'run', 'skill': arguments.skill, 'inputs': inputs, 'subgoal': arguments.subgoal,
                                    'applies': arguments.applies}, arguments.timeout)

    if arguments.command == 'grade':
        return _task_command(node, {'op': 'grade', 'claim': arguments.claim, 'grade': arguments.grade,
                                    'reason': arguments.reason}, 10.0, ended=True)

    if arguments.command == 'commit':
        return _task_command(node, {'op': 'commit'}, 30.0, ended=True)

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
