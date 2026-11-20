"""The executor and Mission Control change one stamps file from two processes: each change waits for the other"""

import sys
import subprocess
import threading

from pathlib import Path

import pytest
import yaml

from skill_registry import parse_contract, skill_stamps
from skill_registry.provenance import file_digest
from skill_registry.skill_review import _runs, _stamps
from skill_registry.skill_stamps import Stamps, StampsError, read_stamps, write_atomic


SAY = {'name': 'say', 'kind': 'action', 'device': 'nuc', 'timeout_s': 5.0,
       'interface': {'transport': 'topic', 'name': '/speak', 'ros_type': 'std_msgs/msg/String'},
       'inputs': [{'name': 'text', 'type': 'string', 'field': 'data'}]}

HOLD   = 'import fcntl, sys\nlock = open(sys.argv[1], "w")\nfcntl.flock(lock, fcntl.LOCK_EX)\nprint("held", flush=True)\nsys.stdin.readline()\n'
BEFORE = {'walk': {'runs': ['r1']}, 'gone': {}}


def _changes(tmp_path):
    stamps = Stamps(tmp_path / 'skills', [], tmp_path / 'stamps.yaml')
    return {'ran':    lambda: stamps.ran('walk', 'r2'),
            'write':  lambda: stamps.write(parse_contract(SAY)),
            'review': lambda: _stamps(tmp_path / 'stamps.yaml', lambda found: found.pop('gone'))}


def _held(tmp_path, monkeypatch, change: str, while_held=lambda: None):
    """Run change while another process holds the lock; it must wait in flock, and finish when the lock goes"""
    path, waiting, flock = tmp_path / 'stamps.yaml', threading.Event(), skill_stamps.fcntl.flock
    monkeypatch.setattr(skill_stamps.fcntl, 'flock', lambda *arguments: (waiting.set(), flock(*arguments)))
    path.write_text(yaml.safe_dump(BEFORE))
    other = subprocess.Popen([sys.executable, '-c', HOLD, str(tmp_path / '.stamps.yaml.lock')],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    try:
        assert other.stdout.readline() == 'held\n'
        thread = threading.Thread(target=_changes(tmp_path)[change])
        thread.start()
        assert waiting.wait(5.0)
        thread.join(0.2)
        assert thread.is_alive() and yaml.safe_load(path.read_text()) == BEFORE
        while_held()
    finally:
        other.stdin.close()
        other.wait()

    thread.join(5.0)
    assert not thread.is_alive()
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize('change', ['ran', 'write', 'review'])
def test_a_change_waits_while_another_process_holds_the_lock(tmp_path, monkeypatch, change):
    assert _held(tmp_path, monkeypatch, change) != BEFORE


def test_write_takes_the_digest_under_the_lock_so_a_keep_meanwhile_is_not_undone(tmp_path, monkeypatch):
    contract = tmp_path / 'skills' / 'say' / 'contract.yaml'
    contract.parent.mkdir(parents=True)
    contract.write_text('# marked\n')

    stamps = _held(tmp_path, monkeypatch, 'write', while_held=lambda: contract.write_text('kept\n'))

    assert stamps['say']['self'] == file_digest(contract)


def test_a_change_that_changes_nothing_does_not_write_the_file(tmp_path):
    path = tmp_path / 'stamps.yaml'
    path.write_text('walk: {runs: [r1]}   # as a person wrote it\n')

    Stamps(tmp_path / 'skills', [], path).ran('not_stamped', 'r2')

    assert path.read_text() == 'walk: {runs: [r1]}   # as a person wrote it\n'


@pytest.mark.parametrize('text', ['walk: [r1\n', '- walk\n'])
def test_a_file_that_cannot_be_read_reads_as_none_and_no_change_writes_over_it(tmp_path, text):
    path   = tmp_path / 'stamps.yaml'
    path.write_text(text)
    stamps = Stamps(tmp_path / 'skills', [], path)

    assert read_stamps(path) == {} and _runs(path) == {}

    for change in (lambda: stamps.ran('walk', 'r2'), lambda: stamps.write(parse_contract(SAY))):
        with pytest.raises(StampsError, match='correct it or move it away'):
            change()

    _stamps(path, lambda found: found.clear())
    assert path.read_text() == text


def test_review_reads_a_stamps_path_with_a_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'stamps.yaml').write_text(yaml.safe_dump(BEFORE))

    _stamps('~/stamps.yaml', lambda found: found.pop('gone'))

    assert _runs('~/stamps.yaml') == {'walk': 1}


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    def fail(self, target):
        raise OSError('disk full')

    monkeypatch.setattr(Path, 'replace', fail)

    with pytest.raises(OSError, match='disk full'):
        write_atomic(tmp_path / 'contract.yaml', 'text')

    assert list(tmp_path.iterdir()) == []
