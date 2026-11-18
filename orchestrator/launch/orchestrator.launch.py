from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution, PythonExpression
from launch_ros.substitutions import FindPackageShare
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.actions import Node

from skill_registry import agent_dir


def generate_launch_description():
    fake         = LaunchConfiguration('fake')
    vlm_base_url = LaunchConfiguration('vlm_base_url')
    params_file  = LaunchConfiguration('params_file')
    record       = LaunchConfiguration('record')
    step_gate    = PythonExpression(["'", LaunchConfiguration('step_gate'), "' == 'true' or ('", LaunchConfiguration('step_gate'),
                                     "' == 'auto' and '", fake, "' != 'true')"])
    vlm_url      = PythonExpression(["'http://127.0.0.1:8090/v1' if '", fake, "' == 'true' else '", vlm_base_url, "'"])

    # Every node gets the same file and the same overrides, so one argument changes them all
    shared = [
        params_file,
        {
            'contracts_directory': LaunchConfiguration('contracts_directory'),
            'code_dir':            PathJoinSubstitution([LaunchConfiguration('agent_dir'), 'code']),
            'knowledge_dir':       PathJoinSubstitution([LaunchConfiguration('agent_dir'), 'knowledge']),
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
            default_value=str(agent_dir()),
            description="the agent's own folder in the workspace: contracts/ (write_skill), code/, knowledge/, runbook.md"
        ),
        DeclareLaunchArgument('task_agent', default_value='session',
                              description='session (a session outside uses agent_cli) or codex (the executor asks codex exec for each step)'),
        DeclareLaunchArgument('codex_model', default_value='gpt-6-astra', description='task_agent codex: the model, with the codex login'),
        DeclareLaunchArgument('write_skill', default_value='true',
                              description='the agent may save contracts with write_skill; false refuses that command '
                                          '(a file put in contracts_directory by hand still loads)'),
        DeclareLaunchArgument(
            'contracts_directory',
            default_value=PathJoinSubstitution([LaunchConfiguration('agent_dir'), 'contracts']),
            description='the skill contracts that load; the package has none, the agent writes or seeds them'
        ),
        DeclareLaunchArgument(
            'runbook',
            default_value=PathJoinSubstitution([LaunchConfiguration('agent_dir'), 'runbook.md']),
            description="what the robot is, in every task the agent opens. Default: the agent folder's runbook.md, "
                        "else the one the package installs (orchestrator/agent/runbook.md)"
        ),
        DeclareLaunchArgument(
            'places_file',
            default_value=PythonExpression(["'", PathJoinSubstitution([FindPackageShare('orchestrator'), 'config', 'places.yaml']),
                                            "' if '", fake, "' == 'true' else ''"]),
            description='named map poses; used when set (for the elevator sim its ros2/places.yaml). Default: none, '
                        'the places come from map_info; with fake:=true, this package\'s places.yaml'
        ),
        DeclareLaunchArgument('map_info', default_value='/map_info', description='the map_info node the places come from, live, when places_file is empty'),
        DeclareLaunchArgument('step_gate', default_value='auto',
                              description='a person approves each step; auto: on unless fake (true | false | auto)'),
        DeclareLaunchArgument('mission_control', default_value='false',
                              description='serve the Mission Control page: see the run and drive it from a browser'),
        DeclareLaunchArgument('mission_control_port', default_value='8080',
                              description='the page is on 127.0.0.1 only; reach it with ssh -L 8080:localhost:8080'),
        DeclareLaunchArgument('mission_control_token', default_value='',
                              description='a command needs this token; empty: the node makes one and logs the link, off: no token'),
        DeclareLaunchArgument('record', default_value='true', description='record every run (run_recorder); the executor refuses tasks while it is not ready'),

        Node(package='skill_registry', executable='registry_node', output='screen', parameters=shared),
        Node(
            package='orchestrator',
            executable='executor_node',
            output='screen',
            parameters=[*shared, {'require_recorder': PythonExpression(["'", record, "' == 'true'"]),
                                  'step_gate': step_gate, 'write_skill': LaunchConfiguration('write_skill'),
                                  'task_agent': LaunchConfiguration('task_agent'), 'codex_model': LaunchConfiguration('codex_model'),
                                  'runbook': LaunchConfiguration('runbook')}]
        ),
        Node(package='orchestrator', executable='run_recorder', output='screen', parameters=shared, condition=IfCondition(record)),
        Node(
            package='orchestrator',
            executable='mission_control',
            output='screen',
            parameters=[params_file, {'port': ParameterValue(LaunchConfiguration('mission_control_port'), value_type=int),
                                      'token': ParameterValue(LaunchConfiguration('mission_control_token'), value_type=str),
                                      'web_dir': PathJoinSubstitution([FindPackageShare('orchestrator'), 'web'])}],
            condition=IfCondition(LaunchConfiguration('mission_control'))
        ),
        Node(package='orchestrator', executable='fake_robot', output='screen', condition=IfCondition(fake)),
    ])
