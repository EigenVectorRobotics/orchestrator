"""What a skill was proved against: the repos it uses, and the version of each skill it calls.

A skill is only as good as the code under it. When the stack moves, a skill that worked can become wrong with
nothing to announce it: the team's hand-written recovery document stated the OLD navigation behaviour as a rule,
the follower changed, and the robot kept acting on it. So write_skill stamps a skill with the state of its repos
and the digest of every skill it runs, and the catalog says a stale skill is stale, with the reason.

The stamp is OUR record about the agent's file, not a part of the contract. A field on Contract would appear in the
contract grammar, which renders itself from the dataclass, and the agent would then be told to write its own stamp.
It is told nothing about maturity for the same reason, and a stamp never changes what maturity says.

Staleness cascades; the repos do not. A composition keeps the digest of each skill it calls, so a rewritten callee
makes its callers stale with no re-stamping, and the reason names the link that broke.

Only write_skill proves a skill. A step that succeeds adds its run to the stamp as evidence and clears nothing: in
task mode a step is one leaf, so the contract's check is a separate decision the agent may never take, and a
success can mean no more than "the message was published". Letting that erase the warning would reproduce the
incident, with the robot acting on stale code as the proof that the code is fine.
"""

import os
import re
import ast
import time
import threading

from pathlib import Path

import yaml

from skill_registry import Contract, Registry, agent_code_dir, code_file

from .recording import file_digest, repo_states


def called_skills(code: str) -> tuple[list[str], bool]:
    """The skills a code skill runs: the names in skills.run("<name>", ...). The flag says a call named its skill
    with something other than a literal, so what this skill depends on cannot be read from its text"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return [], True

    names, unknown = [], False

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'run'):
            continue

        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            names.append(node.args[0].value)
        elif node.args:
            unknown = True

    return names, unknown


def uses_of(contract: Contract, code: str = '') -> tuple[list[str], bool]:
    """Every skill that runs as part of this one: its conditions (pre, effects, check) and, for a code skill, the
    skills its Python calls. fallback is NOT one: it is a suggestion to the agent, not something this skill runs"""
    names, unknown = called_skills(code) if code else ([], False)

    return sorted({*contract.conditions, *names}), unknown


class Stamps:
    """The stamps of the agent's skills, in one file beside its library"""

    def __init__(self, contracts_dir: Path | str, repo_roots: list[str], code_dir: Path | str | None = None,
                 sweep_s: float = 5.0):
        self.contracts  = Path(contracts_dir).expanduser()
        self.code_dir   = Path(code_dir).expanduser() if code_dir else agent_code_dir()
        self.path       = self.contracts.parent / 'stamps.yaml'
        self.repo_roots = list(repo_roots)
        self.sweep_s    = sweep_s           # how long one git sweep serves; the registry reloads on the same period
        self._lock      = threading.Lock()  # the writer stamps on a save, the desk on a step: two threads, one file
        self._world     = None              # (clock, stamps, repo states): what reasons() reads

    # ========== Writing ==========

    def write(self, contract: Contract) -> dict:
        """Stamp one skill against the code that is here now; write_skill calls it once the files are on disk. It
        replaces the stamp, so the runs kept by ran() go: they were evidence about code that is no longer here"""
        uses, unknown = uses_of(contract, self.code_of(contract))
        stamp         = {
            'self':    self.digest(contract.name),   # its own file: an edit by hand loads too, and is not this stamp
            'repos':   self.repos_of(contract),
            'uses':    {name: self.digest(name) for name in uses},
            'unknown': unknown,            # a call that does not name its skill: stale() never clears this skill
            'at':      round(time.time(), 3),
        }

        with self._lock:
            self._save({**self._read(), contract.name: stamp})

        return stamp

    def ran(self, name: str, run_id: str) -> None:
        """A step with this skill succeeded. It is evidence, kept with the stamp, and it proves nothing on its own
        (see the module docstring), so repos and uses are untouched and a stale skill stays stale"""
        with self._lock:
            stamps = self._read()
            stamp  = stamps.get(name)

            if isinstance(stamp, dict):
                stamp['runs'] = [*(stamp.get('runs') or []), run_id][-MAX_RUNS:]
                self._save(stamps)

    def repos_of(self, contract: Contract) -> dict[str, str]:
        """The repos this skill names in source, as they are now. A skill that names none depends on the whole
        stack: coarse on purpose, because a wrong 'nothing changed' is worse than a loud one.

        source is free text, so a name is matched whole: 'vector' must not match a repo called vector_nav"""
        states = repo_states(self.repo_roots)
        words  = set(re.split(r'[^0-9A-Za-z_]+', contract.source))
        named  = {path: state for path, state in states.items() if Path(path).name in words}

        return named or states

    def code_of(self, contract: Contract) -> str:
        """The Python of a code skill, where the runner and write_skill agree it is; '' for another transport"""
        path = code_file(contract.name, self.code_dir)

        return path.read_text() if contract.interface.transport == 'code' and path.is_file() else ''

    def _save(self, stamps: dict) -> None:
        """Called under the lock. A temp file per writer, then one rename, so a reader never sees half a file"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
        temporary.write_text(yaml.safe_dump(stamps, sort_keys=True))
        temporary.replace(self.path)
        self._world = None                  # the next reasons() reads the file again

    # ========== Reading ==========

    def all(self) -> dict:
        with self._lock:
            return self._read()

    def _read(self) -> dict:
        """Called under the lock. A file we cannot parse reads as no stamps, so nothing is falsely proved"""
        try:
            return yaml.safe_load(self.path.read_text()) or {}
        except (FileNotFoundError, yaml.YAMLError):
            return {}

    def digest(self, name: str) -> str | None:
        return file_digest(self.contracts / f'{name}.yaml')

    def reasons(self, registry: Registry | None = None) -> dict[str, str]:
        """Every stale skill and why, in one pass: the stamp file and the repo states are read once, not once per
        skill, and a sweep serves sweep_s. The catalog renders for every decision and a step asks again, and one
        sweep is 3 git calls per repo over every repo under the roots"""
        stamps, now = self.world()
        found       = {}

        for name in stamps:
            reason = self._stale(name, stamps, now, set(), registry)

            if reason:
                found[name] = reason

        return found

    def world(self) -> tuple[dict, dict[str, str]]:
        """The stamps and the repo states, swept at most every sweep_s"""
        with self._lock:
            if self._world is None or time.monotonic() - self._world[0] > self.sweep_s:
                self._world = (time.monotonic(), self._read(), repo_states(self.repo_roots))

            return self._world[1], self._world[2]

    def _stale(self, name: str, stamps: dict, now: dict[str, str], seen: set[str], registry) -> str:
        """Why this skill is no longer proved, or '' when it is. A skill with no stamp is unknown, not stale"""
        if name in seen:                   # a cycle in the uses; the runner refuses one at run time
            return ''

        seen.add(name)
        stamp = stamps.get(name)

        if not isinstance(stamp, dict):
            return ''

        if stamp.get('unknown'):
            return 'it calls a skill it does not name, so what it depends on cannot be known'

        if stamp.get('self') != self.digest(name):
            return 'its own contract was edited after it was stamped'

        repos = stamp.get('repos') or {}

        if not repos:                      # no repo was found when it was stamped: nothing proves it
            return 'no repo was found when it was stamped, so nothing proves it'

        # a repo that the sweep no longer finds is NOT proof of nothing changed: wrong roots must be loud
        moved = [Path(path).name for path, state in repos.items() if now.get(path) != state]

        if moved:
            return f'the code moved under it, or is no longer where it was: {", ".join(sorted(moved))}'

        for used, digest in (stamp.get('uses') or {}).items():
            if registry is not None and registry.get(used) is None:
                return f'a skill it uses is gone: {used}'

            if digest is not None and self.digest(used) != digest:
                return f'a skill it uses changed: {used}'

            if reason := self._stale(used, stamps, now, seen, registry):
                return f'{used} is stale: {reason}'

        return ''


MAX_RUNS = 10          # the last runs that used a skill, as evidence; the bundles hold the whole history
