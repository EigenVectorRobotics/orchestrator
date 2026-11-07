"""Test of the navigation stack at the elevator places (locomotion nav): the executor's own navigate() walks from the
start to the call panel, then cycles call panel -> car panel -> exit -> call panel with the car door held open. Per leg:
time, /goal_reached or timeout, final pose error (estimate and sim truth) and, at a press pose, whether the button is in
the press region. Each parameter set of local_planner and path_follower is set through their parameter services before
its cycles (the nodes read it at once); no code of the stack is changed, and no button is pressed.

Beside the sim stack with the car door never closing, and without the executor (on vector-pc, vector_pc.env):
    ros2 launch jean_dev/config/fvd_sim.launch.py map:=... localization:=true door_hold_s:=-1.0 lio_config:=...
    python3 jean_dev/tool/hand_chaining/nav_approach_test.py <places.yaml> <sets.yaml> <results.json> [set names]
"""
import json
import math
import sys
import threading
import time
from pathlib import Path

import rclpy
import yaml

from nav_msgs.msg import Odometry
from rclpy.executors import MultiThreadedExecutor
from std_msgs.msg import Int8, String

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "hand_chaining"))
from task_executor import TaskExecutor, TaskFailed, wrap  # noqa: E402

CYCLES = 3  # cycles per parameter set
CYCLE = (("panel", "car", "floor_2"), ("exit", "1", None), ("call_panel", "1", "hall_up"))  # place, where, button


def region(
    node: TaskExecutor,
    button: str,
) -> dict:
    """The button in panel coordinates (estimate), as the executor's reach check: distance, lateral [m], turn [deg], inside."""
    target = node.places["buttons"][button]
    bounds = target.get("region", node.places["press_region"])
    x, y, _, yaw = node.current()
    dx, dy, facing = target["x"] - x, target["y"] - y, target["normal_yaw"] + math.pi
    distance = -(math.cos(target["normal_yaw"]) * dx + math.sin(target["normal_yaw"]) * dy)
    lateral = math.sin(facing) * dx - math.cos(facing) * dy
    turn = math.degrees(wrap(facing - yaw))
    inside = bool(bounds["distance"][0] <= distance <= bounds["distance"][1] and bounds["lateral"][0] <= lateral <= bounds["lateral"][1]
                  and bounds["turn_deg"][0] <= turn <= bounds["turn_deg"][1])

    return {"distance": round(distance, 3), "lateral": round(lateral, 3), "turn_deg": round(turn, 1), "inside": inside}


def leg(
    node: TaskExecutor,
    truth: dict,
    name: str,
    pose: dict,
    floor: str,
    button: str | None,
    speed: float | None,
) -> dict:
    """One navigate() to a places pose; the result and the final errors along and across the goal heading."""
    goal = node.pose_on(floor, pose)
    start = time.monotonic()
    try:
        node.navigate(floor, pose, precise=button is not None, speed=speed)
        reached = True
    except TaskFailed as error:
        node.stop()
        node.settle()
        reached = str(error)
    x, y, _, yaw = node.current()
    dx, dy = x - goal["x"], y - goal["y"]
    c, s = math.cos(goal["yaw"]), math.sin(goal["yaw"])
    result = {"leg": name, "reached": reached, "time": round(time.monotonic() - start, 1),
              "along": round(c * dx + s * dy, 3), "across": round(-s * dx + c * dy, 3),
              "yaw_deg": round(math.degrees(wrap(yaw - goal["yaw"])), 1),
              "truth_xy": [round(v, 3) for v in truth.get("xy", (math.nan, math.nan))]}
    if button:
        result["region"] = region(node, button)

    return result


def main() -> None:
    places_file, sets_file, output = sys.argv[1:4]
    with open(sets_file) as file:
        sets = yaml.safe_load(file)
    names = sys.argv[4:] or list(sets)

    rclpy.init(args=["--ros-args", "-p", f"places:={places_file}", "-p", "locomotion:=nav", "-p", "nav_timeout_s:=60.0"])
    node = TaskExecutor()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()

    truth, state = {}, {}
    node.create_subscription(Odometry, "/sim/ground_truth",
                             lambda m: truth.update(xy=(m.pose.pose.position.x, m.pose.pose.position.y)), 10)
    node.create_subscription(String, "/sim/state", lambda m: state.update(json.loads(m.data)), 10)
    call = node.create_publisher(Int8, "/sim/call", 1)

    places, results = node.places, []
    node.localize("1", places["floors"]["1"]["start"])
    node.wait_for(lambda: call.get_subscription_count() > 0, 10.0)  # a first message before discovery is lost
    call.publish(Int8(data=0))  # the car to floor 1; with door_hold_s -1 its door stays open
    # The sim's door state: the lidar door state needs the car inside in view, and the robot is still at the start
    if not node.wait_for(lambda: min(state.get("doors", {}).get("floor0", [0.0])) > 0.95, 60.0):
        raise SystemExit("the car door did not open")

    first = True
    for name in names:
        node.tune_navigation(sets[name])
        print(f"== {name}: {sets[name]}", flush=True)
        legs = [("call_panel", "1", "hall_up")] if first else []
        first = False
        for _ in range(CYCLES):
            legs += list(CYCLE)
        for place, where, button in legs:
            floor = "1"
            pose = places["car"][place] if where == "car" else places["floors"]["1"][place]
            result = leg(node, truth, place, pose, floor, button, node.door_speed if place in ("panel", "exit") else None)
            result["set"] = name
            results.append(result)
            print(json.dumps(result), flush=True)
            if state.get("fallen"):
                print("the robot fell: stop", flush=True)
                break
        if state.get("fallen"):
            break

    with open(output, "w") as file:
        json.dump(results, file, indent=1)
    executor.shutdown()
    rclpy.try_shutdown()


if __name__ == "__main__":
    main()
