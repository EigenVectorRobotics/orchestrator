"""Named map poses for the blackboard, from either places file format.

  this package's format     name: {x, y, yaw}                     yaw in radians, frame map
  the team's map format     places: {Name With Spaces: {x, y, yaw_deg, ...}}, with frame at the top
                            (e.g. ~/maps/two_floor_clean.places.yaml, saved with the map)

A name becomes its blackboard key in lower case with underscores: "freight elevator call panel" is
{freight_elevator_call_panel}. z is kept when a place has it: on a map of several storeys the route
planner picks the storey by the goal's z, and a goal with z 0 snaps to the lowest one. kind, floor
and note are kept when a place has them (map_info gives all three), for the task agent. Other fields
are ignored.
"""

import re
import math


PLACE_INFO = ('kind', 'floor', 'note')    # for the task agent; the skills read only the pose


class PlacesError(ValueError):
    pass


def place_key(name: str) -> str:
    """The blackboard key of a place name. map_info finds a place by the same key (map_info annotations.py place_key):
    keep the two in step"""
    return re.sub(r'[^0-9a-z]+', '_', str(name).strip().lower()).strip('_')


def parse_places(raw: dict | None) -> dict:
    """Blackboard key -> {x, y, yaw (radians), frame}, and z, kind, floor and note when the place has them"""
    if not raw:
        return {}

    team_format = isinstance(raw.get('places'), dict)
    entries     = raw['places'] if team_format else raw
    frame       = raw.get('frame', 'map') if team_format else 'map'
    places      = {}

    for name, pose in entries.items():
        key = place_key(name)

        if not isinstance(pose, dict) or 'x' not in pose or 'y' not in pose:
            raise PlacesError(f'place {name!r} needs x and y')

        if key in places:
            raise PlacesError(f'two places become the key {key}')

        if 'yaw' in pose:
            yaw = float(pose['yaw'])
        else:
            yaw = math.radians(float(pose.get('yaw_deg', 0.0)))

        places[key] = {'x': float(pose['x']), 'y': float(pose['y']), 'yaw': yaw, 'frame': pose.get('frame', frame)}

        if 'z' in pose:
            places[key]['z'] = float(pose['z'])

        places[key] |= {field: str(pose[field]) for field in PLACE_INFO if pose.get(field)}

    return places
