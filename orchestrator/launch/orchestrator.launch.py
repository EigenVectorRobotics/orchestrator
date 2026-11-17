"""The orchestrator: registry, executor, recorder and planner, and with fake:=true the fake robot.

  ros2 launch orchestrator orchestrator.launch.py site:=stanley       on the robot (config/sites/stanley.yaml)
  ros2 launch orchestrator orchestrator.launch.py fake:=true          a laptop test with the fake robot

A site file holds what a robot run needs beyond the code defaults (orchestrator/site.py): the places file, the planner
backend, the agent bridge's address and the raw bag. A launch argument given on the command line wins over the site.
"""

import os
import time

from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, LogInfo, OpaqueFunction
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

from orchestrator.site import load_site


DEFAULTS = {
    'planner_backend': 'claude',
    'planner_model':   'claude-opus-5-5',
    'vlm_base_url':    'http://vector-thor:8090/v1',
}


def resolve(given: dict, site: dict, share: Path, registry_share: Path) -> dict:
    """Every value the nodes get: a launch argument given on the command line, else the site, else the default"""
    value  = lambda name: given.get(name) or site.get(name) or DEFAULTS[name]
    fake   = given.get('fake') == 'true'
    bridge = site.get('agent_bridge') or {}
    raw    = site.get('raw_bag') or {}
    found  = {
        'fake':            fake,
        'record':          given.get('record') != 'false',
        'params_file':     given.get('params_file') or str(share / 'config' / 'orchestrator.yaml'),
        'contracts':       given.get('contracts_directory') or str(registry_share / 'contracts'),
        'places_file':     given.get('places_file') or site.get('places_file') or str(share / 'config' / 'places.yaml'),
        'vlm_base_url':    'http://127.0.0.1:8090/v1' if fake else value('vlm_base_url'),
        'planner_backend': value('planner_backend'),
        'planner_model':   value('planner_model'),
        'agent_backend':   given.get('agent_backend') or 'push',
        'agent_push_host': str(bridge.get('host', '')),
        'agent_push_port': int(bridge.get('port', 8771)),
        'raw_bag':         None,
    }

    if raw.get('topics') and given.get('raw_bag') != 'false' and not fake:
        folder            = Path(os.path.expanduser(raw.get('dir', '~/bags')))
        found['raw_bag'] = {'output': str(folder / time.strftime('raw_%Y%m%d_%H%M')), 'topics': list(raw['topics'])}

    return found


def _actions(context):
    share  = Path(get_package_share_directory('orchestrator'))
    given  = dict(context.launch_configurations)
    found  = resolve(given, load_site(given.get('site', ''), share / 'config' / 'sites'), share,
                     Path(get_package_share_directory('skill_registry')))
    shared = [found['params_file'], {'contracts_directory': found['contracts'], 'places_file': found['places_file'],
                                     'vlm_base_url': found['vlm_base_url']}]
    executor = {'require_recorder': found['record'], 'agent_backend': found['agent_backend'],
                'agent_push_host': found['agent_push_host'], 'agent_push_port': found['agent_push_port']}
    actions  = [
        LogInfo(msg=f'orchestrator: site {given.get("site") or "none"}, places {found["places_file"]}, '
                    f'planner {found["planner_backend"]}, agent bridge {found["agent_push_host"] or "unset"}'),
        Node(package='skill_registry', executable='registry_node', output='screen', parameters=shared),
        Node(package='orchestrator', executable='executor_node', output='screen', parameters=[*shared, executor]),
        Node(package='orchestrator', executable='planner_node', output='screen',
             parameters=[*shared, {'backend': found['planner_backend'], 'model': found['planner_model']}]),
    ]

    if found['record']:
        actions.append(Node(package='orchestrator', executable='run_recorder', output='screen', parameters=shared))

    if found['fake']:
        actions.append(Node(package='orchestrator', executable='fake_robot', output='screen'))

    if found['raw_bag']:                    # stops with the launch: Ctrl-C closes the bag
        bag = found['raw_bag']
        Path(bag['output']).parent.mkdir(parents=True, exist_ok=True)
        preload = os.environ.get('EIGEN_VECTOR_PRELOAD', os.environ.get('LD_PRELOAD', ''))
        actions += [
            LogInfo(msg=f'raw bag: {bag["output"]} ({len(bag["topics"])} topics)'),
            ExecuteProcess(cmd=['ros2', 'bag', 'record', '-o', bag['output'], *bag['topics']], output='screen',
                           additional_env={'LD_PRELOAD': preload}, sigterm_timeout='10'),
        ]

    return actions


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('site', default_value='', description='a site in config/sites (e.g. stanley), or a path'),
        DeclareLaunchArgument('fake', default_value='false', description='start the fake robot and its VLM server'),
        DeclareLaunchArgument('places_file', default_value='', description='named map poses; default: the site, else config/places.yaml'),
        DeclareLaunchArgument('planner_backend', default_value='', description='claude | openai_compatible | scripted | external; default: the site, else claude'),
        DeclareLaunchArgument('planner_model', default_value='', description=f'default: the site, else {DEFAULTS["planner_model"]}'),
        DeclareLaunchArgument('vlm_base_url', default_value='', description=f'default: {DEFAULTS["vlm_base_url"]}'),
        DeclareLaunchArgument('agent_backend', default_value='push', description='push | none'),
        DeclareLaunchArgument('params_file', default_value='', description='parameters for all nodes; default: config/orchestrator.yaml'),
        DeclareLaunchArgument('contracts_directory', default_value=''),
        DeclareLaunchArgument('record', default_value='true', description='record every run (run_recorder); the executor refuses trees while it is not ready'),
        DeclareLaunchArgument('raw_bag', default_value='true', description='record the site raw_bag topics beside the orchestrator'),
        OpaqueFunction(function=_actions),
    ])
