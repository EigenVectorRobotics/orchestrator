"""Run one agent code file under an audit hook: no ROS, no network, no new processes, writes only in the scratch directory.

The sandbox runs `python3 -I agent_guard.py <code file>`. This is a guard against mistakes, not a security boundary:
code that means harm can get around an audit hook (e.g. through ctypes). The NUC allows no user namespaces, so
bubblewrap and unshare cannot give a real one.
"""

import os
import sys
import runpy
import fnmatch
import resource

SCRATCH         = os.path.realpath(os.environ['AGENT_SCRATCH'])
ROS_MODULES     = {'rclpy', 'rcl', 'rmw', 'rosidl_runtime_py', 'tf2_ros', 'launch', 'launch_ros', 'rosbag2_py'}
BLOCKED_MODULES = ROS_MODULES | set(filter(None, os.environ.get('AGENT_DENIED_MODULES', '').split(':')))  # + deny list
PROCESS_EVENTS  = {
    'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork', 'os.forkpty',
    'pty.spawn', 'os.kill', 'os.killpg',
}
NETWORK_EVENTS  = {'socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg'}
PATH_EVENTS     = {
    'os.remove', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown', 'os.symlink', 'os.link',
    'os.truncate', 'shutil.rmtree', 'os.utime',
}
WRITE_FLAGS     = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
HIDDEN          = list(filter(None, os.environ.get('AGENT_HIDDEN', '').split(':')))   # not read, not listed
LIST_EVENTS     = {'os.listdir', 'os.scandir'}                                       # glob and os.walk use scandir


def _real(path) -> str | None:
    """The real path, or None for an open file descriptor (its path was checked when it opened). The whole path,
    every time: a symlink can be the last part, and a folder can become a symlink after it was checked"""
    if isinstance(path, int):
        return None

    return os.path.realpath(os.fsdecode(path))


def _hidden(path) -> bool:
    real = _real(path)

    return real is not None and any(fnmatch.fnmatchcase(real, pattern) for pattern in HIDDEN)


def _inside_scratch(path) -> bool:
    real = _real(path)

    return real is None or real == SCRATCH or real.startswith(SCRATCH + os.sep) or real == os.devnull


def _hook(event: str, args: tuple):
    if event == 'import':
        name = args[0] or ''

        if name.split('.')[0] in BLOCKED_MODULES:
            raise ImportError(f'{name} is blocked in the agent sandbox: use the ros_read tool for ROS data; the '
                              'Unitree SDK and raw DDS are on the deny list')

    elif event == 'open':
        path, mode, flags = args
        writing = any(letter in mode for letter in 'wax+') if mode else bool(flags & WRITE_FLAGS)

        if writing and not _inside_scratch(path):
            raise PermissionError(f'the agent sandbox writes only under {SCRATCH}, not {path}')

        if _hidden(path):
            raise PermissionError(f'{path} is hidden from the agent sandbox')

    elif event in LIST_EVENTS:
        if args and args[0] is not None and _hidden(args[0]):
            raise PermissionError(f'{args[0]} is hidden from the agent sandbox')

    elif event in PROCESS_EVENTS:
        raise PermissionError(f'{event} is blocked in the agent sandbox')

    elif event in NETWORK_EVENTS:
        raise PermissionError(f'{event} is blocked in the agent sandbox: no network')

    elif event in PATH_EVENTS:
        paths = [arg for arg in args[:2] if isinstance(arg, (str, bytes, os.PathLike))]

        if not all(_inside_scratch(path) for path in paths):
            raise PermissionError(f'{event} is allowed only under {SCRATCH}')


def _limit_resources():
    """Set here, not by the parent: preexec_fn is unsafe in the executor, which runs threads"""
    os.nice(19)
    memory = int(os.environ.get('AGENT_MEMORY_MB', '4096')) * 1024 * 1024
    cpu_s  = int(os.environ.get('AGENT_CPU_S', '65'))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))


def main():
    code_path = sys.argv[1]

    _limit_resources()

    for path in reversed([path for path in os.environ.get('AGENT_PYTHONPATH', '').split(':') if path]):
        sys.path.insert(0, path)

    sys.path.insert(0, SCRATCH)                     # helpers the agent saved earlier
    sys.addaudithook(_hook)
    sys.argv = [code_path]

    runpy.run_path(code_path, run_name='__main__')


if __name__ == '__main__':
    main()
