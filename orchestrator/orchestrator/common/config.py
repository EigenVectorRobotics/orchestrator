"""Where the robot keeps its files, named places, and site profiles. No ROS here: the agent bridge loads sites too.

Places: named map poses for the blackboard, from either places file format.
  this package's format     name: {x, y, yaw}                     yaw in radians, frame map
  the team's map format     places: {Name With Spaces: {x, y, yaw_deg, ...}}, with frame at the top
                            (e.g. ~/maps/two_floor_clean_cabin2.places.yaml, saved with the map)

A name becomes its blackboard key in lower case with underscores: "freight elevator call panel" is
{freight_elevator_call_panel}. z is kept when a place has it: on a map of several storeys the route
planner picks the storey by the goal's z, and a goal with z 0 snaps to the lowest one. Fields other
than x, y, z and the yaw are ignored.

The team's format may also give floors: {Floor Name: {z_min, z_max}} by the z of a place (map_info floor_of).
place_floors gives each place the floor its z falls in, as the number in the floor's name ("floor 2" -> "2").

Sites: the values a robot run needs that are not code defaults, in one YAML file (config/sites/<name>.yaml).
  places_file      the places file the nodes load
  planner_backend  claude | openai_compatible | scripted | external
  planner_model    the planner's model
  agent_bridge     {host, port}: the operator laptop that runs mission_control/agent_bridge.py
  raw_bag          {dir, topics}: a raw bag recorded beside the orchestrator

orchestrator.launch.py reads it (site:=<name> or a path); a launch argument given on the command line wins.
mission_control/agent_bridge.py reads the same file for its listen address. This module needs no ROS.
"""

import re
import math

from pathlib import Path

import yaml


ARTIFACTS_DIR = str(Path.home() / '.ros' / 'orchestrator_captures')   # files skills save for the agent and the recorder
STATE_DIR     = Path.home() / 'orch_state'                             # what the robot keeps across runs
RUNS_DIR      = Path.home() / 'orch_runs'                              # one folder per recorded run


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


KEYS = {'places_file', 'planner_backend', 'planner_model', 'agent_bridge', 'raw_bag'}


class SiteError(ValueError):
    pass


def site_path(site: str, directory: str | Path) -> Path:
    """A site name is a file in directory; a value with a slash or ending in .yaml is a path"""
    return Path(site).expanduser() if '/' in site or site.endswith('.yaml') else Path(directory) / f'{site}.yaml'


def load_site(site: str, directory: str | Path) -> dict:
    """The site's values, {} for no site; an unknown key or a missing file is an error"""
    if not site:
        return {}

    path = site_path(site, directory)

    if not path.is_file():
        known = sorted(file.stem for file in Path(directory).glob('*.yaml')) if Path(directory).is_dir() else []
        raise SiteError(f'no site file {path}' + (f' (sites: {", ".join(known)})' if known else ''))

    values  = yaml.safe_load(path.read_text()) or {}
    unknown = sorted(set(values) - KEYS)

    if unknown:
        raise SiteError(f'{path}: unknown keys {unknown}; known: {sorted(KEYS)}')

    for name in ('agent_bridge', 'raw_bag'):
        if name in values and not isinstance(values[name], dict):
            raise SiteError(f'{path}: {name} must be a mapping')

    return values
