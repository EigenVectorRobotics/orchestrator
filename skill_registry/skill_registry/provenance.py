"""The state of the code: file digests and git state. A skill stamp (skill_stamps.py) and a run bundle (the
recorder's provenance.json) both say which code they saw, with these"""

import hashlib
import subprocess

from pathlib import Path

from .registry import workspace


MAX_HASHED_BYTES = 5_000_000                   # an untracked file bigger than this is hashed by name and size


def file_digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else None


def directory_digests(directory: Path, pattern: str) -> dict:
    """Path inside directory -> hash of its bytes, e.g. each skill's contract and code: '<name>/contract.yaml'"""
    return {str(path.relative_to(directory)): file_digest(path) for path in sorted(Path(directory).glob(pattern))}


def git_state(repo: Path, exclude: tuple[str, ...] = ()) -> dict:
    """Revision, branch, and a hash of the local edits (tracked diff and untracked files), so a run with
    uncommitted changes is not mistaken for its commit. exclude: folders inside the repo that do not count, e.g. the
    skill library for a stamp; the revision is then the last commit that changed anything else"""
    def git(*arguments):
        result = subprocess.run(['git', '-C', str(repo), *arguments], capture_output=True, check=False)
        return result.stdout if result.returncode == 0 else None

    names = git('rev-parse', 'HEAD', '--abbrev-ref', 'HEAD')

    if names is None:
        return {'path': str(repo), 'error': 'not a git checkout'}

    revision, branch = names.decode().split()
    paths            = ['--', '.', *(f':(exclude){folder}' for folder in exclude)] if exclude else []
    edits            = git('diff', 'HEAD', *paths) or b''

    if exclude:
        revision = (git('log', '-1', '--format=%H', *paths) or b'').decode().strip() or revision

    for name in filter(None, (git('ls-files', '--others', '--exclude-standard', '-z', *paths) or b'').split(b'\0')):
        path   = repo / name.decode()
        if not path.is_file():                      # git lists an untracked DIRECTORY as one entry; it is not readable
            edits += name
            continue

        size   = path.stat().st_size
        edits += name + (path.read_bytes() if size <= MAX_HASHED_BYTES else str(size).encode())

    return {
        'path':     str(repo),
        'revision': revision,
        'branch':   branch,
        'dirty':    bool(edits),
        'edits':    hashlib.sha256(edits).hexdigest()[:16] if edits else None,
    }


def repo_states(roots: list[str], exclude: list[Path] = ()) -> dict[str, str]:
    """Each repo under roots as one short string: its revision, and its local edits when it has any (git_state).
    It answers one question for a skill: is the code still the one it was proved against? The run bundle keeps the
    full git_state per repo, so a stamp is the short form of what provenance.json already records. exclude: folders
    that do not count, wherever they are (a link is followed): the skill library is in a repo, and a new skill is not
    a change in the code under the others"""
    states  = {}
    folders = [Path(folder).resolve() for folder in exclude]

    for repo in find_repos(roots):
        inside = tuple(str(folder.relative_to(repo.resolve())) for folder in folders if folder.is_relative_to(repo.resolve()))
        state  = git_state(repo, inside)

        if state.get('revision'):
            states[str(repo)] = state['revision'][:12] + (f'+{state["edits"][:8]}' if state.get('edits') else '')

    return states


DEFAULT_REPO_ROOTS = [str(workspace() / 'src')]     # the recorder's and the stamps': the repos this workspace builds


def find_repos(roots: list[str]) -> list[Path]:
    """Each root that is a git checkout, and each checkout directly inside a root (a colcon src/)"""
    repos = []

    for root in roots:
        root = Path(root).expanduser()

        if (root / '.git').exists():
            repos.append(root)
        elif root.is_dir():
            repos += sorted(child for child in root.iterdir() if (child / '.git').exists())

    return repos
