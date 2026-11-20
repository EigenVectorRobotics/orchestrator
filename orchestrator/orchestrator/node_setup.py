"""Parameters and loading shared by the executor, recorder and mission control nodes"""

import yaml

from pathlib import Path
from typing import Callable

from rclpy.node import Node

from skill_registry import DEFAULT_ESSENTIAL_DEVICES, Registry, agent_dir, missing_folder, skills_dir, workspace

from .places import parse_places
from .map_places import MapInfoPlaces
from .session_log import CLAUDE_LOGS, CODEX_SESSIONS, SessionLog
from .agent_session import AgentSessions


def load_places(path: str) -> dict:
    """Named map-frame poses, put on the blackboard by name; either places format (see places.py)"""
    if not path or not Path(path).exists():
        return {}

    with open(path) as file:
        return parse_places(yaml.safe_load(file))


def declare_sources(node: Node) -> tuple[str, str, str]:
    """Declare skills_dir, places_file and map_info: where every node loads its skills and places from.
    A places_file is used when it is set; else the places come from the map_info node (e.g. /map_info), live"""
    library     = node.declare_parameter('skills_dir', str(skills_dir())).value
    places_file = node.declare_parameter('places_file', '').value
    map_info    = node.declare_parameter('map_info', '/map_info').value

    return library, places_file, map_info


def agent_folders(node: Node) -> dict[str, str]:
    """Declare agent_folders: where the task agent's session starts, then the discovery agent's"""
    folders = node.declare_parameter('agent_folders', [str(agent_dir()), str(workspace() / 'agent_discovery')]).value

    if len(folders) != 2:
        raise ValueError(f'agent_folders holds two folders, the task agent\'s and the discovery agent\'s; not {folders}')

    return dict(zip(('task', 'discovery'), folders))


def session_log(node: Node, folders: dict[str, str]) -> SessionLog:
    """Declare session_logs and claude_logs: the agent's sessions are the Codex and Claude Code logs of a session in
    its folders"""
    return SessionLog(folders.values(), node.declare_parameter('session_logs', str(CODEX_SESSIONS)).value,
                      node.declare_parameter('claude_logs', str(CLAUDE_LOGS)).value)


def agent_sessions(node: Node, folders: dict[str, str]) -> AgentSessions:
    """Declare agent and agent_program: the tool that Mission Control's Start agent button starts in its folders"""
    return AgentSessions(node.declare_parameter('agent', '').value, node.declare_parameter('agent_program', '').value,
                         folders, env_file=workspace() / 'env.sh')


def load_skills(node: Node) -> tuple[Registry, Callable[[], dict], set[str]]:
    """Declare the sources and essential_devices; return the registry, the places now (a call) and the devices"""
    library, places_file, map_info             = declare_sources(node)
    essential_devices                          = node.declare_parameter('essential_devices', sorted(DEFAULT_ESSENTIAL_DEVICES)).value
    file_places                                = load_places(places_file)
    places                                     = (lambda: file_places) if places_file or not map_info else MapInfoPlaces(node, map_info)

    if warning := missing_folder(library):
        node.get_logger().warning(warning)

    return Registry.from_directory(Path(library)), places, set(essential_devices)
