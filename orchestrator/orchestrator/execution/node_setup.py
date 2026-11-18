"""Parameters and loading shared by the executor and planner nodes"""

import yaml

from pathlib import Path

from rclpy.node import Node
from ament_index_python.packages import get_package_share_directory

from skill_registry import DEFAULT_ESSENTIAL_DEVICES, DEFAULT_SKILL_PACKAGES, Registry, contract_directories

from ..common.places import parse_places, place_floors
from ..skills.loader import SkillLibrary


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


def declare_sources(node: Node) -> tuple[list[Path], str, list[str]]:
    """Declare skill_packages, contracts_directory and places_file: where every node loads its skills and places from.
    The contracts are the skill packages' folders, unless contracts_directory names one folder instead"""
    own_share      = Path(get_package_share_directory('orchestrator'))
    skill_packages = list(node.declare_parameter('skill_packages', DEFAULT_SKILL_PACKAGES).value)
    contracts      = node.declare_parameter('contracts_directory', '').value
    places_file    = node.declare_parameter('places_file', str(own_share / 'config' / 'places.yaml')).value

    return contract_directories(skill_packages, contracts), places_file, skill_packages


def load_skills(node: Node) -> tuple[Registry, dict, set[str], SkillLibrary]:
    """Declare the sources and essential_devices; return the registry, places, devices and the skills' code"""
    directories, places_file, skill_packages = declare_sources(node)
    essential_devices                        = node.declare_parameter('essential_devices', sorted(DEFAULT_ESSENTIAL_DEVICES)).value

    return Registry.from_directories(directories), load_places(places_file), set(essential_devices), SkillLibrary(skill_packages)
