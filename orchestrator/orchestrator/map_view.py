"""The Map panel of Mission Control: the floor plan of the robot's floor (floor_plan.py), its places, the robot, and
the route it follows.

No ROS here: the node gives the places (map_places.MapInfoPlaces), the pose ({x, y, yaw}, geometry.pose_dict of
/odom_base_link), the floor (/slam_elevatorlio/floor) and the route (/route_planner/path, as (x, y) points). The page
draws it to scale; this module keeps the latest of each and says how old the pose is.
"""

import re
import math
import time
import threading


MAX_ROUTE   = 200                   # route points the page gets: a long path is thinned, its ends kept
STALE_POSE  = 3.0                   # seconds: an older pose is said to be old
SAME_FLOOR  = 1.5                   # metres of z: a route point this near the robot's height is on its floor
POSE_EVERY  = 0.2                   # seconds: /odom_base_link comes at 50-200 Hz; the page reads one pose a second


def floor_number(floor) -> int | None:
    """The number of a map_info floor id ('floor 2' -> 2; map_info names its floors so), or None when it has none.
    /slam_elevatorlio/floor gives the same number (1 = lowest)"""
    found = re.search(r'(-?\d+)\s*$', str(floor or ''))

    return int(found.group(1)) if found else None


class MapView:
    def __init__(self, clock=time.time):
        self.clock   = clock
        self._lock   = threading.Lock()
        self._places = {}
        self._pose   = None             # (time, {x, y, z, yaw})
        self._floor  = None
        self._route  = []               # [x, y, z]

    def put_places(self, places: dict):
        """Blackboard key -> {x, y, yaw, floor, kind, ...} (places.parse_places); floor is a map_info id there"""
        with self._lock:
            self._places = dict(places)

    def wants_pose(self) -> bool:
        """False while the last pose is newer than POSE_EVERY: a caller can drop the message unread"""
        with self._lock:
            return self._pose is None or self.clock() - self._pose[0] >= POSE_EVERY

    def put_pose(self, pose: dict):
        with self._lock:
            self._pose = (self.clock(), {'x': pose['x'], 'y': pose['y'], 'z': pose.get('z'), 'yaw': pose['yaw']})

    def put_floor(self, floor: int):
        with self._lock:
            self._floor = floor

    def put_route(self, points: list[tuple[float, float, float]]):
        """The route as (x, y, z): a route through the elevator has points on two floors"""
        with self._lock:
            self._route = [list(point) for point in points]

    def state(self, plans: dict | None = None) -> dict:
        """Every place with its floor, the robot with the age of its pose and the nearest place of its floor, the route
        thinned with the floor of each point, and the floor plan to show (plans: FloorPlans.floors()). The page shows
        one floor, the robot's or one you pick: it keeps the places and route points of that floor"""
        now   = self.clock()
        plans = plans or {'floors': {}, 'problem': ''}

        with self._lock:
            places, pose, floor, route = self._places, self._pose, self._floor, self._route

        where  = pose and pose[1]
        shown  = [{'name': name, 'x': place['x'], 'y': place['y'], 'kind': place.get('kind', ''),
                   'floor': place.get('floor') or ''} for name, place in places.items()]
        robot  = None

        if pose is not None:
            # while the floor is not known, a place right above or below would read as near: name none
            ours    = [place for place in shown if floor_number(place['floor']) in (None, floor)]
            nearest = min(ours, key=lambda place: math.dist((place['x'], place['y']), (where['x'], where['y'])),
                          default=None) if floor is not None else None
            robot   = {'x': where['x'], 'y': where['y'], 'yaw': where['yaw'], 'age_s': round(now - pose[0], 1),
                       'old': now - pose[0] > STALE_POSE,
                       'nearest': nearest and {'name': nearest['name'], 'distance': round(math.dist(
                           (nearest['x'], nearest['y']), (where['x'], where['y'])), 2)}}

        step  = max(1, math.ceil(len(route) / MAX_ROUTE))
        route = route[::step] + ([route[-1]] if route and (len(route) - 1) % step else [])

        return {'places': shown, 'robot': robot, 'floor': floor,
                'route': [[point[0], point[1], _floor_of(plans['floors'], point, where)] for point in route],
                'plan': {**plans, 'floor': _plan_floor(plans['floors'], floor, where)}}


def _floor_of(floors: dict, point: list, where: dict | None) -> str | None:
    """The floor of a route point: the floor whose z range holds it (z_min <= z <= z_max, as map_info's floor_of), ''
    in none (the elevator shaft). With no floor ranges: 'robot' when it is near the robot's height, '' when it is not,
    None when z or the robot is not known"""
    if len(point) < 3:
        return None

    if floors:
        return next((name for name, (low, high) in floors.items() if low <= point[2] <= high), '')

    return None if not where or where.get('z') is None else 'robot' if abs(point[2] - where['z']) <= SAME_FLOOR else ''


def _plan_floor(floors: dict, number: int | None, where: dict | None) -> str | None:
    """The floor plan to show: the one SLAM says the robot is on, else the one whose z range holds the robot, else the
    first"""
    names     = sorted(floors)
    by_number = [name for name in names if floor_number(name) == number] if number is not None else []
    by_z      = [name for name in names if floors[name][0] <= where['z'] <= floors[name][1]] \
                if where and where.get('z') is not None else []

    return next(iter(by_number + by_z + names), None)
