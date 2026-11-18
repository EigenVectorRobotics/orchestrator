import os
import re
import subprocess
import sys

from collections import Counter, defaultdict


CONTROL_NODES = ('unitree_interface', 'path_follower', 'cmd_vel_mux', 'local_planner', 'route_planner',
                 'slam_elevatorlio', 'lio_node')
NODE_DEFAULTS = {'lio': 'lio_node', 'unitree_interface_tuning_node': 'unitree_interface'}
HOSTS         = {
    'nuc':  {'ssh': None, 'launch': 'nuc.launch.py', 'install': '/home/vector/eigen_vector_ws/install/'},
    'orin': {'ssh': 'unitree@192.168.123.164', 'launch': 'orin.launch.py', 'install': '/home/unitree/eigen_vector_ws/install/'},
}
HELPERS       = {                       # pattern: age in s below which an orphan may be the current run's own helper
    'ros2 topic echo': 600,
    'ros2 bag record': 3 * 3600,
}
SHELLS        = ('bash', 'sh', 'tmux', 'nohup', 'ssh', 'timeout')
LIB_PATH      = re.compile(r'/lib/[^/]+/[^/]+$')
TIMEOUT_S     = 15
PS_FIELDS     = 'pid=,ppid=,etimes=,args='


def parse_ps(text: str) -> dict[int, dict]:
    """ps -eo pid=,ppid=,etimes=,args= output as {pid: {pid, ppid, age_s, args}}"""
    table = {}

    for line in text.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and all(field.isdigit() for field in fields[:3]):
            pid, ppid, age = map(int, fields[:3])
            table[pid] = {'pid': pid, 'ppid': ppid, 'age_s': age, 'args': fields[3]}

    return table


def ros_node(args: str) -> tuple[str, str] | None:
    """(node name, executable path) of an installed ROS executable: its __node remap, else the executable's default"""
    words = args.split()
    exe   = words[1] if len(words) > 1 and os.path.basename(words[0]).startswith('python') else words[0]

    if not LIB_PATH.search(exe):
        return None

    remap = next((word.split(':=', 1)[1] for word in words if word.startswith(('__node:=', '__name:='))), None)
    base  = os.path.basename(exe)

    return remap or NODE_DEFAULTS.get(base, base.removesuffix('.py').removesuffix('_node')), exe


def launch_files(args: str) -> set[str]:
    """The package and launch file names of a ros2 launch process, empty for any other process"""
    words = args.split()

    for index, word in enumerate(words[:2]):
        if os.path.basename(word) == 'ros2' and words[index + 1:index + 2] == ['launch']:
            return {os.path.basename(word) for word in words[index + 2:index + 4]}

    return set()


def orphaned(table: dict[int, dict], proc: dict) -> bool:
    """The parent is init or the user's systemd, which adopt the processes of a closed shell or a nohup"""
    parent = table.get(proc['ppid'])

    return proc['ppid'] == 1 or (parent is not None and parent['args'].split()[0].endswith('systemd') and '--user' in parent['args'])


def origin(table, pid, launch_file):
    """(started by the stack's launch, how it was started)"""
    proc = table[pid]

    if orphaned(table, proc):
        return False, 'orphaned (nohup or a closed shell)'

    while proc['ppid'] in table:
        proc  = table[proc['ppid']]
        files = launch_files(proc['args'])
        if files:
            return launch_file in files, f'from ros2 launch {" ".join(sorted(files))}'
        if os.path.basename(proc['args'].split()[0]).startswith('tmux'):
            return False, 'from tmux'

    parent = table.get(table[pid]['ppid'])

    return False, f'from {parent["args"][:40]}' if parent else 'from an unknown parent'


def workspace(path):
    path = re.sub(r'^/home/[^/]+', '~', path)

    return path.split('/install/')[0] + '/install' if '/install/' in path else path.split('/lib/')[0]


def stack_nodes(tables, hosts=HOSTS):
    """(host, process, node name, executable) of each robot-control node and each node from the stack's install"""
    return [(host, proc, *node) for host, table in tables.items() for proc in table.values()
            if (node := ros_node(proc['args'])) and (node[0] in CONTROL_NODES or node[1].startswith(hosts[host]['install']))]


def duplicate_findings(node_names, nodes):
    """A control node in two processes, or a node name twice in the graph: fail for control nodes, else warn"""
    copies = defaultdict(list)

    for host, proc, name, path in nodes:
        copies[name].append(f'{host} pid {proc["pid"]} {workspace(path)}')

    findings = [('fail', f'{name} runs {len(where)} times: {"; ".join(where)}')
                for name, where in copies.items() if name in CONTROL_NODES and len(where) > 1]

    for full_name, count in sorted(Counter(node_names or []).items()):
        name = full_name.rsplit('/', 1)[-1]
        if count > 1 and not (name in CONTROL_NODES and len(copies[name]) > 1):
            findings.append(('fail' if name in CONTROL_NODES else 'warn', f'{full_name} is {count} times in the ROS graph'))

    return findings


def stray_findings(tables, nodes, hosts=HOSTS):
    """A stack node outside its host's launch (a stack restart would start a second copy), or from another workspace"""
    findings = []

    for host, proc, name, path in nodes:
        launch      = hosts[host]['launch']
        owned, how  = origin(tables[host], proc['pid'], launch)
        foreign     = '/install/' in path and not path.startswith(hosts[host]['install'])
        if owned and not foreign:
            continue
        parts = ([f'runs outside {launch}, {how}'] if not owned else []) + ([f'comes from {workspace(path)}'] if foreign else [])
        text  = f'{name} ({host} pid {proc["pid"]}) ' + ' and '.join(parts)
        findings.append(('warn', text + ('; a stack restart would start a second copy' if not owned else '')))

    return findings


def orphan_findings(tables, helpers=HELPERS):
    """Helpers adopted by init past their grace age; each holds a DDS participant"""
    findings = []

    for host, table in tables.items():
        for proc in table.values():
            helper = next((pattern for pattern in helpers if pattern in proc['args']), None)
            if (helper and proc['age_s'] > helpers[helper] and orphaned(table, proc)
                    and os.path.basename(proc['args'].split()[0]) not in SHELLS):
                findings.append(('warn', f'orphaned {helper} ({host} pid {proc["pid"]}, {proc["age_s"] // 60} min old)'))

    return findings


def judge(node_names: list[str] | None, tables: dict[str, dict[int, dict]], hosts: dict = HOSTS) -> list[tuple[str, str]]:
    """Findings, each ('fail' | 'warn', text), failures last: the command runner keeps the tail of the output"""
    nodes    = stack_nodes(tables, hosts)
    findings = duplicate_findings(node_names, nodes) + stray_findings(tables, nodes, hosts) + orphan_findings(tables)
    unread   = ([('warn', 'no ROS graph: ros2 node list failed')] if node_names is None else []) + \
               [('warn', f'no process list from {host}') for host in hosts if host not in tables]

    return sorted(unread + findings, key=lambda finding: finding[0] == 'fail')


def read_node_names():
    try:
        done = subprocess.run(['timeout', str(TIMEOUT_S), 'ros2', 'node', 'list'], capture_output=True, text=True,
                              timeout=TIMEOUT_S + 5)
    except (OSError, subprocess.TimeoutExpired):
        return None

    return [line.strip() for line in done.stdout.splitlines() if line.startswith('/')] if done.returncode == 0 else None


def read_ps(ssh=None):
    """The process table here, or at ssh; ssh aborts with the executor's preloaded DDS libraries"""
    command = ['ps', '-eo', PS_FIELDS]
    command = command if ssh is None else ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', ssh, ' '.join(command)]
    env     = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}

    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None

    return parse_ps(done.stdout) if done.returncode == 0 else None


def main():
    """python3 -m orchestrator.health.stale_nodes duplicates | strays | report: exit 1 on a fail (duplicates) or warn (strays)"""
    modes = {'duplicates': {'fail'}, 'strays': {'warn'}, 'report': {'fail', 'warn'}}

    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        sys.exit(f'usage: python3 -m orchestrator.health.stale_nodes {" | ".join(modes)}')

    node_names = read_node_names()
    tables     = {host: table for host, spec in HOSTS.items() if (table := read_ps(spec['ssh'])) is not None}
    findings   = [finding for finding in judge(node_names, tables) if finding[0] in modes[sys.argv[1]]]

    for level, text in findings:
        print(f'{level}: {text}')

    if not findings:
        print(f'none found: {len(node_names or [])} nodes in the graph, processes on {", ".join(tables) or "no host"}')

    sys.exit(1 if findings and sys.argv[1] != 'report' else 0)


if __name__ == '__main__':
    main()
