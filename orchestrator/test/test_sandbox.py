from orchestrator.sandbox import SandboxConfig, run_python


def test_run_python_runs_code_and_blocks_ros_writes_processes_and_hidden_folders(tmp_path):
    config = SandboxConfig(scratch_dir=str(tmp_path / 'scratch'), timeout_s=20)
    hidden = tmp_path / 'orchestrator_ws'                                          # another workspace's contracts
    hidden.mkdir()
    (hidden / 'say.yaml').write_text('name: say\n')

    assert run_python('import numpy; print(numpy.ones(2).sum())', config)['stdout'].strip() == '2.0'

    for code, error in [
        ('import rclpy', 'blocked in the agent sandbox'),
        ('import unitree_sdk2py', 'blocked in the agent sandbox'),
        (f'open({str(tmp_path / "outside.txt")!r}, "w")', 'writes only under'),
        ('import subprocess; subprocess.run(["true"])', 'subprocess.Popen is blocked'),
        ('import socket; socket.create_connection(("127.0.0.1", 9))', 'no network'),
        (f'open({str(hidden / "say.yaml")!r}).read()', 'hidden from the agent sandbox'),
        (f'import os; os.listdir({str(hidden)!r})', 'hidden from the agent sandbox'),
    ]:
        result = run_python(code, config)
        assert result['exit_code'] != 0 and error in result['stderr'], code

    assert run_python('open("helper.py", "w").write("X = 3")', config)['exit_code'] == 0
    assert run_python('import helper; print(helper.X)', config)['stdout'].strip() == '3'
    assert run_python('while True: pass', SandboxConfig(scratch_dir=config.scratch_dir, timeout_s=1))['timed_out']
