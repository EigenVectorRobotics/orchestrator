"""Show the robot's behavior tree and its decisions live in a browser."""

import argparse
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from run_viewer.server import serve
from run_viewer.sources import follow_camera, follow_live, follow_states, follow_trace, stop_streams, watch_calls
from run_viewer.store import EventStore


def parse_args() -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--robot', default='vector@vector-nuc', help="ssh host of the recorder, or 'local' when the viewer runs on that host")
    parser.add_argument('--trace', help='trace file on the robot host to show (default: the newest recorder events.jsonl)')
    parser.add_argument('--host', action='append', help='address to listen on; give it more than once for more (default 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--ros-env', default='~/eigen_vector_ws/env.sh', help='ROS environment on the robot host, for the topics')
    parser.add_argument('--trace-topic', action='store_true', help='read the trace from /orchestrator/trace, e.g. a recorder bag played back, not from events.jsonl')
    parser.add_argument('--calls', help='with --trace-topic: folder of agent_responder records (<session>/agent_calls) on the robot host')
    parser.add_argument('--camera-host', help="host of the camera, where its echo runs so the raw image stays there: e.g. 'vector@vector-nuc,unitree@192.168.123.164' (ssh through the NUC); 'none' for no camera (default: --robot)")
    parser.add_argument('--camera-env', help='ROS environment on the camera host (default: --ros-env)')
    return parser.parse_args()


def main() -> None:
    """Start the trace reader and the server."""
    args = parse_args()
    store = EventStore()
    if not args.trace_topic:
        threading.Thread(target=follow_trace, args=(args.robot, store, args.trace), daemon=True).start()
    if args.calls:
        threading.Thread(target=watch_calls, args=(args.robot, store, args.calls), daemon=True).start()
    threading.Thread(target=follow_states, args=(args.robot, store, args.ros_env), daemon=True).start()
    threading.Thread(target=follow_live, args=(args.robot, store, args.ros_env, args.trace_topic), daemon=True).start()
    camera_host = args.camera_host or args.robot
    if camera_host != 'none':
        threading.Thread(target=follow_camera, args=(camera_host, store, args.camera_env or args.ros_env), daemon=True).start()
    hosts = args.host or ['127.0.0.1']
    for host in hosts[1:]:
        threading.Thread(target=serve, args=(store, host, args.port), daemon=True).start()
    print(' '.join(f'open http://{host}:{args.port}' for host in hosts), flush=True)
    signal.signal(signal.SIGTERM, signal.default_int_handler)   # kill stops the viewer as Ctrl+C does
    try:
        serve(store, hosts[0], args.port)
    except KeyboardInterrupt:
        pass
    finally:
        stop_streams()                          # else the ssh streams, and their remote commands, run on


if __name__ == '__main__':
    main()
