"""The small map of Mission Control: the places of the robot's floor, the robot, and the route it follows.

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

    def state(self) -> dict:
        """The places of the robot's floor (all places while the floor is not known), the robot with the age of its
        pose and the nearest place of its floor, and the route on its floor, thinned. {} while there is nothing"""
        now = self.clock()

        with self._lock:
            places, pose, floor, route = self._places, self._pose, self._floor, self._route

        shown = [{'name': name, 'x': place['x'], 'y': place['y'], 'kind': place.get('kind', '')}
                 for name, place in places.items()
                 if floor is None or floor_number(place.get('floor')) in (None, floor)]

        if not shown and pose is None:
            return {}

        robot = None

        if pose is not None:
            at, where = pose
            # while the floor is not known, a place right above or below would read as near: name none
            nearest   = min(shown, key=lambda place: math.dist((place['x'], place['y']), (where['x'], where['y'])),
                            default=None) if floor is not None else None
            robot     = {'x': where['x'], 'y': where['y'], 'yaw': where['yaw'], 'age_s': round(now - at, 1),
                         'old': now - at > STALE_POSE,
                         'nearest': nearest and {'name': nearest['name'], 'distance': round(math.dist(
                             (nearest['x'], nearest['y']), (where['x'], where['y'])), 2)}}

            if where.get('z') is not None:          # the route on the robot's floor only
                route = [point for point in route if len(point) < 3 or abs(point[2] - where['z']) <= SAME_FLOOR]

        step  = max(1, math.ceil(len(route) / MAX_ROUTE))
        route = [point[:2] for point in route[::step]] + ([route[-1][:2]] if route and (len(route) - 1) % step else [])

        return {'places': shown, 'robot': robot, 'floor': floor, 'route': route}
