"""agent_cli: answer the executor's agent calls from a session, e.g. Claude Code over ssh on the NUC.

  agent_cli wait [--timeout S]          block until a call is open; print it
  agent_cli show [--full]               the current call; --full adds the failure with its feedback, and the history
  agent_cli tf TARGET SOURCE            the pose of SOURCE in TARGET
  agent_cli topic TOPIC                 the next message on TOPIC (not images or point clouds)
  agent_cli topics                      the topic list
  agent_cli image camera|overlay OUT    save the latest image to OUT
  agent_cli python FILE                 run FILE in the agent sandbox (numpy, scipy, pinocchio, coal)
  agent_cli decide DECISION --reason R [--dx --dy --dyaw] [--ox --oy --oz] [--left-arm | --right-arm] [--send]
                                        without --send, only show what would be sent
  agent_cli say TEXT                    speak TEXT through the robot's speaker (/speak)

Run it in the same ROS environment as the executor. It publishes nothing but speech (say) and a decision (--send).
"""

import sys
import json
import time
import argparse
import threading

from pathlib import Path

import rclpy

from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import String
from sensor_msgs.msg import Image
from ament_index_python.packages import get_package_share_directory

from .images import ImageError, image_to_jpeg
from .agent_ros import RosReader, RosReadError
from .agent_sandbox import SandboxConfig, run_python
from .agent_external import DECISION_TOPIC, REPLY_TOPIC, REQUEST_TOPIC


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


def _latest_request(node, timeout_s: float) -> dict | None:
    holder = []
    node.create_subscription(String, REQUEST_TOPIC, lambda message: holder.append(json.loads(message.data)), LATCHED)
    _spin_until(node, lambda: bool(holder), timeout_s)

    return holder[-1] if holder else None


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

    stance = request.get('stance')

    if stance:
        lines.append(f'stance (executor): {json.dumps(stance)}')

    if full:
        lines += ['--- failure', json.dumps(failure, indent=1), '--- history', json.dumps(request.get('history'), indent=1)]

    return '\n'.join(lines)


def _decide(node, arguments) -> int:
    request = _latest_request(node, 3.0)

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

    replies   = []
    publisher = node.create_publisher(String, DECISION_TOPIC, COMMAND)
    node.create_subscription(String, REPLY_TOPIC, lambda message: replies.append(json.loads(message.data)), COMMAND)

    if not _spin_until(node, lambda: publisher.get_subscription_count() > 0, 3.0):
        print('no executor listens on ' + DECISION_TOPIC, file=sys.stderr)
        return 1

    publisher.publish(String(data=json.dumps(decision)))

    if not _spin_until(node, lambda: any(reply.get('id') == request['id'] for reply in replies), 5.0):
        print('sent, but no reply from the executor within 5 s', file=sys.stderr)
        return 1

    reply = next(reply for reply in replies if reply.get('id') == request['id'])
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
    arguments = parser.parse_args()

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

    if arguments.command in ('wait', 'show'):
        holder = []
        node.create_subscription(String, REQUEST_TOPIC, lambda message: holder.append(json.loads(message.data)), LATCHED)
        is_open = lambda: bool(holder) and holder[-1]['state'] == 'open'

        if arguments.command == 'wait':
            if not _spin_until(node, is_open, arguments.timeout):
                print('no call opened', file=sys.stderr)
                return 1
        else:
            _spin_until(node, lambda: bool(holder), 3.0)

            if not holder:
                print('no call yet', file=sys.stderr)
                return 1

        print(_summary(holder[-1], getattr(arguments, 'full', False)))
        return 0

    # Reads wait in this thread while an executor spins the node in another, as in the executor node
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spinner  = threading.Thread(target=executor.spin, daemon=True)
    spinner.start()

    try:
        reader = RosReader(node, lambda: image_to_jpeg(reader._once(CAMERA_TOPIC, Image, 3.0)))
        time.sleep(0.5)                                 # discovery

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
