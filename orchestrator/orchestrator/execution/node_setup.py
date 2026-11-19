"""What the executor and planner nodes share: their parameters and loading, and the trace"""

import json
import time

from pathlib import Path

import yaml

from rclpy.node import Node
from rclpy.parameter import Parameter
from std_msgs.msg import String
from rcl_interfaces.msg import Log

from skill_registry import DEFAULT_ESSENTIAL_DEVICES, DEFAULT_SKILL_PACKAGES, Registry, contract_directories

from ..common.config import parse_places, place_floors
from ..common.topics import LOG_TOPIC, TRACE_QOS, TRACE_TOPIC
from ..skills.loader import SkillLibrary


LEVELS = {'debug': Log.DEBUG, 'info': Log.INFO, 'warn': Log.WARN, 'error': Log.ERROR}


def load_places(path: str) -> dict:
    """Named map-frame poses, put on the blackboard by name; either places format (see common/config.py)"""
    if not path or not Path(path).exists():
        return {}

    with open(path) as file:
        return parse_places(yaml.safe_load(file))


def load_place_floors(path: str) -> dict:
    """Blackboard key -> floor number as text, from the places file's floors table (see common/config.py)"""
    if not path or not Path(path).exists():
        return {}

    with open(path) as file:
        return place_floors(yaml.safe_load(file))


def load_place_map(path: str) -> str:
    """The prior map (.pcd) that the places file names in its map key; '' when it names none"""
    with open(path) as file:
        return str((yaml.safe_load(file) or {}).get('map') or '')


def declare_sources(node: Node) -> tuple[list[Path], str, list[str]]:
    """Declare skill_packages, contracts_directory and places_file: where every node loads its skills and places from.
    The contracts are the skill packages' folders, unless contracts_directory names one folder instead"""
    skill_packages = list(node.declare_parameter('skill_packages', DEFAULT_SKILL_PACKAGES).value)
    contracts      = node.declare_parameter('contracts_directory', '').value
    places_file    = node.declare_parameter('places_file', Parameter.Type.STRING).value

    if not places_file or not Path(places_file).is_file():
        raise ValueError(f"places_file must name a places file (the site's places_file, or places_file:=): '{places_file}'")

    return contract_directories(skill_packages, contracts), places_file, skill_packages


def load_skills(node: Node) -> tuple[Registry, dict, set[str], SkillLibrary]:
    """Declare the sources and essential_devices; return the registry, places, devices and the skills' code"""
    directories, places_file, skill_packages = declare_sources(node)
    essential_devices                        = node.declare_parameter('essential_devices', sorted(DEFAULT_ESSENTIAL_DEVICES)).value

    return Registry.from_directories(directories), load_places(places_file), set(essential_devices), SkillLibrary(skill_packages)


class Tracer:
    """Every planner and executor step, as JSON on /orchestrator/trace and as a line for the Foxglove Log panel"""

    def __init__(self, node: Node, source: str):
        self.node   = node
        self.source = source
        self.fields = {}            # added to every event, e.g. the run_id of the tree that runs

        self._json_publisher = node.create_publisher(String, TRACE_TOPIC, TRACE_QOS)
        self._log_publisher  = node.create_publisher(Log, LOG_TOPIC, TRACE_QOS)

    def emit(self, kind: str, text: str, level: str = 'info', **data):
        event = {'time': time.time(), 'source': self.source, 'kind': kind, 'text': text, **self.fields, **data}

        self._json_publisher.publish(String(data=json.dumps(event, default=str)))

        log = Log()

        log.stamp = self.node.get_clock().now().to_msg()
        log.level = LEVELS[level]
        log.name  = f'{self.source}.{kind}'
        log.msg   = text

        self._log_publisher.publish(log)
