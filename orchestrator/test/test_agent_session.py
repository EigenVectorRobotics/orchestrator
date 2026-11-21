import subprocess

import pytest

from orchestrator.agent_session import AgentSessions
from orchestrator.mission_control import Commands, MissionApi, MissionState


class Host:
    """Records each command. A kind in running answers has-session; the login shell finds the programs in found"""

    def __init__(self, running=(), found=('claude',)):
        self.calls, self.running, self.found = [], set(running), found

    def __call__(self, words, **_):
        self.calls.append(words)

        if words[0] == 'bash':
            name = words[-1].split()[-1]
            return subprocess.CompletedProcess(words, 0 if name in self.found else 1, f'/bin/{name}\n' if name in self.found else '', '')

        asked = words[3] == 'has-session'
        return subprocess.CompletedProcess(words, 1 if asked and words[2] not in self.running else 0, '', '')


def test_a_start_runs_the_tool_found_by_a_login_shell_in_the_folder_of_its_kind(tmp_path):
    (tmp_path / 'agent').mkdir(); (tmp_path / 'env.sh').write_text('')
    host     = Host()
    sessions = AgentSessions('claude', '', {'task': tmp_path / 'agent', 'discovery': tmp_path / 'none'},
                             env_file=tmp_path / 'env.sh', run=host)

    assert sessions.start({'discovery': False})[0] == 200
    assert host.calls[0][-1] == f'. {tmp_path}/env.sh; command -v claude'
    new = host.calls[-1]
    assert new[:7] == ['tmux', '-L', 'task', 'new-session', '-d', '-s', 'task'] and new[-3:-1] == ['-c', str(tmp_path / 'agent')]
    assert f'. {tmp_path}/env.sh; /bin/claude --dangerously-skip-permissions' in new[-1] and 'agent_cli task wait' in new[-1]

    host.calls.clear()
    assert sessions.start({'discovery': True})[0] == 409 and host.calls == []          # no folder: nothing runs


def test_a_running_agent_gets_the_new_program_in_its_pane_and_a_missing_tool_starts_nothing(tmp_path):
    host     = Host(running={'task'})
    sessions = AgentSessions('claude', '', {'task': tmp_path}, run=host)

    assert sessions.start({})[0] == 200
    assert host.calls[-1][3:9] == ['respawn-pane', '-k', '-t', 'task', '-c', str(tmp_path)]

    host.calls.clear()
    assert AgentSessions('codex', '', {'task': tmp_path}, run=host).start({})[0] == 409   # no codex on the PATH
    assert all(words[0] == 'bash' for words in host.calls)


def test_no_tool_starts_nothing_and_a_wrong_tool_stops_the_node(tmp_path):
    none = AgentSessions('', '', {'task': tmp_path}, run=Host())

    assert none.state() is None and none.start({})[0] == 404

    with pytest.raises(ValueError):
        AgentSessions('gemini', '', {'task': tmp_path})


def test_starting_an_agent_needs_the_page_token(tmp_path):
    host = Host()
    api  = MissionApi(MissionState(), Commands(None, None, None, None, None), token_needed=False,
                      agents=AgentSessions('claude', '', {'task': tmp_path}, run=host))

    assert api.post('/api/agent', {})[0] == 403 and host.calls == []


def test_the_page_asks_tmux_at_most_every_two_seconds_and_again_after_a_start(tmp_path):
    now, host = [0.0], Host(running={'discovery'})
    sessions  = AgentSessions('claude', '', {'task': tmp_path, 'discovery': tmp_path}, run=host, clock=lambda: now[0])

    assert sessions.state() == {'tool': 'claude', 'running': {'task': False, 'discovery': True}}
    now[0] = 1.5; sessions.state()
    assert len(host.calls) == 2                                    # one has-session per kind, once

    sessions.start({}); host.calls.clear(); sessions.state()
    assert len(host.calls) == 2                                    # asked again after the start
