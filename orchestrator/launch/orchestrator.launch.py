from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution, PythonExpression
from launch_ros.substitutions import FindPackageShare
from launch_ros.actions import Node


def generate_launch_description():
    fake         = LaunchConfiguration('fake')
    vlm_base_url = LaunchConfiguration('vlm_base_url')
    params_file  = LaunchConfiguration('params_file')
    backend      = LaunchConfiguration('planner_backend')
    model        = LaunchConfiguration('planner_model')
    record       = LaunchConfiguration('record')
    mode         = LaunchConfiguration('mode')
    step_gate    = PythonExpression(["'", LaunchConfiguration('step_gate'), "' == 'true' or ('", LaunchConfiguration('step_gate'),
                                     "' == 'auto' and '", fake, "' != 'true')"])
    vlm_url      = PythonExpression(["'http://127.0.0.1:8090/v1' if '", fake, "' == 'true' else '", vlm_base_url, "'"])

    # Every node gets the same file and the same overrides, so one argument changes them all
    shared = [
        params_file,
        {
            'contracts_directory': LaunchConfiguration('contracts_directory'),
            'places_file':         LaunchConfiguration('places_file'),
            'vlm_base_url':        vlm_url,
        },
    ]

    return LaunchDescription([
        DeclareLaunchArgument('fake', default_value='false', description='start the fake robot and its VLM server'),
        DeclareLaunchArgument('vlm_base_url', default_value='http://vector-thor:8090/v1'),
        DeclareLaunchArgument(
            'params_file',
            default_value=PathJoinSubstitution([FindPackageShare('orchestrator'), 'config', 'orchestrator.yaml']),
            description='parameters for all nodes'
        ),
        DeclareLaunchArgument(
            'contracts_directory',
            default_value=PathJoinSubstitution([FindPackageShare('skill_registry'), 'contracts'])
        ),
        DeclareLaunchArgument(
            'places_file',
            default_value=PathJoinSubstitution([FindPackageShare('orchestrator'), 'config', 'places.yaml']),
            description='named map poses; for the elevator sim use its ros2/places.yaml'
        ),
        DeclareLaunchArgument('planner_backend', default_value='claude', description='claude | openai_compatible | scripted'),
        DeclareLaunchArgument('planner_model', default_value='claude-opus-5'),
        DeclareLaunchArgument('mode', default_value='task', description='task: a session does each task step by step (agent_cli); tree: planner_node writes a tree'),
        DeclareLaunchArgument('step_gate', default_value='auto',
                              description='task mode: a person approves each step; auto: on unless fake (true | false | auto)'),
        DeclareLaunchArgument('write_skill', default_value='false',
                              description='skill discovery: the agent saves contracts in contracts_directory; set that to its own folder, '
                                          'never the package contracts'),
        DeclareLaunchArgument('record', default_value='true', description='record every run (run_recorder); the executor refuses trees while it is not ready'),

        Node(package='skill_registry', executable='registry_node', output='screen', parameters=shared),
        Node(
            package='orchestrator',
            executable='executor_node',
            output='screen',
            parameters=[*shared, {'require_recorder': PythonExpression(["'", record, "' == 'true'"]), 'mode': mode,
                                  'step_gate': step_gate, 'write_skill': LaunchConfiguration('write_skill')}]
        ),
        Node(package='orchestrator', executable='run_recorder', output='screen', parameters=shared, condition=IfCondition(record)),
        Node(
            package='orchestrator',
            executable='planner_node',
            output='screen',
            parameters=[*shared, {'backend': backend, 'model': model}],
            condition=IfCondition(PythonExpression(["'", mode, "' == 'tree'"]))
        ),
        Node(package='orchestrator', executable='fake_robot', output='screen', condition=IfCondition(fake)),
    ])
