"""What the robot has learned, as claims that go up in tiers with evidence: observation -> belief -> fact.

A claim is one thing seen or measured, about open tags (a place by its map_info name, a skill, a device, ...) or
nothing. Every later sighting is a confirmation or a contradiction, with its evidence (a run, a record, who saw it).
The rules (docs/knowledge.md):

- observation -> belief: confirmed in 2 runs or more, by 2 sources or more (a check, an agent skill, the operator)
- belief -> fact: the Wilson lower bound of confirmations / (confirmations + contradictions) is at least
  FACT_RELIABILITY, and the operator approves it. Nobody else makes a fact.
- fact -> belief: one contradiction
- any -> retired: valid_for_s has passed since the claim was last seen, or the operator retires it

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

from pathlib import Path
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field


TIERS            = ('observation', 'belief', 'fact', 'retired')
FACT_RELIABILITY = 0.8
WILSON_Z         = 1.645            # one-sided 95 %: with no contradiction, 11 confirmations make a belief ready
MIN_RUNS         = 2
MIN_SOURCES      = 2
OPERATOR         = 'operator'
TAG_KIND         = re.compile(r'[a-z][a-z0-9_]*')


class KnowledgeError(ValueError):
    pass


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
    by:       str                       # 'operator', 'agent:press_judge', 'check:button_lit', ...
    time:     float
    run:      str | None = None         # the run_id of the recorder's bundle
    evidence: list[str] = field(default_factory=list)    # e.g. 'record:7', 'frame:press', 'E-019'
    value:    object = None


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

    def __post_init__(self):
        self.about = _clean(self.about)              # a claim written by hand in claims.yaml too; a bad kind still loads

    @property
    def last_seen(self) -> float:
        return max(s.time for s in [*self.confirmations, *self.contradictions])

    @property
    def value(self):
        """The latest value confirmed"""
        values = [s.value for s in self.confirmations if s.value is not None]
        return values[-1] if values else None

    def reliability(self) -> float:
        """Over independent sightings: one per run and source, so ten notes in one call count once"""
        good, bad = _independent(self.confirmations), _independent(self.contradictions)
        return wilson_lower_bound(good, good + bad)

    def ready_for_fact(self) -> bool:
        return self.tier == 'belief' and self.reliability() >= FACT_RELIABILITY

    def check_ready(self):
        if not self.ready_for_fact():
            raise KnowledgeError(f'{self.id} is a {self.tier} with reliability {self.reliability():.2f}; '
                                 f'a fact needs a belief at {FACT_RELIABILITY}')

    def value_stats(self) -> dict | None:
        """Mean and spread of numeric values, per axis for lists (e.g. a press offset)"""
        values = [s.value for s in self.confirmations if _numeric(s.value) or isinstance(s.value, list) and s.value and all(map(_numeric, s.value))]

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
    return Claim(**raw)


class KnowledgeStore:
    def __init__(self, folder: str, clock=time.time):
        self.folder = Path(folder).expanduser()
        self.clock  = clock
        self.folder.mkdir(parents=True, exist_ok=True)

        self.claims_path = self.folder / 'claims.yaml'
        self.log_path    = self.folder / 'log.jsonl'

    # ========== Reading ==========

    def claims(self, tiers: tuple[str, ...] = TIERS) -> list[Claim]:
        return [claim for claim in self._load().values() if claim.tier in tiers]

    def get(self, claim_id: str) -> Claim:
        return _lookup(self._load(), claim_id)

    def find(self, about: dict, key: str) -> Claim | None:
        """The live value claim with this subject and key"""
        return _match(self._load(), about_of(about), key)

    def prompt_section(self, observations: bool = False) -> str:
        """Facts and beliefs as a prompt section with its heading, for the planner and the agent; beliefs are marked
        as not certain. observations: also the live observations, so an agent can confirm one by its id. Empty when
        there are none"""
        self.expire()

        claims = self.claims(('fact', 'belief', 'observation'))
        lines  = []
        tiers  = [('fact', 'Facts (confirmed and approved)'), ('belief', 'Beliefs (seen more than once, not certain)')]

        if observations:
            tiers.append(('observation', 'Observations (seen, not confirmed; note with claim=<id> to confirm or contradict one)'))

        for tier, heading in tiers:
            rows = [c for c in claims if c.tier == tier]

            if rows:
                lines.append(f'{heading}:')
                lines += [f'- [{c.id}] ({c.subject()}) {c.text}' + (f' Value: {c.value}.' if c.key else '') for c in rows]

        return '## What is known (knowledge store)\n\n' + '\n'.join(lines) if lines else ''

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
            claims[claim.id] = claim
            self._log('note', claim, sighting.by, text=claim.text)

            return claim, 'new observation'

    def confirm(self, claim_id: str, sighting: Sighting) -> str:
        with self._locked() as claims:
            return self._sight(_lookup(claims, claim_id, live=True), sighting, confirms=True)

    def contradict(self, claim_id: str, sighting: Sighting) -> str:
        with self._locked() as claims:
            return self._sight(_lookup(claims, claim_id, live=True), sighting, confirms=False)

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
        """Retire the claims whose valid_for_s has passed since they were last seen (every write does this too)"""
        with self._locked():
            return list(self._expired)

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
        before = claim.tier

        if not confirms and claim.tier == 'fact':
            claim.tier = 'belief'
        elif confirms and claim.tier == 'observation' and _runs(claim) >= MIN_RUNS and _sources(claim) >= MIN_SOURCES:
            claim.tier = 'belief'

        change = 'confirmed' if confirms else 'contradicted'

        if claim.tier != before:
            change += f', {before} -> {claim.tier}'

        if claim.ready_for_fact():
            change += ', ready for the operator to approve as a fact'

        self._log('confirm' if confirms else 'contradict', claim, sighting.by, tier_before=before, run=sighting.run,
                  value=sighting.value, reliability=claim.reliability())

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

            yield claims

            save = save or bool(self._expired)

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
    """One per run and source; a sighting with no run counts on its own"""
    return len({(s.run, s.by) if s.run else (s.time, s.by) for s in sightings})


def _runs(claim: Claim) -> int:
    return len({s.run for s in claim.confirmations if s.run})


def _sources(claim: Claim) -> int:
    return len({s.by for s in claim.confirmations})


def _agrees(known, seen) -> bool:
    """A measured number never contradicts (it goes into the spread); a different yes/no or word does"""
    if known is None or seen is None or _numeric(seen) or isinstance(seen, list) and all(map(_numeric, seen)):
        return True

    return known == seen
