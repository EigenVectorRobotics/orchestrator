from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution, PythonExpression
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
            'map_info':            LaunchConfiguration('map_info'),
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
            'agent_dir',
            default_value=PathJoinSubstitution([EnvironmentVariable('EIGEN_VECTOR_WS', default_value=EnvironmentVariable('HOME')), 'agent']),
            description="the agent's own folder in the workspace: contracts/ (write_skill) and docs/ (what it writes about the stack)"
        ),
        DeclareLaunchArgument('task_agent', default_value='session',
                              description='task mode: session (a session outside uses agent_cli) or codex (the executor asks codex exec for each step)'),
        DeclareLaunchArgument('codex_model', default_value='gpt-6-astra', description='task_agent codex: the model, with the codex login'),
        DeclareLaunchArgument('write_skill', default_value='false',
                              description='skill discovery: the agent saves contracts in <agent_dir>/contracts, and only those load'),
        DeclareLaunchArgument(
            'contracts_directory',
            default_value=PythonExpression(["'", LaunchConfiguration('agent_dir'), "/contracts' if '", LaunchConfiguration('write_skill'),
                                            "' == 'true' else '", PathJoinSubstitution([FindPackageShare('skill_registry'), 'contracts']), "'"]),
            description="the skill contracts that load: the package's, or with write_skill:=true the agent's"
        ),
        DeclareLaunchArgument(
            'places_file',
            default_value=PythonExpression(["'", PathJoinSubstitution([FindPackageShare('orchestrator'), 'config', 'places.yaml']),
                                            "' if '", fake, "' == 'true' else ''"]),
            description='named map poses; used when set (for the elevator sim its ros2/places.yaml). Default: none, '
                        'the places come from map_info; with fake:=true, this package\'s places.yaml'
        ),
        DeclareLaunchArgument('map_info', default_value='/map_info', description='the map_info node the places come from, live, when places_file is empty'),
        DeclareLaunchArgument('planner_backend', default_value='claude', description='claude | openai_compatible | scripted'),
        DeclareLaunchArgument('planner_model', default_value='claude-opus-5'),
        DeclareLaunchArgument('mode', default_value='task', description='task: a session does each task step by step (agent_cli); tree: planner_node writes a tree'),
        DeclareLaunchArgument('step_gate', default_value='auto',
                              description='task mode: a person approves each step; auto: on unless fake (true | false | auto)'),
        DeclareLaunchArgument('record', default_value='true', description='record every run (run_recorder); the executor refuses trees while it is not ready'),

        Node(package='skill_registry', executable='registry_node', output='screen', parameters=shared),
        Node(
            package='orchestrator',
            executable='executor_node',
            output='screen',
            parameters=[*shared, {'require_recorder': PythonExpression(["'", record, "' == 'true'"]), 'mode': mode,
                                  'step_gate': step_gate, 'write_skill': LaunchConfiguration('write_skill'),
                                  'task_agent': LaunchConfiguration('task_agent'), 'codex_model': LaunchConfiguration('codex_model')}]
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
