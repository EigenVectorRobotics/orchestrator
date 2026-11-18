"""A site: the values a robot run needs that are not code defaults, in one YAML file (config/sites/<name>.yaml).

  places_file      the places file the nodes load
  planner_backend  claude | openai_compatible | scripted | external
  planner_model    the planner's model
  agent_bridge     {host, port}: the operator laptop that runs mission_control/agent_bridge.py
  raw_bag          {dir, topics}: a raw bag recorded beside the orchestrator

orchestrator.launch.py reads it (site:=<name> or a path); a launch argument given on the command line wins.
mission_control/agent_bridge.py reads the same file for its listen address. This module needs no ROS.
"""

from pathlib import Path

import yaml


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
