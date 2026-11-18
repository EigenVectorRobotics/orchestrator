"""Named map poses for the blackboard, from either places file format.

  this package's format     name: {x, y, yaw}                     yaw in radians, frame map
  the team's map format     places: {Name With Spaces: {x, y, yaw_deg, ...}}, with frame at the top
                            (e.g. ~/maps/two_floor_clean_cabin2.places.yaml, saved with the map)

A name becomes its blackboard key in lower case with underscores: "freight elevator call panel" is
{freight_elevator_call_panel}. z is kept when a place has it: on a map of several storeys the route
planner picks the storey by the goal's z, and a goal with z 0 snaps to the lowest one. Fields other
than x, y, z and the yaw are ignored.

The team's format may also give floors: {Floor Name: {z_min, z_max}} by the z of a place (map_info floor_of).
place_floors gives each place the floor its z falls in, as the number in the floor's name ("floor 2" -> "2").
"""

import re
import math


FLOOR_NUMBER = re.compile(r'\d+')


class PlacesError(ValueError):
    pass


def place_key(name: str) -> str:
    return re.sub(r'[^0-9a-z]+', '_', str(name).strip().lower()).strip('_')


def parse_places(raw: dict | None) -> dict:
    """Blackboard key -> {x, y, yaw (radians), frame}"""
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

    return places


def place_floors(raw: dict | None) -> dict:
    """Blackboard key -> floor number as text, for each place whose z falls in a floor of the file's floors table"""
    if not raw or not isinstance(raw.get('places'), dict) or not isinstance(raw.get('floors'), dict):
        return {}

    floors = {}

    for name, pose in raw['places'].items():
        if not isinstance(pose, dict) or 'z' not in pose:
            continue

        for floor, band in raw['floors'].items():
            if float(band['z_min']) <= float(pose['z']) < float(band['z_max']):
                number = FLOOR_NUMBER.search(str(floor))
                floors[place_key(name)] = number.group(0) if number else str(floor)
                break

    return floors
