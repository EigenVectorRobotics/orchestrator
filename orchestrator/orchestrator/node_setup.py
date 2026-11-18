"""Parameters and loading shared by the executor and recorder nodes"""

import yaml

from pathlib import Path
from typing import Callable

from rclpy.node import Node

from skill_registry import DEFAULT_ESSENTIAL_DEVICES, Registry, agent_contracts_dir, missing_folder

from .places import parse_places
from .map_places import MapInfoPlaces


def load_places(path: str) -> dict:
    """Named map-frame poses, put on the blackboard by name; either places format (see places.py)"""
    if not path or not Path(path).exists():
        return {}

    with open(path) as file:
        return parse_places(yaml.safe_load(file))


def declare_sources(node: Node) -> tuple[str, str, str]:
    """Declare contracts_directory, places_file and map_info: where every node loads its skills and places from.
    A places_file is used when it is set; else the places come from the map_info node (e.g. /map_info), live"""
    contracts_directory = node.declare_parameter('contracts_directory', str(agent_contracts_dir())).value
    places_file         = node.declare_parameter('places_file', '').value

    map_info            = node.declare_parameter('map_info', '/map_info').value

    return contracts_directory, places_file, map_info


def load_skills(node: Node) -> tuple[Registry, Callable[[], dict], set[str]]:
    """Declare the sources and essential_devices; return the registry, the places now (a call) and the devices"""
    contracts_directory, places_file, map_info = declare_sources(node)
    essential_devices                          = node.declare_parameter('essential_devices', sorted(DEFAULT_ESSENTIAL_DEVICES)).value
    file_places                                = load_places(places_file)
    places                                     = (lambda: file_places) if places_file or not map_info else MapInfoPlaces(node, map_info)

    if warning := missing_folder(contracts_directory):
        node.get_logger().warning(warning)

    return Registry.from_directory(Path(contracts_directory)), places, set(essential_devices)
