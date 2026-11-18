from orchestrator.health.checks import judge_alive, run_command


SPEC = {'topics': ['/feedback', '/camera'], 'services': ['/ready'], 'http': ['http://thor:8090/v1/models']}


def test_alive_when_everything_is_up():
    success, message, outputs = judge_alive(SPEC, {'/feedback', '/camera', '/other'}, {'/ready'}, lambda url: True)

    assert success
    assert message == 'all 4 up'
    assert outputs == {}


def test_alive_names_what_is_missing():
    success, message, outputs = judge_alive(SPEC, {'/feedback'}, set(), lambda url: False)

    assert not success
    assert outputs['missing'] == {'topics': ['/camera'], 'services': ['/ready'], 'http': ['http://thor:8090/v1/models']}
    assert 'topics: /camera' in message


def test_command_exit_code_decides():
    assert run_command('echo started', 5.0)[:2] == (True, 'exit 0: started')

    success, message, outputs = run_command('echo no ssh >&2; exit 255', 5.0)

    assert not success
    assert outputs['exit_code'] == 255
    assert 'no ssh' in message


def test_command_timeout_fails():
    success, message, _ = run_command('sleep 2', 0.3)

    assert not success
    assert 'no exit within' in message
