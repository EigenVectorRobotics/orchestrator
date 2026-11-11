"""Parameters and loading shared by the executor and planner nodes"""

import yaml

from pathlib import Path

from rclpy.node import Node
from ament_index_python.packages import get_package_share_directory

from skill_registry import DEFAULT_ESSENTIAL_DEVICES, Registry

from .places import parse_places, place_floors


def load_places(path: str) -> dict:
    """Named map-frame poses, put on the blackboard by name; either places format (see places.py)"""
    if not path or not Path(path).exists():
        return {}

    with open(path) as file:
        return parse_places(yaml.safe_load(file))


def load_place_floors(path: str) -> dict:
    """Blackboard key -> floor number as text, from the places file's floors table (see places.py)"""
    if not path or not Path(path).exists():
        return {}

    with open(path) as file:
        return place_floors(yaml.safe_load(file))


def declare_sources(node: Node) -> tuple[str, str]:
    """Declare contracts_directory and places_file, the files every node loads its skills and places from"""
    registry_share = Path(get_package_share_directory('skill_registry'))
    own_share      = Path(get_package_share_directory('orchestrator'))

    contracts_directory = node.declare_parameter('contracts_directory', str(registry_share / 'contracts')).value
    places_file         = node.declare_parameter('places_file', str(own_share / 'config' / 'places.yaml')).value

    return contracts_directory, places_file


def load_skills(node: Node) -> tuple[Registry, dict, set[str]]:
    """Declare contracts_directory, places_file and essential_devices; return the registry, places and devices"""
    contracts_directory, places_file = declare_sources(node)
    essential_devices                = node.declare_parameter('essential_devices', sorted(DEFAULT_ESSENTIAL_DEVICES)).value

    return Registry.from_directory(Path(contracts_directory)), load_places(places_file), set(essential_devices)
