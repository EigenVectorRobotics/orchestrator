# FVD sim on one computer: sim_orin (the Orin, the lidar and the arm in MuJoCo) plus the NUC stack of
# eigen_vector bringup nuc.launch.py (lio branch) without the real lidar driver and arm skill.
#
#   ros2 launch fvd_sim.launch.py                                                  # mapping
#   ros2 launch fvd_sim.launch.py map:=<map.pcd> localization:=true                # localization
#   ros2 launch fvd_sim.launch.py map:=<map.pcd> localization:=true executor:=true # the FVD task
#   ... robot_start:='[2.6, -3.8, -1.0]'   # put the robot down away from the annotated start (particle filter)
#
# The video comes later from the bag: tool/hand_chaining/render_run.py.
#
# Source config/vector_pc.env first: it sets a separate ROS domain on localhost only.

import os
import sys

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, GroupAction, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

# The venv Python (MuJoCo, small_gicp); `ros2 launch` itself runs on the system Python
PYTHON = os.path.join(os.environ['VIRTUAL_ENV'], 'bin', 'python') if 'VIRTUAL_ENV' in os.environ else sys.executable
CONFIG = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(os.path.dirname(CONFIG), 'src')


def python_node(script, *arguments, condition=None):
    """A node from jean_dev/src, run with this Python (not an installed package)"""
    return ExecuteProcess(
        cmd=[PYTHON, os.path.join(SOURCE, script), '--ros-args', *arguments],
        output='screen',
        condition=condition
    )


def static_transform(
    name: str,
    parent: str,
    child: str,
    *pose: str,
) -> Node:
    """A tf2_ros static transform; pose like '--y', '0.015', '--roll', '0.1'"""
    return Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name=name,
        arguments=[*pose, '--frame-id', parent, '--child-frame-id', child],
    )


def generate_launch_description():
    localized = IfCondition(LaunchConfiguration('localization'))

    return LaunchDescription([
        DeclareLaunchArgument('map', default_value='', description='Prior map (.pcd). Empty means mapping'),
        DeclareLaunchArgument('localization', default_value='false'),
        DeclareLaunchArgument('executor', default_value='false', description='Start the task executor'),
        DeclareLaunchArgument('motion', default_value='unitree_rl', description='kinematic | unitree_rl'),
        DeclareLaunchArgument('robot_start', default_value='[0.0]', description='[x, y, yaw] where the robot is put down'),
        DeclareLaunchArgument('door_hold_s', default_value='15.0', description='Open car doors close after this; -1 never'),
        DeclareLaunchArgument('places', default_value=os.path.join(CONFIG, 'fvd_sim_places.yaml')),
        DeclareLaunchArgument('start_floor', default_value='1', description='Floor label where the robot starts'),
        DeclareLaunchArgument('button_source', default_value='track_3d',
                              description='Button pose for a press: track_3d (on Thor, over ROS_STATIC_PEERS) | sim'),
        DeclareLaunchArgument('locomotion', default_value='nav',
                              description="nav: every move on the navigation stack | own: the executor's own moves"),
        DeclareLaunchArgument('nav_params', default_value=os.path.join(CONFIG, 'nav_params_g1_press.yaml'),
                              description='Navigation stack parameters the executor sets at each command; empty: none'),
        DeclareLaunchArgument(
            'lio_config',
            default_value=os.path.join(CONFIG, 'elevator_lio', 'root_unitree_g1.yaml'),
            description='Elevator-LIO with elevator mode on (mapping). The task uses the deployed plain-LIO config'
        ),

        python_node(
            'hand_chaining/sim/sim_orin.py',
            '-p', ['motion:=', LaunchConfiguration('motion')],
            '-p', ['robot_start:=', LaunchConfiguration('robot_start')],
            '-p', ['door_hold_s:=', LaunchConfiguration('door_hold_s')],
            '-p', ['button_source:=', LaunchConfiguration('button_source')]
        ),
        # From bringup nuc.launch.py: lidar 0.35 m above base_link
        static_transform('sensor_to_base_link', 'sensor', 'base_link', '--z', '-0.35'),
        # The head camera frames, as on the robot. manipulation_stack g1_29dof_with_hand_rev_1_0_ros.urdf: mid360_joint
        # (sensor -> torso_link) and d435_joint (torso_link -> camera_link); realsense2_camera (_d435.urdf.xacro): the
        # colour frame 15 mm left of camera_link, and the optical frame (z forward, x right, y down) of the images.
        # Fixed joints only: robot_state_publisher with this URDF would also give base_link a second parent (pelvis)
        static_transform('sensor_to_torso', 'sensor', 'torso_link', '--x', '-0.011312', '--y', '0.005899', '--z', '-0.407630',
                         '--roll', '0.031021', '--pitch', '0.029913', '--yaw', '0.035418'),
        static_transform('torso_to_camera', 'torso_link', 'camera_link', '--x', '0.081099', '--y', '0.023467', '--z', '0.414921',
                         '--roll', '-0.005696', '--pitch', '0.320641', '--yaw', '-0.027792'),
        static_transform('camera_to_color', 'camera_link', 'camera_color_frame', '--y', '0.015'),
        static_transform('color_to_optical', 'camera_color_frame', 'camera_color_optical_frame',
                         '--roll', '-1.5707963', '--yaw', '-1.5707963'),
        # Scoped: navigation.launch.py sets map:='' for its route planner, which would leak to the nodes below
        GroupAction(scoped=True, actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    PathJoinSubstitution([FindPackageShare('navigation_bringup'), 'launch', 'navigation.launch.py'])
                ),
                launch_arguments={
                    'map':          LaunchConfiguration('map'),
                    'localization': LaunchConfiguration('localization'),
                    'lio_config':   LaunchConfiguration('lio_config'),
                }.items()
            ),
        ]),
        # Lidar door state: one rectangle in the car (seen from the landing), one on the landing (seen from the car)
        python_node(
            'door_state/door_state.py',
            '-r', '__node:=door_state_landing',
            '-r', '/door_state:=/door_state/landing',
            '-p', 'config:=' + os.path.join(CONFIG, 'fvd_sim_door_landing.yaml'),
            condition=localized
        ),
        python_node(
            'door_state/door_state.py',
            '-r', '__node:=door_state_car',
            '-r', '/door_state:=/door_state/car',
            '-p', 'config:=' + os.path.join(CONFIG, 'fvd_sim_door_car.yaml'),
            condition=localized
        ),
        python_node(
            'hand_chaining/pf_relocalize.py',
            '-p', ['map:=', LaunchConfiguration('map')],
            '-p', ['places:=', LaunchConfiguration('places')],
            condition=localized
        ),
        # Elevator floor from the Livox IMU (upside down on the G1: its z axis points down)
        Node(
            package='elevator_states',
            executable='imu_floor_detect_node',
            output='screen',
            # The sim car: 3.8 m at 0.65 m/s and 0.6 m/s^2, so one floor is ~5.8 s from accel to decel (RIC: < 4.2 s).
            # The Livox IMU reports g, so the threshold is in g
            parameters=[{'current_floor': LaunchConfiguration('start_floor'), 'invert_z': True, 'accel_threshold': 0.03,
                         'duration_cutoffs': [0.0, 8.5, 14.5, 20.5, 26.5, 32.5]}],
            condition=localized
        ),
        python_node(
            'hand_chaining/task_executor.py',
            '-p', ['places:=', LaunchConfiguration('places')],
            '-p', ['locomotion:=', LaunchConfiguration('locomotion')],
            '-p', ['nav_params:=', LaunchConfiguration('nav_params')],
            condition=IfCondition(LaunchConfiguration('executor'))
        ),
    ])
