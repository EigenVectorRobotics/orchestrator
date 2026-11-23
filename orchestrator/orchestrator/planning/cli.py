"""planner_cli: write the planner's plan from a session, e.g. Claude Code over ssh on the NUC (planner backend external).

    planner_cli wait [--timeout S]        block until a planning call is open; print it and save its system prompt
    planner_cli show                      the current call; saves its system prompt
    planner_cli answer --use-tree ID --note TEXT     choose an existing tree
    planner_cli answer --use-tree ID --param goal={elevator1_target_floor_2} --note TEXT
                                                     choose an existing tree with other parameter values
    planner_cli answer --tree FILE --note TEXT       answer with the tree in FILE (BehaviorTree.CPP v4 XML)
    planner_cli run --use-tree ID [--param NAME=VALUE ...] [--task TEXT] [--note TEXT]
                                                     send the task, wait for its call (after preflight), answer it

The system prompt (skill catalog, existing trees, rules) is saved to --system-out. The last message of the call is
the task, or the validator's errors on the last answer. Run it in the same ROS environment as the planner.
"""

import re
import sys
import json
import time
import argparse

from pathlib import Path

import rclpy

from std_msgs.msg import String
from skill_registry import load_tree

from ..common.topics import COMMAND_QOS as COMMAND, LATCHED_QOS as LATCHED, PLANNER_ANSWER_TOPIC as ANSWER_TOPIC, PLANNER_REQUEST_TOPIC as REQUEST_TOPIC, TASK_TOPIC


SYSTEM_OUT     = Path.home() / '.ros' / 'planner_system.md'
SUBSCRIBER_S   = 5.0


def spin_until(
    node,
    done,
    timeout_s: float,
) -> bool:
    """Spin until done() or the timeout; return done()."""
    deadline = time.monotonic() + timeout_s

    while not done() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)

    return done()


def latest_request(
    node,
    timeout_s: float,
    open_only: bool,
    not_id: str | None = None,
) -> dict | None:
    """The latest request on the latched topic (the open one when open_only, other than not_id), or None after timeout_s."""
    holder = []
    node.create_subscription(String, REQUEST_TOPIC, lambda message: holder.append(json.loads(message.data)), LATCHED)
    wanted = ((lambda: bool(holder) and holder[-1]['state'] == 'open' and holder[-1]['id'] != not_id) if open_only
              else (lambda: bool(holder)))

    return holder[-1] if spin_until(node, wanted, timeout_s) else None


def summary(
    request: dict,
    system_out: Path,
) -> str:
    """The call as text; the system prompt goes to system_out."""
    system_out.parent.mkdir(parents=True, exist_ok=True)
    system_out.write_text(request['system'])
    messages = request['messages']
    attempt  = sum(1 for message in messages if message['role'] == 'user')
    last     = messages[-1]['content'] if messages else ''

    return (f'call {request["id"]} ({request["state"]}), attempt {attempt}\n'
            f'system prompt: {system_out} ({len(request["system"])} characters)\n'
            f'last message:\n{last}')


def parse_parameters(
    texts: list[str],
) -> list[tuple[str, str]]:
    """NAME=VALUE texts as (name, value); a name is letters, digits and underscores, a value has no double quote."""
    parameters = []

    for text in texts:
        name, separator, value = text.partition('=')

        if not separator or not re.fullmatch(r'\w+', name) or '"' in value:
            raise ValueError(f'bad parameter {text!r}: give NAME=VALUE, with no double quote in the value')

        parameters.append((name, value))

    return parameters


def answer_text(
    arguments: argparse.Namespace,
) -> str:
    """The answer as the planner expects a model's: an explanation, then the tree or the choice."""
    if arguments.use_tree:
        attributes = ''.join(f' {name}="{value}"' for name, value in parse_parameters(arguments.param))

        return f'{arguments.note}\n<use_tree id="{arguments.use_tree}"{attributes}/>'

    return f'{arguments.note}\n<tree>\n{load_tree(arguments.tree).strip()}\n</tree>'


def run_text(
    arguments: argparse.Namespace,
) -> tuple[str, str]:
    """The task sentence and the note for run, from the tree and its parameters when not given."""
    values = ', '.join(f'{name}={value}' for name, value in parse_parameters(arguments.param))
    task   = arguments.task or f'Run {arguments.use_tree}' + (f' with {values}' if values else '') + '.'
    note   = arguments.note or f'The existing tree {arguments.use_tree}' + (f' with {values}' if values else '') + '.'

    return task, note


def run_task(
    node,
    arguments: argparse.Namespace,
) -> int:
    """Send the task, wait for its planning call (preflight runs first), answer it with the tree."""
    before = latest_request(node, 1.0, open_only=False)
    task, arguments.note = run_text(arguments)
    publisher = node.create_publisher(String, TASK_TOPIC, COMMAND)

    if not spin_until(node, lambda: publisher.get_subscription_count() > 0, SUBSCRIBER_S):
        print(f'no planner subscribes to {TASK_TOPIC}: is the orchestrator up?', file=sys.stderr)
        return 1

    publisher.publish(String(data=task))
    print(f'task sent: {task}\nwaiting for the planning call (preflight runs first, up to {arguments.timeout:g} s)', flush=True)

    return send_answer(node, arguments, timeout_s=arguments.timeout, not_id=(before or {}).get('id'))


def send_answer(
    node,
    arguments: argparse.Namespace,
    timeout_s: float = 3.0,
    not_id: str | None = None,
) -> int:
    """Publish the answer to the open call."""
    request = latest_request(node, timeout_s, open_only=True, not_id=not_id)

    if request is None:
        print('no open planning call', file=sys.stderr)
        return 1

    publisher = node.create_publisher(String, ANSWER_TOPIC, COMMAND)
    spin_until(node, lambda: publisher.get_subscription_count() > 0, SUBSCRIBER_S)
    publisher.publish(String(data=json.dumps({'id': request['id'], 'text': answer_text(arguments)})))
    spin_until(node, lambda: False, 0.5)            # let the message go out before the node ends
    print(f'answered call {request["id"]}')

    return 0


def main() -> None:
    """Parse the command and run it."""
    parser   = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    wait = commands.add_parser('wait')
    wait.add_argument('--timeout', type=float, default=3600.0)
    wait.add_argument('--system-out', type=Path, default=SYSTEM_OUT)
    show = commands.add_parser('show')
    show.add_argument('--system-out', type=Path, default=SYSTEM_OUT)
    answer = commands.add_parser('answer')
    choice = answer.add_mutually_exclusive_group(required=True)
    choice.add_argument('--use-tree', metavar='ID')
    choice.add_argument('--tree', type=Path, metavar='FILE')
    answer.add_argument('--note', required=True, help='a few sentences: what the robot will do, how it handles failures')
    answer.add_argument('--param', action='append', default=[], metavar='NAME=VALUE',
                        help='with --use-tree: a parameter of the tree, e.g. goal={elevator1_target_floor_2}; repeat for more')

    run = commands.add_parser('run')
    run.add_argument('--use-tree', metavar='ID', required=True)
    run.add_argument('--param', action='append', default=[], metavar='NAME=VALUE', help='a parameter of the tree; repeat for more')
    run.add_argument('--task', help='the task sentence; default: the tree and its parameters')
    run.add_argument('--note', help='the answer note; default: the tree and its parameters')
    run.add_argument('--timeout', type=float, default=900.0, help='how long to wait for the call, preflight included')

    arguments = parser.parse_args()

    if arguments.command == 'answer' and arguments.param and not arguments.use_tree:
        parser.error('--param needs --use-tree; in a --tree FILE, set the parameters in the file')

    if arguments.command in ('answer', 'run'):
        try:
            parse_parameters(arguments.param)
        except ValueError as error:
            parser.error(str(error))

    rclpy.init()
    node = rclpy.create_node('planner_cli')

    try:
        if arguments.command == 'answer':
            sys.exit(send_answer(node, arguments))

        if arguments.command == 'run':
            sys.exit(run_task(node, arguments))

        request = latest_request(node, arguments.timeout if arguments.command == 'wait' else 3.0, arguments.command == 'wait')

        if request is None:
            print('no planning call' + (' opened' if arguments.command == 'wait' else ' yet'), file=sys.stderr)
            sys.exit(1)

        print(summary(request, arguments.system_out))
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
