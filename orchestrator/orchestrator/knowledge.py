"""What the robot has learned, as claims that go up in tiers with evidence: observation -> belief -> fact.

A claim is one thing seen or measured, about open tags (a place by its map_info name, a skill, a device, ...) or
nothing. Every later sighting is a confirmation or a contradiction, with its evidence (a run, a record, who saw it).
The rules (docs/knowledge.md):

Grades are staged, then committed (stage, commit): the agent stages a grade after a step that followed a claim,
and the grades of a run go into the claims together after the run ends. Each grade keeps the code under the claim
at its step (under), so a grade of code that has changed since is kept but not counted.

- observation -> belief: confirmed in 2 runs or more, by 2 sources or more (a check, an agent skill, the operator).
  The agent's grade of a claim that a step followed (OUTCOME) is weak evidence: useful confirms and wrong
  contradicts; it counts in reliability, but it moves no tier, up or down. Misleading and not-applicable are kept
  apart (Claim.grades) and change no reliability: they say that the claim's text or tags are too wide
- belief -> fact: the Wilson lower bound of confirmations / (confirmations + contradictions) is at least
  FACT_RELIABILITY, and the operator approves it. Nobody else makes a fact.
- fact -> belief -> observation: one contradiction moves a claim down one tier
- any -> retired: valid_for_s has passed since the claim was last seen, or the operator retires it

A claim about a skill is true of one version of the code under it: the agent rewrites its skills, and the stack
under them changes. So with a code clock (skill_stamps.SkillClock), a claim with a skill tag keeps the code its
evidence was seen on (under). When that code moves, its sightings stop counting (moved says why), it goes back to
an observation, and it must earn its tier again on the new code. Claims about places, devices and objects have no
clock. A limit: a value measured through a skill but tagged only with a place (an arrival offset) depends on that
skill's code too, and is not reset; give it the skill tag as well.

claims.yaml holds the claims; log.jsonl holds every change and who made it, so the operator's approvals can later
teach the agent to approve. Both live in one folder on the NUC (no cloud). Writers take a file lock, so the
executor and the knowledge CLI can both write. The agent reaches the store only through its note tool: its
sandbox writes only in its scratch folder, so it cannot write claims.yaml or approve.

This module needs no ROS.
"""

import re
import json
import math
import time
import fcntl
import yaml

from typing import NamedTuple, Protocol
from collections import Counter
from pathlib import Path
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

from .recording import write_json


TIERS            = ('observation', 'belief', 'fact', 'retired')
LIVE             = TIERS[:3]        # in order: a contradiction moves a claim one step back in this list
FACT_RELIABILITY = 0.8
WILSON_Z         = 1.645            # one-sided 95 %: with no contradiction, 11 confirmations make a belief ready
MIN_RUNS         = 2
MIN_SOURCES      = 2
OPERATOR         = 'operator'
OUTCOME          = 'outcome'        # by 'outcome:<skill>': the grade of a step that followed a claim, as weak evidence
GRADES           = {'useful': True, 'wrong': False, 'misleading': None, 'not-applicable': None}   # True confirms, False contradicts
TAG_KIND         = re.compile(r'[a-z][a-z0-9_]*')
RUN_ID           = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]*')   # a staged file name: no path


class KnowledgeError(ValueError):
    pass


class CodeClock(Protocol):
    def snapshot(self, skill: str) -> dict: ...
    def since(self, skill: str, snapshot: dict) -> str: ...       # why the code moved since snapshot, or ''


def wilson_lower_bound(successes: int, total: int, z: float = WILSON_Z) -> float:
    if total == 0:
        return 0.0

    p      = successes / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))

    return (centre - spread) / (1 + z * z / total)


@dataclass
class Sighting:
    """One confirmation or contradiction: who, in which run, from what evidence, and the value seen"""
    by:       str                       # 'operator', 'agent:task', 'check:door_open', ...
    time:     float
    run:      str | None = None         # the run_id of the recorder's bundle
    evidence: list[str] = field(default_factory=list)    # e.g. 'record:7', 'frame:press', 'step:3'
    value:    object = None
    moved:    str | None = None         # the code under the claim moved after it was seen: it no longer counts
    grade:    str | None = None         # the agent's grade of a claim that a step followed (GRADES)
    reason:   str | None = None         # why, in the agent's words
    under:    dict | None = None        # a staged grade: the code under the claim at its step, checked at the commit


class Counted(NamedTuple):
    """The sightings seen on the code that is here now"""
    confirmations:  list[Sighting]
    contradictions: list[Sighting]


@dataclass
class Claim:
    id:             str
    text:           str
    tier:           str = 'observation'
    about:          dict = field(default_factory=dict)  # open tags, e.g. {place: name, skill: name}; {} is general
    key:            str | None = None                    # a value claim: compared by key
    valid_for_s:    float | None = None
    confirmations:  list[Sighting] = field(default_factory=list)
    contradictions: list[Sighting] = field(default_factory=list)
    approved_by:    str | None = None
    under:          dict | None = None                   # the code the counted sightings were seen on (a skill tag)
    grades:         list[Sighting] = field(default_factory=list)   # misleading or not-applicable: no evidence

    def __post_init__(self):
        self.about = _clean(self.about)              # a claim written by hand in claims.yaml too; a bad kind still loads

    @property
    def sightings(self) -> list[Sighting]:
        return [*self.confirmations, *self.contradictions]

    @property
    def last_seen(self) -> float:
        return max(s.time for s in self.sightings)

    @property
    def counted(self) -> Counted:
        return Counted([s for s in self.confirmations if not s.moved], [s for s in self.contradictions if not s.moved])

    @property
    def value(self):
        """The latest value confirmed"""
        values = [s.value for s in self.counted.confirmations if s.value is not None]
        return values[-1] if values else None

    def reliability(self) -> float:
        """Over independent sightings: one per run and source, so ten notes in one call count once"""
        good, bad = map(_independent, self.counted)
        return wilson_lower_bound(good, good + bad)

    def ready_for_fact(self) -> bool:
        return self.tier == 'belief' and self.reliability() >= FACT_RELIABILITY

    def check_ready(self):
        if not self.ready_for_fact():
            raise KnowledgeError(f'{self.id} is a {self.tier} with reliability {self.reliability():.2f}; '
                                 f'a fact needs a belief at {FACT_RELIABILITY}')

    def value_stats(self) -> dict | None:
        """Mean and spread of numeric values, per axis for lists (e.g. a press offset)"""
        values = [s.value for s in self.counted.confirmations if _numeric(s.value) or isinstance(s.value, list) and s.value and all(map(_numeric, s.value))]

        if not values:
            return None

        scalar = not isinstance(values[0], list)
        rows   = [[value] if scalar else value for value in values]

        if any(isinstance(value, list) == scalar for value in values) or len({len(row) for row in rows}) != 1:
            return None

        columns = list(zip(*rows))
        means   = [sum(column) / len(column) for column in columns]
        spreads = [math.sqrt(sum((x - mean) ** 2 for x in column) / len(column)) for column, mean in zip(columns, means)]
        unwrap  = (lambda axes: axes[0]) if scalar else (lambda axes: axes)

        return {'n': len(rows), 'mean': unwrap(means), 'std': unwrap(spreads)}

    def subject(self) -> str:
        return ', '.join(f'{kind} {name}' for kind, name in self.about.items()) or 'general'


def about_of(tags: dict | None = None) -> dict:
    """The subject of a claim: open tags as {kind: name}; empty tags are dropped, {} is general. The kinds are free
    (place, skill, device, object, ...); only place names come from map_info"""
    about = _clean(tags)

    for kind in about:
        if not TAG_KIND.fullmatch(kind):
            raise KnowledgeError(f'a tag kind is one snake_case word, not {kind!r}')

    return about


def _clean(tags: dict | None) -> dict:
    """Stripped and sorted, with empty and null names dropped"""
    about = {str(kind).strip(): str(name).strip() for kind, name in (tags or {}).items() if name is not None}
    return dict(sorted((kind, name) for kind, name in about.items() if name))


def _claim_from(raw: dict) -> Claim:
    raw = dict(raw)
    raw['confirmations']  = [Sighting(**s) for s in raw.get('confirmations', [])]
    raw['contradictions'] = [Sighting(**s) for s in raw.get('contradictions', [])]
    raw['grades']         = [Sighting(**s) for s in raw.get('grades', [])]
    return Claim(**raw)


class KnowledgeStore:
    def __init__(self, folder: str, clock=time.time, code: CodeClock | None = None):
        self.folder = Path(folder).expanduser()
        self.clock  = clock
        self.code   = code                  # None: no claim depends on code (the CLI, a test)
        self.folder.mkdir(parents=True, exist_ok=True)

        self.claims_path = self.folder / 'claims.yaml'
        self.log_path    = self.folder / 'log.jsonl'
        self.staged_dir  = self.folder / 'staged'       # <run_id>.json: the grades of a run, not committed yet

    # ========== Reading ==========

    def claims(self, tiers: tuple[str, ...] = TIERS) -> list[Claim]:
        return [claim for claim in self._load().values() if claim.tier in tiers]

    def get(self, claim_id: str) -> Claim:
        return _lookup(self._load(), claim_id)

    def check_live(self, claim_ids: list[str]) -> None:
        """Raises when one of the claims does not exist or is retired; one load for all"""
        claims = self._load()

        for claim_id in claim_ids:
            _lookup(claims, claim_id, live=True)

    def find(self, about: dict, key: str) -> Claim | None:
        """The live value claim with this subject and key"""
        return _match(self._load(), about_of(about), key)

    def guide(self) -> str:
        """For the task session: what the store is and its rules, made from the constants here, and an index of what
        the store holds; the session reads the claims it needs with the knowledge CLI"""
        live    = self._live()
        to_fact = next(n for n in range(1, 1000) if wilson_lower_bound(n, n) >= FACT_RELIABILITY)
        rules   = [
            f'- Tiers: {" -> ".join(LIVE)}. A claim becomes a belief when it is seen in {MIN_RUNS} runs by '
            f'{MIN_SOURCES} sources; your grade of a step that followed it ({OUTCOME}:<skill>) counts as evidence, '
            f'but it moves no tier. A belief becomes a fact after {to_fact} confirmations with no contradiction, and '
            f'only when the operator approves it.',
            '- What makes a claim lose trust: a contradiction moves it down one tier (not a grade); a claim with valid_for_s is '
            'retired that long after it was last seen; the operator can retire it.',
            '- A claim about a skill holds only for the code under that skill: its contract, the skills it runs, and '
            'the repos it uses. When that code changes, its evidence stops counting and it is an observation again, '
            'untried on this code. `knowledge show` gives the reason (moved) on each old sighting.',
        ]
        head    = ['## The knowledge store', '', 'What the robot has learned, as claims with evidence. Read the claims '
                   'about a skill before you use it, and again after a step fails.']

        held = Counter((claim.subject(), claim.tier) for claim in live)
        rows = {subject: [f'{held[subject, tier]} {tier}' for tier in LIVE if held[subject, tier]]
                for subject, _ in held}

        return '\n'.join([*head, f'- Read: `knowledge --dir {self.folder} list --about skill=<name>` (or place=, device=, '
                           f'object=; add --tier belief), and `knowledge --dir {self.folder} show <id>` for the evidence '
                           f'and its runs.', *rules, '',
                           'What it holds now:' if rows else 'It holds nothing yet.',
                           *(f'- {subject}: {", ".join(counts)}' for subject, counts in sorted(rows.items()))])

    def _live(self) -> list[Claim]:
        """The live claims, after expiry and the code clock: one lock and one load"""
        with self._locked(save=False) as claims:
            return [claim for claim in claims.values() if claim.tier in LIVE]

    # ========== Writing ==========

    def note(self, text: str, sighting: Sighting, about: dict | None = None, key: str | None = None,
             valid_for_s: float | None = None) -> tuple[Claim, str]:
        """An observation. A value claim (key) with the same subject that is still live gets a confirmation, or a
        contradiction when its value differs and is not numeric. Returns the claim and what happened"""
        about = about_of(about)

        with self._locked() as claims:
            existing = _match(claims, about, key) if key else None

            if existing is not None:
                agrees = _agrees(existing.value, sighting.value)
                change = self._sight(existing, sighting, confirms=agrees)
                return existing, change

            claim = Claim(id=_next_id(claims), text=text.strip(), about=about, key=key, valid_for_s=valid_for_s,
                          confirmations=[sighting])
            claim.under = self._snapshot(claim)
            claims[claim.id] = claim
            self._log('note', claim, sighting.by, text=claim.text)

            return claim, 'new observation'

    def sight(self, claim_id: str, sighting: Sighting, confirms: bool) -> str:
        with self._locked() as claims:
            return self._sight(_lookup(claims, claim_id, live=True), sighting, confirms)

    def confirm(self, claim_id: str, sighting: Sighting) -> str:
        return self.sight(claim_id, sighting, confirms=True)

    def contradict(self, claim_id: str, sighting: Sighting) -> str:
        return self.sight(claim_id, sighting, confirms=False)

    def grade(self, claim_id: str, sighting: Sighting) -> str:
        """The agent's grade of a claim that a step followed: useful or wrong is weak evidence; misleading or
        not-applicable is kept apart and changes no reliability"""
        with self._locked() as claims:
            return self._grade(_lookup(claims, claim_id, live=True), sighting)

    # ========== Staged grades: one file per run, committed after the run ==========

    def stage_applied(self, run_id: str, claim_ids: list[str], skill: str, step: int) -> None:
        """A step followed these claims: each gets an entry with no grade, with the code under it now. A later
        step that follows a claim again replaces its entry, and its grade must be given again"""
        claims = self._load()
        staged = self.staged(run_id)

        for claim_id in claim_ids:
            under            = self._snapshot(_lookup(claims, claim_id))
            staged[claim_id] = asdict(Sighting(by=f'{OUTCOME}:{skill}', time=self.clock(), run=run_id,
                                               evidence=[f'step:{step}'], under=under))
        self._write_staged(run_id, staged)

    def stage(self, run_id: str, claim_id: str, grade: str, reason: str) -> None:
        """A grade of a claim that a step of the run followed; the last grade wins"""
        _check_grade(grade)
        staged = self.staged(run_id)

        if not reason.strip():
            raise KnowledgeError('a grade needs a reason')

        if not staged:
            raise KnowledgeError(f'run {run_id} has nothing staged: it is committed, or no step of it followed a claim')

        if claim_id not in staged:
            raise KnowledgeError(f'no step of this run followed {claim_id}: grade only a claim that a run gave in applies')

        staged[claim_id] |= {'grade': grade, 'reason': reason, 'time': self.clock()}
        self._write_staged(run_id, staged)

    def staged(self, run_id: str) -> dict:
        path = self._staged_path(run_id)
        return json.loads(path.read_text()) if path.exists() else {}

    def staged_runs(self) -> list[str]:
        return sorted(path.stem for path in self.staged_dir.glob('*.json')) if self.staged_dir.exists() else []

    def commit(self, run_id: str) -> tuple[list[str], list[str]]:
        """The graded entries of a run go into the claims together, in one write. Returns what happened to each,
        and the claims with no grade"""
        staged   = self.staged(run_id)
        ungraded = [claim_id for claim_id, entry in staged.items() if not entry['grade']]
        said     = []

        with self._locked() as claims:
            for claim_id, entry in staged.items():
                if entry['grade']:
                    try:
                        claim = _lookup(claims, claim_id, live=True)

                        if any(s.run == run_id and s.grade for s in [*claim.sightings, *claim.grades]):
                            said.append(f'{claim_id} committed already')     # a crash after the save, before the unlink
                        else:
                            said.append(f'{claim_id} {self._grade(claim, Sighting(**entry))}')
                    except KnowledgeError as error:
                        said.append(f'{claim_id} not kept: {error}')

        self._staged_path(run_id).unlink(missing_ok=True)

        return said, ungraded

    def _grade(self, claim: Claim, sighting: Sighting) -> str:
        _check_grade(sighting.grade)
        changed        = self._since(claim, sighting.under)
        sighting.moved = f'graded on older code: {changed}' if changed else sighting.moved
        sighting.under = None                                 # checked: the claim keeps its own under
        counted        = f' (not counted: {sighting.moved})' if changed else ''

        if (confirms := GRADES[sighting.grade]) is not None:
            return f'{sighting.grade}: {self._sight(claim, sighting, confirms)}{counted}'

        claim.grades.append(sighting)
        self._log('grade', claim, sighting.by, grade=sighting.grade, reason=sighting.reason, run=sighting.run)

        return f'{sighting.grade}: kept apart, no evidence; make the text or tags narrower{counted}'

    def _staged_path(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise KnowledgeError(f'not a run id: {run_id!r}')

        return self.staged_dir / f'{run_id}.json'

    def set_aside(self, run_id: str) -> Path:
        """A staged file that cannot be committed: renamed .bad, for a person to look at"""
        path = self._staged_path(run_id)
        return path.replace(path.with_suffix('.bad'))

    def _write_staged(self, run_id: str, staged: dict) -> None:
        self.staged_dir.mkdir(exist_ok=True)
        write_json(self._staged_path(run_id), staged)

    def approve(self, claim_id: str, by: str, text: str | None = None) -> Claim:
        """The operator makes a ready belief a fact, with the exact text (edited or not)"""
        if by != OPERATOR:
            raise KnowledgeError('only the operator approves a fact')

        with self._locked() as claims:
            claim = _lookup(claims, claim_id, live=True)
            claim.check_ready()

            edited           = text is not None and text.strip() != claim.text
            claim.text       = text.strip() if text else claim.text
            claim.tier       = 'fact'
            claim.approved_by = by
            self._log('approve', claim, by, edited=edited, text=claim.text, reliability=claim.reliability())

            return claim

    def refuse(self, claim_id: str, by: str, reason: str):
        """The operator says no to a ready belief: it stays a belief, and the log keeps why"""
        with self._locked(save=False) as claims:
            self._log('refuse', _lookup(claims, claim_id, live=True), by, reason=reason)

    def retire(self, claim_id: str, by: str, reason: str):
        with self._locked() as claims:
            claim      = _lookup(claims, claim_id, live=True)
            claim.tier = 'retired'
            self._log('retire', claim, by, reason=reason)

    def expire(self) -> list[str]:
        """Retire the claims whose valid_for_s has passed since they were last seen, and stop counting the evidence
        of the claims whose code moved (every write does this too). Returns the retired ids"""
        with self._locked(save=False):             # it saves when something expired or moved
            return list(self._expired)

    def _rebase(self, claims: dict) -> bool:
        """A claim whose code moved goes back to an observation, with its sightings kept but not counted. So does
        a claim about a skill with no record of its code (written before the clock, or with no clock): what it was
        seen on cannot be known. Its next sighting starts a record"""
        if self.code is None:
            return False

        moved = False

        for claim in claims.values():
            skill = claim.about.get('skill')

            if not skill or claim.tier == 'retired':
                continue

            if claim.under is not None:
                reason = self.code.since(skill, claim.under)
            elif claim.tier != 'observation' or any(not s.moved for s in claim.sightings):
                reason = 'there is no record of the code it was seen on'
            else:
                continue                    # already moved, and not seen since

            if not reason:
                continue

            for sighting in [*claim.sightings, *claim.grades]:
                sighting.moved = sighting.moved or reason

            before                                   = claim.tier
            claim.tier, claim.under, claim.approved_by = 'observation', None, None
            moved                                    = True
            self._log('code_moved', claim, 'store', reason=reason, tier_before=before)

        return moved

    def _snapshot(self, claim: Claim) -> dict | None:
        skill = claim.about.get('skill')
        return self.code.snapshot(skill) if self.code is not None and skill else None

    def _since(self, claim: Claim, under: dict | None) -> str:
        """Why the code under the claim moved since the snapshot under, or ''"""
        return self.code.since(claim.about['skill'], under) if under and self.code is not None else ''

    def _expire(self, claims: dict) -> list[str]:
        retired = []

        for claim in claims.values():
            if claim.tier != 'retired' and claim.valid_for_s is not None and self.clock() - claim.last_seen > claim.valid_for_s:
                claim.tier = 'retired'
                retired.append(claim.id)
                self._log('expire', claim, 'store', valid_for_s=claim.valid_for_s)

        return retired

    # ========== Rules ==========

    def _sight(self, claim: Claim, sighting: Sighting, confirms: bool) -> str:
        (claim.confirmations if confirms else claim.contradictions).append(sighting)
        claim.under = claim.under or self._snapshot(claim)      # the first sighting on code that moved
        before      = claim.tier

        if not confirms and claim.tier in LIVE[1:] and not _weak(sighting):
            claim.tier = LIVE[LIVE.index(claim.tier) - 1]
        elif confirms and claim.tier == 'observation' and _runs(claim) >= MIN_RUNS and _sources(claim) >= MIN_SOURCES:
            claim.tier = 'belief'

        change = 'confirmed' if confirms else 'contradicted'

        if claim.tier != before:
            change += f', {before} -> {claim.tier}'

        if claim.ready_for_fact():
            change += ', ready for the operator to approve as a fact'

        self._log('confirm' if confirms else 'contradict', claim, sighting.by, tier_before=before, run=sighting.run,
                  value=sighting.value, reliability=claim.reliability(), grade=sighting.grade, reason=sighting.reason)

        return change

    # ========== Files ==========

    def _load(self) -> dict[str, Claim]:
        if not self.claims_path.exists():
            return {}

        raw = yaml.safe_load(self.claims_path.read_text()) or []

        return {claim.id: claim for claim in map(_claim_from, raw)}

    @contextmanager
    def _locked(self, save: bool = True):
        """Load under an exclusive lock, let the caller change the claims, save atomically"""
        with open(self.folder / '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            claims        = self._load()
            self._expired = self._expire(claims)    # read by expire(), under this lock
            rebased       = self._rebase(claims)

            yield claims

            save = save or bool(self._expired) or rebased

            if not save:
                return

            temporary = self.claims_path.with_suffix('.yaml.tmp')
            temporary.write_text(yaml.safe_dump([asdict(claim) for claim in claims.values()], sort_keys=False, allow_unicode=True))
            temporary.replace(self.claims_path)

    def _log(self, action: str, claim: Claim, by: str, **data):
        entry = {'time': self.clock(), 'action': action, 'claim': claim.id, 'tier': claim.tier, 'by': by, **data}

        with self.log_path.open('a') as file:
            file.write(json.dumps(entry, default=str) + '\n')


def _lookup(claims: dict, claim_id: str, live: bool = False) -> Claim:
    claim = claims.get(claim_id)

    if claim is None:
        raise KnowledgeError(f'no claim {claim_id}')

    if live and claim.tier == 'retired':
        raise KnowledgeError(f'{claim_id} is retired')

    return claim


def _match(claims: dict, about: dict, key: str) -> Claim | None:
    """The live value claim with this subject and key"""
    return next((c for c in claims.values() if c.about == about and c.key == key and c.tier != 'retired'), None)


def _next_id(claims: dict) -> str:
    numbers = [int(claim_id.split('-')[1]) for claim_id in claims]
    return f'C-{max(numbers, default=0) + 1:04d}'


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _independent(sightings: list[Sighting]) -> int:
    """One per run and source; a sighting with no run counts on its own. Grades (weak) count once per run, and
    not at all in a run that has another sighting already: an agent that notes a claim and grades it in one run has
    seen it once"""
    strong = {(s.run, s.by) if s.run else (s.time, s.by) for s in sightings if not _weak(s)}
    runs   = {run for run, _ in strong}
    weak   = {s.run or (s.time, s.by) for s in sightings if _weak(s) and s.run not in runs}
    return len(strong) + len(weak)


def _check_grade(grade: str | None) -> None:
    if grade not in GRADES:
        raise KnowledgeError(f'no grade {grade}; use {", ".join(GRADES)}')


def _runs(claim: Claim) -> int:
    return len({s.run for s in claim.counted.confirmations if s.run and not _weak(s)})


def _sources(claim: Claim) -> int:
    return len({s.by for s in claim.counted.confirmations if not _weak(s)})


def _weak(sighting: Sighting) -> bool:
    """A grade of a step that followed the claim: it counts in reliability, but it does not move a tier"""
    return sighting.by.startswith(f'{OUTCOME}:')


def _agrees(known, seen) -> bool:
    """A measured number never contradicts (it goes into the spread); a different yes/no or word does"""
    if known is None or seen is None or _numeric(seen) or isinstance(seen, list) and all(map(_numeric, seen)):
        return True

    return known == seen
