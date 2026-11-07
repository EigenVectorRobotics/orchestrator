"""Figures of one FVD sim run from its MCAP bag: the path on both floors, the elevator entry, and the timeline.

python3 run_report.py <bag_dir> <places.yaml> <map.pcd> <output_dir>
"""
import json
import math
import os
import sys

import matplotlib
import numpy as np
import rosbag2_py
import yaml

from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message

from slam_elevatorlio.map_io import read_pcd_xyz

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

TOPICS = ["/sim/state", "/odom_base_link", "/task_executor/status", "/door_state/landing", "/door_state/car",
          "/elevator/floor", "/navigation_cmd_vel", "/local_movement_cmd_vel", "/route_planner/path", "/path"]


def read_bag(
    path: str,
) -> dict:
    """Topic -> list of (time [s], message)."""
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=path, storage_id="mcap"), rosbag2_py.ConverterOptions("", ""))
    types = {topic.name: topic.type for topic in reader.get_all_topics_and_types()}
    reader.set_filter(rosbag2_py.StorageFilter(topics=[topic for topic in TOPICS if topic in types]))
    data = {topic: [] for topic in TOPICS}
    while reader.has_next():
        topic, raw, stamp = reader.read_next()
        data[topic].append((stamp * 1e-9, deserialize_message(raw, get_message(types[topic]))))

    return data


def steps(
    data: dict,
) -> list[tuple[float, str, str]]:
    """(time, step, state) at each step change of the task executor."""
    changes, last = [], None
    for stamp, message in data["/task_executor/status"]:
        status = json.loads(message.data)
        key = (status.get("step"), status.get("state"))
        if key != last and status.get("step"):
            changes.append((stamp, status["step"], status["state"]))
            last = key

    return changes


def draw_poses(
    axis,
    places: dict,
    floor: str,
) -> None:
    """The annotated poses of one floor and the car, as arrows."""
    poses = {name: pose for name, pose in places["floors"][floor].items() if isinstance(pose, dict) and "yaw" in pose}
    poses.update({f"car {name}": pose for name, pose in places["car"].items() if isinstance(pose, dict)})
    poses.update({f"target {name}": pose for name, pose in places["floors"][floor]["targets"].items()})
    for name, pose in poses.items():
        axis.arrow(pose["x"], pose["y"], 0.25 * math.cos(pose["yaw"]), 0.25 * math.sin(pose["yaw"]),
                   head_width=0.08, color="tab:red", zorder=5)
        axis.text(pose["x"] + 0.05, pose["y"] + 0.12, name, fontsize=7, color="tab:red", zorder=5, clip_on=True)


def main() -> None:
    bag, places_path, map_path, output = sys.argv[1:5]
    os.makedirs(output, exist_ok=True)
    with open(places_path) as file:
        places = yaml.safe_load(file)
    data = read_bag(bag)
    changes = steps(data)
    t0 = changes[0][0] if changes else data["/sim/state"][0][0]

    truth = np.array([[stamp - t0, *json.loads(message.data)["robot"], json.loads(message.data)["lift_z"]]
                      for stamp, message in data["/sim/state"]])
    odom = np.array([[stamp - t0, message.pose.pose.position.x, message.pose.pose.position.y, message.pose.pose.position.z]
                     for stamp, message in data["/odom_base_link"]])
    running = (truth[:, 0] >= 0) & (truth[:, 0] <= (changes[-1][0] - t0 + 5 if changes else 1e9))
    points = read_pcd_xyz(map_path)

    # 1: the path on both floors
    figure, axes = plt.subplots(1, 2, figsize=(18, 9))
    for axis, (floor, info) in zip(axes, places["floors"].items()):
        band = (points[:, 2] > info["ground_z"] + 0.3) & (points[:, 2] < info["ground_z"] + 2.0)
        axis.scatter(points[band, 0], points[band, 1], s=0.2, c="0.6")
        on_floor = running & (np.abs(truth[:, 3] - info["ground_z"] - 0.78) < 0.3)
        axis.scatter(truth[on_floor, 1], truth[on_floor, 2], s=2, c=truth[on_floor, 0], cmap="viridis")
        draw_poses(axis, places, floor)
        axis.set_title(f"floor {floor}: robot path (truth, colour = time)")
        axis.set_aspect("equal")
        axis.grid(True)
    figure.tight_layout()
    figure.savefig(os.path.join(output, "path_both_floors.png"), dpi=80)

    # 2: the elevator entry, from the door front to the panel
    entry = [stamp for stamp, step, state in changes if step.startswith("4")]
    press = [stamp for stamp, step, state in changes if step.startswith("5")]
    if entry and press:
        start, end = entry[0] - t0 - 2.0, press[0] - t0 + 1.0
        window = (truth[:, 0] > start) & (truth[:, 0] < end) & (truth[:, 3] < 2.0)
        figure, (top, speed) = plt.subplots(1, 2, figsize=(18, 8))
        band = (points[:, 2] > 0.3) & (points[:, 2] < 2.0) & (np.abs(points[:, 0]) < 2.0) & (points[:, 1] > -2.0) & (points[:, 1] < 3.0)
        top.scatter(points[band, 0], points[band, 1], s=0.5, c="0.6")
        top.plot(truth[window, 1], truth[window, 2], "k-", lw=1.5, label="robot (truth)")
        for row in truth[window][::5]:
            top.arrow(row[1], row[2], 0.12 * math.cos(row[4]), 0.12 * math.sin(row[4]), head_width=0.03, color="tab:green")
        for stamp, message in data["/route_planner/path"]:
            if start < stamp - t0 < end and message.poses:
                route = np.array([[pose.pose.position.x, pose.pose.position.y] for pose in message.poses])
                top.plot(route[:, 0], route[:, 1], "b--", lw=1, alpha=0.6)
        for stamp, message in data["/path"][::20]:
            if start < stamp - t0 < end and len(message.poses) > 1 and len(odom):
                pose = odom[np.argmin(np.abs(odom[:, 0] - (stamp - t0)))]
                yaw = truth[np.argmin(np.abs(truth[:, 0] - (stamp - t0))), 4]
                local = np.array([[p.pose.position.x, p.pose.position.y] for p in message.poses])
                world = pose[1:3] + local @ np.array([[math.cos(yaw), math.sin(yaw)], [-math.sin(yaw), math.cos(yaw)]])
                top.plot(world[:, 0], world[:, 1], "m-", lw=0.6, alpha=0.5)
        draw_poses(top, places, list(places["floors"])[0])
        top.set_xlim(-1.6, 1.6)
        top.set_ylim(-1.8, 2.8)
        top.set_aspect("equal")
        top.grid(True)
        top.set_title("entry: robot (black, heading green), route planner (blue dashed), local planner paths (magenta)")
        top.legend(loc="lower left")
        for topic, colour in (("/navigation_cmd_vel", "tab:blue"), ("/local_movement_cmd_vel", "tab:red")):
            values = np.array([[stamp - t0, message.twist.linear.x, message.twist.linear.y, message.twist.angular.z]
                               for stamp, message in data[topic] if start < stamp - t0 < end])
            if len(values):
                speed.plot(values[:, 0], values[:, 1], "-", c=colour, lw=1, label=f"{topic} vx")
                speed.plot(values[:, 0], values[:, 2], ":", c=colour, lw=1, label=f"{topic} vy")
                speed.plot(values[:, 0], values[:, 3], "--", c=colour, lw=0.8, label=f"{topic} wz")
        rows = truth[window]
        moved = np.hypot(np.diff(rows[:, 1]), np.diff(rows[:, 2])) / np.maximum(np.diff(rows[:, 0]), 1e-3)
        speed.plot(rows[1:, 0], moved, "k-", lw=1, label="robot speed (truth)")
        for stamp, step, state in changes:
            if start < stamp - t0 < end:
                speed.axvline(stamp - t0, c="0.5", lw=0.5)
                speed.text(stamp - t0, 0.9, step, rotation=90, fontsize=7, va="top")
        speed.set_xlabel("time from the command [s]")
        speed.set_ylabel("m/s, rad/s")
        speed.grid(True)
        speed.legend(fontsize=7)
        figure.tight_layout()
        figure.savefig(os.path.join(output, "elevator_entry.png"), dpi=80)

    # 3: timeline
    figure, (axis_z, axis_steps) = plt.subplots(2, 1, figsize=(18, 9), sharex=True)
    axis_z.plot(truth[running, 0], truth[running, 3] + 0.1, "k-", label="pelvis z + 0.1 (truth)")
    axis_z.plot(odom[:, 0], odom[:, 3], "b-", lw=1, label="base_link z (/odom_base_link)")
    axis_z.plot(truth[running, 0], truth[running, 5], "g-", label="car floor z (truth)")
    for topic, offset in (("/door_state/landing", -0.6), ("/door_state/car", -1.2)):
        values = np.array([[stamp - t0, message.data == "OPEN"] for stamp, message in data[topic]], dtype=float)
        if len(values):
            axis_z.plot(values[:, 0], offset + 0.4 * values[:, 1], lw=1, label=f"{topic} (high = OPEN)")
    for stamp, message in data["/elevator/floor"]:
        axis_z.axvline(stamp - t0, c="tab:orange", lw=1)
        axis_z.text(stamp - t0, 4.5, f"floor {message.data}", color="tab:orange", fontsize=8)
    axis_z.set_ylabel("m")
    axis_z.grid(True)
    axis_z.legend(fontsize=8, loc="upper left")
    axis_z.set_xlim(0, truth[running, 0].max())
    for index, (stamp, step, state) in enumerate(changes):
        axis_steps.barh(0, (changes[index + 1][0] if index + 1 < len(changes) else stamp + 5) - stamp, left=stamp - t0,
                        color=f"C{index % 10}")
        axis_steps.text(stamp - t0, 0.45 + 0.1 * (index % 3), f"{step} ({state})", fontsize=8)
    axis_steps.set_yticks([])
    axis_steps.set_xlabel("time from the command [s]")
    figure.tight_layout()
    figure.savefig(os.path.join(output, "timeline.png"), dpi=80)

    summary = [{"t": round(stamp - t0, 1), "step": step, "state": state} for stamp, step, state in changes]
    with open(os.path.join(output, "steps.json"), "w") as file:
        json.dump(summary, file, indent=1)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
