from orchestrator.stale_nodes import judge, launch_files, parse_ps, ros_node


WS     = '/home/vector/eigen_vector_ws/install'
LAUNCH = f"""
      1       0  9000 /sbin/init splash
    900       1  8000 /usr/lib/systemd/systemd --user
   1000       1  5000 tmux -L eigen_vector new-session -d -s eigen_vector bash -lc 'ros2 launch bringup nuc.launch.py'
   1001    1000  5000 bash -lc source ~/eigen_vector_ws/env.sh && ros2 launch bringup nuc.launch.py map:=/m.pcd
   1002    1001  5000 /usr/bin/python3 /opt/ros/jazzy/bin/ros2 launch bringup nuc.launch.py map:=/m.pcd localization:=true
   1010    1002  5000 {WS}/unitree_interface/lib/unitree_interface/unitree_interface_node --ros-args -r __node:=unitree_interface
   1011    1002  5000 {WS}/local_planner/lib/local_planner/local_planner_node --ros-args -r __node:=local_planner
   1012    1002  5000 {WS}/local_planner/lib/local_planner/path_follower_node --ros-args -r __node:=path_follower
   1013    1002  5000 {WS}/route_planner/lib/route_planner/route_planner_node --ros-args -r __node:=route_planner
   1014    1002  5000 /usr/bin/python3 {WS}/elevator_states/lib/elevator_states/door_state_node.py --ros-args -r __node:=door_state_node
   1015    1002  5000 /opt/ros/jazzy/lib/foxglove_bridge/foxglove_bridge --ros-args -r __node:=foxglove_bridge
   1100     900  4000 /usr/bin/python3 /home/vector/orchestrator_ws/install/orchestrator/lib/orchestrator/executor_node
"""
GRAPH  = ['/unitree_interface', '/local_planner', '/path_follower', '/route_planner', '/door_state_node', '/foxglove_bridge']


def _judge(*extra_lines, graph=GRAPH, orin=''):
    return judge(graph, {'nuc': parse_ps(LAUNCH + '\n'.join(extra_lines)), 'orin': parse_ps(orin)})


def test_a_stack_run_by_its_launch_is_clean():
    assert _judge() == []


def test_node_names_and_launches_are_read_from_the_command_line():
    assert ros_node(f'{WS}/lio/lib/lio/lio --ros-args -r __node:=lio_node') == ('lio_node', f'{WS}/lio/lib/lio/lio')
    assert ros_node(f'{WS}/local_planner/lib/local_planner/path_follower_node')[0] == 'path_follower'
    assert ros_node(f'/usr/bin/python3 {WS}/elevator_states/lib/elevator_states/door_state_node.py -r __node:=door_state_node')[0] == 'door_state_node'
    assert ros_node('/usr/bin/python3 /opt/ros/jazzy/bin/ros2 run local_planner path_follower_node') is None
    assert launch_files('/usr/bin/python3 /opt/ros/jazzy/bin/ros2 launch bringup nuc.launch.py map:=x') == {'bringup', 'nuc.launch.py'}
    assert launch_files("bash -lc 'ros2 launch bringup nuc.launch.py'") == set()


def test_a_second_unitree_interface_from_another_workspace_fails():
    findings = _judge('   2000       1  3000 /home/vector/play_wav_ws/install/unitree_interface/lib/unitree_interface/'
                      'unitree_interface_node', graph=GRAPH + ['/unitree_interface'])

    assert findings[-1][0] == 'fail'
    assert 'unitree_interface runs 2 times' in findings[-1][1] and '~/play_wav_ws/install' in findings[-1][1]
    assert [level for level, _ in findings] == ['warn', 'fail']
    assert 'orphaned' in findings[0][1] and 'comes from ~/play_wav_ws/install' in findings[0][1]


def test_a_control_node_twice_in_the_graph_fails_and_another_node_twice_warns():
    findings = _judge(graph=GRAPH + ['/path_follower', '/foxglove_bridge'])

    assert findings == [('warn', '/foxglove_bridge is 2 times in the ROS graph'),
                        ('fail', '/path_follower is 2 times in the ROS graph')]


def test_nodes_restarted_by_hand_in_tmux_warn_that_a_restart_would_duplicate_them():
    lines    = [line for line in LAUNCH.splitlines() if ' 1012 ' not in line and ' 1013 ' not in line]
    findings = judge(GRAPH, {'nuc': parse_ps('\n'.join(lines + [
        '   3000       1  2000 tmux new-session -d -s pose_pid_follower',
        '   3001    3000  2000 bash',
        '   3002    3001  2000 /usr/bin/python3 /opt/ros/jazzy/bin/ros2 run local_planner path_follower_node',
        f'   3003    3002  2000 {WS}/local_planner/lib/local_planner/path_follower_node --ros-args --params-file p.yaml',
        '   3100       1  1900 tmux new-session -d -s route_planner_restart',
        f'   3101    3100  1900 {WS}/route_planner/lib/route_planner/route_planner_node',
    ])), 'orin': {}})

    assert [level for level, _ in findings] == ['warn', 'warn']
    assert all('outside nuc.launch.py, from tmux; a stack restart would start a second copy' in text for _, text in findings)
    assert {text.split()[0] for _, text in findings} == {'path_follower', 'route_planner'}


def test_old_orphaned_helpers_warn_and_young_or_parented_ones_do_not():
    findings = _judge(
        '   4000       1  7200 /usr/bin/python3 /opt/ros/jazzy/bin/ros2 topic echo /orchestrator/trace --field data',
        '   4001       1    60 /usr/bin/python3 /opt/ros/jazzy/bin/ros2 topic echo /feedback',
        '   4004    1001  7200 /usr/bin/python3 /opt/ros/jazzy/bin/ros2 bag record -a',
        "   4005       1  7200 bash -c 'nohup ros2 topic echo /x'",
    )

    assert findings == [('warn', 'orphaned ros2 topic echo (nuc pid 4000, 120 min old)')]


def test_an_orin_node_outside_orin_launch_warns():
    orin     = ('   10       1  900 tmux new-session -d -s gripper_est\n'
                '   11      10  900 /home/unitree/eigen_vector_ws/install/humanoid_arm_skills/lib/humanoid_arm_skills/'
                'gripper_pose_estimator')
    findings = _judge(orin=orin)

    assert findings == [('warn', 'gripper_pose_estimator (orin pid 11) runs outside orin.launch.py, from tmux; '
                                 'a stack restart would start a second copy')]


def test_unreadable_sources_warn():
    findings = judge(None, {'nuc': parse_ps(LAUNCH)})

    assert findings == [('warn', 'no ROS graph: ros2 node list failed'), ('warn', 'no process list from orin')]
