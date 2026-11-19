import subprocess

from skill_registry.provenance import find_repos, git_state


def _git(where, *arguments):
    subprocess.run(['git', '-C', str(where), *arguments], check=True, capture_output=True)


def test_git_state_sees_local_edits(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()

    _git(repo, 'init', '-q')
    (repo / 'a.txt').write_text('a')
    _git(repo, 'add', 'a.txt')
    _git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'a')

    assert not git_state(repo)['dirty']

    (repo / 'b.txt').write_text('b')
    first = git_state(repo)

    (repo / 'b.txt').write_text('changed')

    assert first['dirty'] and git_state(repo)['edits'] != first['edits']
    assert find_repos([str(tmp_path)]) == [repo]


def test_git_state_reads_a_repo_that_holds_an_untracked_repo(tmp_path):
    """Git lists a repo inside the repo as one directory entry, because git does not look inside another repo.
    Git lists a plain untracked directory by its files, and that test passes without the fix. Make the inner
    folder with git init"""
    repo = tmp_path / 'repo'

    _git(tmp_path, 'init', '-q', str(repo))
    _git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'a')
    _git(repo, 'init', '-q', 'inner')                       # an untracked directory with a .git of its own

    state = git_state(repo)

    assert 'error' not in state and state['dirty'] and state['edits']
