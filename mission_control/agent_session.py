#!/usr/bin/env python3
import os
import sys
import json
import time
import shlex
import argparse
import threading
import subprocess

from pathlib import Path


USAGE = '''Run agent_cli on the robot from the laptop, for a Claude Code session that answers agent calls.

  agent_session.py wait [--full]        block until a call opens, then show it
  agent_session.py show [--full]        the current call
  agent_session.py image camera|overlay OUT
  agent_session.py python FILE
  agent_session.py decide ... --send    send the decision, then wait for the next call (--no-wait: only send)
  agent_session.py <any other agent_cli command>    tf, topic, topics, say ...

wait and show sync the skill documents into <local>/skills/ before the call, saying whether they changed since the last
call, and put the press frames of a judge call in <local>/<call id>/, printing their local paths. wait pulls each frame
as soon as the executor saves it, so a judge call finds them here. All commands share one ssh connection. Every command
is timed into <local>/timing.jsonl. wait and decide --send write the call's record for the Run Viewer, and keep it in
<local>/<call id>/session.json.'''

FRAME_WAIT_S  = 30.0
SESSION_MODEL = 'claude-code session'
SSH           = ['-o', 'ControlMaster=auto', '-o', 'ControlPath=~/.ssh/agent_session_%C', '-o', 'ControlPersist=900']

# Prints each new press frame on the robot once it is whole (a JPEG ends with FF D9)
WATCH = '''
import os, time
d = os.path.expanduser("~/.ros/orchestrator_captures")
os.makedirs(d, exist_ok=True)
seen = set(os.listdir(d))
while True:
    for name in sorted(set(os.listdir(d)) - seen):
        if name.startswith(".") or not name.endswith(".jpg"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, "rb") as f:
                f.seek(-2, 2)
                whole = f.read() == b"\\xff\\xd9"
        except OSError:
            whole = False
        if whole:
            seen.add(name)
            print(path, flush=True)
    time.sleep(0.2)
'''


def main():
    parser = argparse.ArgumentParser(description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--host', default=os.environ.get('AGENT_HOST', 'vector@vector-nuc'))
    parser.add_argument('--env', default=os.environ.get('AGENT_ENV', '~/orchestrator_ws/env.sh'))
    parser.add_argument('--local', type=Path, default=Path(os.environ.get('AGENT_LOCAL', '~/agent_calls')).expanduser())
    parser.add_argument('command')
    parser.add_argument('args', nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    arguments.local.mkdir(parents=True, exist_ok=True)

    # A sent decision goes straight on to wait for the next call, so no call opens while nobody waits
    chain          = arguments.command == 'decide' and '--send' in arguments.args and '--no-wait' not in arguments.args
    arguments.args = [arg for arg in arguments.args if arg != '--no-wait']
    status         = timed(arguments)

    # A failed send (the call already closed: the ride ended it, or it timed out) waits too, or the next call finds nobody
    if chain:
        print('decision sent; waiting for the next call' if status == 0 else 'decision not taken; waiting for the next call', flush=True)
        last                              = arguments.local / 'last_call'
        arguments.answered                = last.read_text().strip() if last.is_file() else ''
        arguments.command, arguments.args = 'wait', []
        status                            = timed(arguments)

    sys.exit(status)


def timed(arguments) -> int:
    start  = time.time()
    status = run(arguments)
    log    = {'command': arguments.command, 'args': arguments.args[:2], 'start': round(start, 3), 'seconds': round(time.time() - start, 2),
              'status': status}

    with (arguments.local / 'timing.jsonl').open('a') as timing:
        timing.write(json.dumps(log) + '\n')

    return status


def run(arguments) -> int:
    if arguments.command in ('wait', 'show'):
        status, call = fetch_call(arguments)

        # A wait returns only with a call: a timeout or a lost connection waits again
        while arguments.command == 'wait' and call is None:
            time.sleep(1.0)
            status, call = fetch_call(arguments, sync=False)

        if call:
            print(call['summary'])
            print(call['docs_line'])

            if arguments.command == 'wait':
                open_record(arguments, call)

        return status

    if arguments.command == 'decide' and '--send' in arguments.args:
        result = remote_cli(arguments, [arguments.command, *arguments.args], echo=True)
        close_record(arguments, result)
        return result.returncode

    if arguments.command == 'image' and len(arguments.args) == 2:
        remote = f'/tmp/agent_session_{int(time.time() * 1000)}.jpg'
        status = remote_cli(arguments, ['image', arguments.args[0], remote]).returncode
        return status or copy_from(arguments.host, [remote], Path(arguments.args[1]))

    if arguments.command == 'python' and len(arguments.args) >= 1:
        remote = f'/tmp/agent_session_{Path(arguments.args[0]).name}'
        subprocess.run(['scp', *SSH, '-q', arguments.args[0], f'{arguments.host}:{remote}'], check=True)
        return remote_cli(arguments, ['python', remote, *arguments.args[1:]], echo=True).returncode

    return remote_cli(arguments, [arguments.command, *arguments.args], echo=True).returncode


def fetch_call(arguments, prefetching: bool = False, sync: bool = True) -> tuple[int, dict | None]:
    """The call as {request, summary, docs_line, frames: name -> local path}; prefetching: a watcher already runs"""
    # (1) Skill documents: synced before the call, while the robot works; re-read only when they changed
    changed   = sync_skills(arguments.host, remote_skill_dir(arguments), arguments.local / 'skills') if sync else []
    docs_line = (f'skill documents changed since the last call, read them: {", ".join(str(arguments.local / "skills" / name) for name in changed)}'
                 if changed else f'skill documents unchanged since the last call ({arguments.local / "skills"}): no need to read them again')

    # (2) Press frames: pulled as soon as the executor saves them, so a judge call finds them here
    staged  = arguments.local / 'captures'
    watcher = prefetch_frames(arguments.host, staged) if arguments.command == 'wait' and not prefetching else None

    # (3) The call; a wait right after a decision skips that call until the executor closes it
    answered = getattr(arguments, 'answered', '')

    while True:
        result = remote_cli(arguments, [arguments.command, *arguments.args, '--json'])

        if result.returncode != 0 or json.loads(result.stdout.strip().splitlines()[-1])['request']['id'] != answered:
            break

        time.sleep(0.5)

    if result.returncode != 0:
        if watcher:
            watcher.terminate()

        print(result.stderr, end='', file=sys.stderr)
        return result.returncode, None

    call    = json.loads(result.stdout.strip().splitlines()[-1])
    request = call['request']
    (arguments.local / 'skill_dir').write_text(call['skill_dir'])
    (arguments.local / 'last_call').write_text(request['id'])
    summary = call['summary'].replace(str(Path(call['skill_dir']) / f'{request["skill"]}.md'),
                                      str(arguments.local / 'skills' / f'{request["skill"]}.md'))

    # (4) Its frames: the executor saves them as the call opens, so the prefetch gets time to finish them; the rest copied now
    frames   = (request.get('press') or {}).get('frames') or {}
    deadline = time.time() + (FRAME_WAIT_S if watcher or prefetching else 0.0)
    local    = {}

    while frames and time.time() < deadline and not all((staged / Path(remote).name).is_file() for remote in frames.values()):
        time.sleep(0.1)

    if watcher:
        watcher.terminate()

    if frames:
        folder = arguments.local / request['id']
        folder.mkdir(parents=True, exist_ok=True)
        missing = []

        for remote in frames.values():
            name = Path(remote).name

            if (staged / name).is_file():
                (staged / name).replace(folder / name)
            else:
                missing.append(remote)

        if missing:
            copy_from(arguments.host, missing, folder)

        for name, remote in frames.items():
            local[name] = str(folder / Path(remote).name)
            summary     = summary.replace(remote, local[name])

    return 0, {'request': request, 'summary': summary, 'docs_line': docs_line, 'frames': local}


def open_record(arguments, call: dict):
    request = call['request']
    folder  = arguments.local / request['id']
    folder.mkdir(parents=True, exist_ok=True)
    head    = {'call': request['id'], 'skill': request['skill'], 'model': SESSION_MODEL, 'effort': None,
               'started': round(time.time(), 1), 'prompt': call['summary']}
    (folder / 'session.json').write_text(json.dumps(head, indent=1))
    publish(arguments, {**head, 'state': 'thinking'})


def close_record(arguments, result: subprocess.CompletedProcess):
    last   = arguments.local / 'last_call'
    record = arguments.local / (last.read_text().strip() if last.is_file() else '') / 'session.json'

    if not last.is_file() or not record.is_file():
        return

    # agent_cli decide prints the decision it sends as JSON, before the executor's reply
    head     = json.loads(record.read_text())
    output   = result.stdout
    decision = json.JSONDecoder().raw_decode(output[output.index('{'):])[0] if '{' in output else None
    lines    = (result.stdout + result.stderr).strip().splitlines()

    if decision:
        decision.pop('id', None)

    log = {**head, 'state': 'answered' if result.returncode == 0 else 'failed', 'decision': decision,
           'sent': lines[-1] if lines else f'decide exited {result.returncode}', 'seconds': round(time.time() - head['started'], 1)}
    record.write_text(json.dumps(log, indent=1))
    publish(arguments, log)


def publish(arguments, log: dict):
    """The call's record next to the newest recorder session on the robot, where the Run Viewer reads it"""
    command = ('session=$(dirname "$(ls -t ~/orch_runs/*/events.jsonl 2>/dev/null | head -1)") && '
               f'mkdir -p "$session/agent_calls" && cat > "$session/agent_calls/{log["call"]}.json"')
    subprocess.run(['ssh', *SSH, arguments.host, f'bash -lc {shlex.quote(command)}'], input=json.dumps(log, indent=1),
                   capture_output=True, text=True)


def remote_skill_dir(arguments) -> str:
    cached = arguments.local / 'skill_dir'

    if cached.is_file():
        return cached.read_text().strip()

    command = f'source {arguments.env} >/dev/null 2>&1 && echo $(ros2 pkg prefix skill_registry)/share/skill_registry/agent_skills'
    result  = subprocess.run(['ssh', *SSH, arguments.host, f'bash -lc {shlex.quote(command)}'], capture_output=True, text=True, check=True)

    return result.stdout.strip().splitlines()[-1]


def prefetch_frames(host: str, staged: Path) -> subprocess.Popen:
    staged.mkdir(parents=True, exist_ok=True)
    watcher = subprocess.Popen(['ssh', *SSH, host, f'python3 -u -c {shlex.quote(WATCH)}'], stdout=subprocess.PIPE, text=True)

    def pull():
        for line in watcher.stdout:
            name = Path(line.strip()).name
            part = staged / f'.{name}.part'                 # renamed once whole, so a waiting call never reads half a frame

            if subprocess.run(['scp', *SSH, '-q', f'{host}:{line.strip()}', str(part)]).returncode == 0:
                part.replace(staged / name)

    threading.Thread(target=pull, daemon=True).start()

    return watcher


def remote_cli(arguments, cli_args: list[str], echo: bool = False) -> subprocess.CompletedProcess:
    command = f'source {arguments.env} && ros2 run orchestrator agent_cli {shlex.join(cli_args)}'
    result  = subprocess.run(['ssh', *SSH, arguments.host, f'bash -lc {shlex.quote(command)}'], capture_output=True, text=True)

    if echo:
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)

    return result


def copy_from(host: str, remote_paths: list[str], destination: Path) -> int:
    if len(remote_paths) > 1 or destination.suffix == '':
        destination.mkdir(parents=True, exist_ok=True)

    return subprocess.run(['scp', *SSH, '-q', *[f'{host}:{path}' for path in remote_paths], str(destination)]).returncode


def sync_skills(host: str, skill_dir: str, local: Path) -> list[str]:
    local.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['rsync', '-a', '--itemize-changes', '-e', shlex.join(['ssh', *SSH]), '--include=*.md', '--exclude=*',
                             f'{host}:{skill_dir}/', f'{local}/'],
                            capture_output=True, text=True, check=True)

    return [line.split(maxsplit=1)[1] for line in result.stdout.splitlines() if line.startswith('>f')]


if __name__ == '__main__':
    main()
