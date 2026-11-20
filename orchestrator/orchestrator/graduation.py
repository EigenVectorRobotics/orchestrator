"""A skill's maturity as it earned it: made from the agent's grades of its steps (docs/skill-packages.md, "Graduation").

Decided 2026-11-19: the earned maturity is computed from step_grades.jsonl when it is read, and no file changes. Only
grades of steps on the real robot count: the fake robot tests the plumbing, not the skill. Only worked and failed
count, and only for the version that is on disk now (step_grades.py). Reliability is the Wilson bound at 80 %:

  untested    fewer grades than below, or not reliable enough yet
  wip         n >= 5 and the lower bound >= 0.6
  tested      n >= 10, the lower bound >= 0.8, and no failure in the last 10: a failure takes it down to wip
  deprecated  n >= 10 and the upper bound <= 0.2: it is reliably bad

The catalog and the Skills tab show effective(). It is the higher of the contract's maturity (a person's word) and the
earned one. deprecated always wins, and only a skill can earn it: a contract cannot say it. A composition (a code
skill or a tree skill) is never more trusted than its weakest part: what it shows is at most the lowest that its
parts show, and a deprecated part counts as untested there.

"The last 10" are the last 10 grades by the time the agent gave them, not by the order of the commits.
"""

import threading

from typing import Callable

from skill_registry.contract import MATURITIES

from .knowledge import wilson_lower_bound
from .recording import read_lines
from .step_grades import STEP_GRADES, StepGrades


Z          = 1.2816                # the Wilson bound at 80 %: 10 of 10 gives 0.86, 5 of 5 gives 0.75
WIP_N      = 5
WIP_MIN    = 0.6
TESTED_N   = 10                    # also the last uses that must have no failure
TESTED_MIN = 0.8
BAD_N      = 10
BAD_MAX    = 0.2
ORDER      = MATURITIES[::-1]      # untested, wip, tested


def earn(outcomes: list[bool]) -> str:
    """The maturity a list of worked (True) and failed (False) grades earns, oldest first"""
    n, worked = len(outcomes), sum(outcomes)
    lower     = wilson_lower_bound(worked, n, Z)
    upper     = 1 - wilson_lower_bound(n - worked, n, Z)        # the upper bound of the rate is 1 - that of failures

    if n >= BAD_N and upper <= BAD_MAX:
        return 'deprecated'

    if n >= TESTED_N and lower >= TESTED_MIN and all(outcomes[-TESTED_N:]):
        return 'tested'

    if n >= WIP_N and lower >= WIP_MIN:
        return 'wip'

    return 'untested'


def effective(written: str, earned: str) -> str:
    """The maturity to show: a person's word or the earned one, the higher; deprecated always wins"""
    return earned if earned == 'deprecated' else max(written, earned, key=ORDER.index)


def rank(maturity: str) -> int:
    """How far up a maturity is; a deprecated part counts as untested, because a composition earns its own"""
    return ORDER.index(maturity) if maturity in ORDER else 0


class Graduation:
    """The earned maturity of each skill, from the step grade log; grouped again only when the log changes"""

    def __init__(self, steps: StepGrades, composition: Callable[[str], dict] = lambda name: {},
                 written_of: Callable[[str], str | None] = lambda name: None):
        self.steps       = steps                 # its log, and the version of a skill on disk now: only its grades count
        self.composition = composition           # a composition's parts at every depth (skill_review.composition)
        self.written_of  = written_of            # a part's contract maturity; None: the library does not have it
        self._lock       = threading.Lock()
        self._lines      = None                  # the list read_lines gave last: the same list while the log is the same
        self._grades     = {}                    # (skill, version) -> [True | False], oldest first

    def of(self, skill: str, written: str, written_of: Callable[[str], str | None] | None = None) -> dict:
        """{maturity (to show), earned, worked, failed, capped_by} of the skill's version now, on the robot. written:
        the contract's maturity. capped_by: the weakest part of a composition, when it holds the composition down.
        written_of: the parts' contract maturities, when the caller has them already"""
        found = self._earned(skill)
        shown = effective(written, found['earned'])
        found |= {'maturity': shown, 'capped_by': ''}

        for part in self.composition(skill).get('calls', []):
            if (part_written := (written_of or self.written_of)(part)) is None:
                continue                         # a part the library does not have: it holds nothing down

            part_shown = effective(part_written, self._earned(part)['earned'])

            if rank(part_shown) < rank(found['maturity']):
                found |= {'maturity': ORDER[rank(part_shown)], 'capped_by': part}

        return found

    def _earned(self, skill: str) -> dict:
        try:
            version = self.steps.version(skill)
        except OSError:                          # e.g. its folder went while it was read: no grade is of it
            version = None

        outcomes = self._read().get((skill, version), []) if version else []
        return {'earned': earn(outcomes), 'worked': sum(outcomes), 'failed': len(outcomes) - sum(outcomes)}

    def _read(self) -> dict:
        with self._lock:                         # read inside: a thread with an older list must not build from it
            lines = read_lines(self.steps.log_path)

            if lines is not self._lines:
                graded = {}

                for entry in lines:
                    skill, version, grade = entry.get('skill'), entry.get('version'), entry.get('grade')

                    if (entry.get('robot') is True and all(isinstance(value, str) for value in (skill, version, grade))
                            and STEP_GRADES.get(grade) is not None and isinstance(entry.get('time'), (int, float))):
                        graded.setdefault((skill, version), []).append((entry['time'], STEP_GRADES[grade]))

                self._grades = {key: [ok for _, ok in sorted(found, key=lambda pair: pair[0])] for key, found in graded.items()}
                self._lines  = lines

            return self._grades
