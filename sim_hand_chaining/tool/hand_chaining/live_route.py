"""The route_planner's route from the robot's own cell, rebuilt from a run bag: the one its carrot is on.

The stock node publishes ~/path once per goal (the route from the robot's cell at that time), so the carrot leaves the
drawn path as the robot moves (doc section 5.1). The G1 config plans on the prior map only (use_live_obstacles false),
so for one goal the Dijkstra tree is fixed, and RoutePlanner.path(robot) at a carrot tick is the route of that tick.
This calls the route_planner package (its RoutePlanner on the cached terrain graph the node loads); it changes nothing.

python3 live_route.py <bag.mcap> <map.pcd>   # check against a bag that recorded these routes live (run 31)
"""
import sys
from pathlib import Path

import numpy as np

from route_planner.planner import RoutePlanner
from route_planner.terrain_graph import TerrainGraph

MIN_COMPONENT = 20  # route_planner's min_component (read only, the G1 config keeps the default)


def load_planner(
    map_pcd: str,
) -> RoutePlanner:
    """RoutePlanner on the terrain graph cache next to the map, as the node loads it."""
    return RoutePlanner(TerrainGraph.load(str(Path(map_pcd).with_suffix(".terrain.npz"))), min_component=MIN_COMPONENT)


def live_routes(
    planner: RoutePlanner,
    goals: list[tuple[float, object]],
    odometry: list[tuple[float, object]],
    ticks: list[float],
) -> list[tuple[float, np.ndarray]]:
    """(tick time, route x, y, z) at each carrot tick where the goal or the robot's cell changed.

    goals: (time, PoseStamped) route goals; odometry: (time, Odometry) of /odom_base_link; ticks: carrot times.
    """
    routes, goal_index, odom_index, last = [], -1, -1, None
    for tick in ticks:
        while goal_index + 1 < len(goals) and goals[goal_index + 1][0] <= tick:
            goal_index += 1
            position = goals[goal_index][1].pose.position
            planner.set_goal((position.x, position.y, position.z if abs(position.z) > 1e-9 else float("nan")))
        while odom_index + 1 < len(odometry) and odometry[odom_index + 1][0] <= tick:
            odom_index += 1
        if goal_index < 0 or odom_index < 0:
            continue

        position = odometry[odom_index][1].pose.pose.position
        robot = np.array([position.x, position.y, position.z])
        node = planner.snap(*robot[:2], z=float(robot[2]), restrict=~planner.blocked)
        if (goal_index, node) != last:
            last = goal_index, node
            route = planner.path(robot)
            routes.append((tick, route if route is not None else np.zeros((0, 3))))

    return routes


def main() -> None:
    """Rebuild the routes of a bag with live ~/path messages (run 31) and compare them at the same times."""
    from mcap_ros2.reader import read_ros2_messages

    topics = ["/route_goal", "/task_executor/goal", "/odom_base_link", "/way_point", "/route_planner/path"]
    data = {topic: [] for topic in topics}
    for message in read_ros2_messages(sys.argv[1], topics=topics):
        data[message.channel.topic].append((message.log_time_ns * 1e-9, message.ros_msg))

    goals = data["/route_goal"] or data["/task_executor/goal"]
    recorded = [(t, np.array([[p.pose.position.x, p.pose.position.y] for p in m.poses])) for t, m in data["/route_planner/path"]]
    rebuilt = live_routes(load_planner(sys.argv[2]), goals, data["/odom_base_link"], [t for t, _ in recorded])
    by_time = dict(rebuilt)
    same, offsets = 0, []
    for t, route in recorded:
        mine = by_time.get(t)
        if mine is None or len(route) == 0:
            continue
        if len(mine) == len(route) and np.allclose(mine[:, :2], route, atol=1e-3):
            same += 1
        else:
            offsets.append(np.abs(len(mine) - len(route)))
    print(f"{len(recorded)} recorded routes, {len(rebuilt)} rebuilt, {same} the same, {len(offsets)} different "
          f"(waypoint count differences {offsets[:10]})")


if __name__ == "__main__":
    main()
