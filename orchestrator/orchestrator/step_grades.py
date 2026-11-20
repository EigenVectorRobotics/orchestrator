"""The agent's grade of each step's skill: the evidence a skill graduates on (docs/skill-packages.md, "Credit assignment").

A step's raw end says little about its skill: a success can mean no more than "the message was published", and a
failure can be the agent's choice or a device that was down. So the agent grades each step that ran, and the grades
count, not the ends. It is built like the grades of claims (knowledge.py): one staged file per run, graded during the
task or after it, and committed together after the task (agent_cli commit, or the next task's open).

  worked   the skill did what the subgoal needed, and the agent saw it       a success of that version
  failed   the skill did not do its job, and the skill is at fault           a failure of that version
  misused  the agent chose the wrong skill or gave it wrong inputs           no evidence
  blocked  a device or the place was the cause (the Orin was down, say)      no evidence

A grade belongs to the version of the skill at its step (skill_review.grade_version), so the record of an old version
never graduates a new one. The committed grades go to one log, step_grades.jsonl, next to the knowledge store.
"""

import json
import time

from pathlib import Path
from typing import Callable

from .knowledge import KnowledgeError, StagedRuns
from .recording import append_line


STEP_GRADES = {'worked': True, 'failed': False, 'misused': None, 'blocked': None}   # True: a success, False: a failure
STAGED      = 'staged_steps'
LOG         = 'step_grades.jsonl'
TAIL        = 256 * 1024             # bytes at the end of the log that a run's last commit can be in


def _key(step: int) -> str:
    """How a step is named in the staged file and to the agent"""
    return f'step {step}'


class StepGrades(StagedRuns):
    def __init__(self, folder, version: Callable[[str], str | None] = lambda name: None, clock=time.time):
        self.folder     = Path(folder).expanduser()
        self.staged_dir = self.folder / STAGED
        self.log_path   = self.folder / LOG
        self.version    = version                # a skill's version now: what a grade of its step is of
        self.clock      = clock

    def stage_step(self, run_id: str, step: int, skill: str, version: str | None, end: str) -> None:
        """A step ran: an entry with no grade, for the version of its skill when the step started (self.version then);
        a write_skill during the step must not get the grade of the version that ran"""
        staged             = self.staged(run_id)
        staged[_key(step)] = {'step': step, 'skill': skill, 'version': version, 'end': end, 'grade': None,
                              'reason': '', 'time': None}
        self._write_staged(run_id, staged)

    def stage(self, run_id: str, step: int, grade: str, reason: str) -> None:
        """The agent's grade of a step of the run; the last grade wins"""
        if grade not in STEP_GRADES:
            raise KnowledgeError(f'{grade!r} is not a step grade; use {", ".join(STEP_GRADES)}')

        if not reason.strip():
            raise KnowledgeError('a grade needs a reason')

        if not isinstance(step, int) or isinstance(step, bool):
            raise KnowledgeError(f'step is a whole number (agent_cli grade-step 3 ...), not {step!r}')

        staged = self.staged(run_id)

        if _key(step) not in staged:
            raise KnowledgeError(f'step {step} of run {run_id} has nothing to grade: it did not run, or it is committed')

        staged[_key(step)] |= {'grade': grade, 'reason': reason.strip(), 'time': self.clock()}
        self._write_staged(run_id, staged)

    def commit(self, run_id: str) -> tuple[list[str], list[str]]:
        """The graded steps of a run go to the log. Returns what was kept, and the steps with no grade"""
        staged         = self.staged(run_id)
        said, ungraded = [], []
        logged         = self._logged(run_id) if any(entry['grade'] for entry in staged.values()) else set()

        for key, entry in staged.items():
            if not entry['grade']:
                ungraded.append(key)
            elif entry['step'] in logged:                    # a crash after the log write, before the unlink
                said.append(f'{key} {entry["skill"]}: committed already')
            else:
                append_line(self.log_path, {'run': run_id, **entry})
                said.append(f'{key} {entry["skill"]}: {entry["grade"]}')

        self._staged_path(run_id).unlink(missing_ok=True)
        return said, ungraded

    def _logged(self, run_id: str) -> set[int]:
        """The steps of this run in the log already. A commit writes a run's lines together at the end, so a crash
        before the unlink leaves them in the last TAIL bytes: the rest of the log is not read"""
        try:
            with self.log_path.open('rb') as file:
                file.seek(max(0, file.seek(0, 2) - TAIL))
                lines = file.read().splitlines()
        except FileNotFoundError:
            return set()

        logged = set()

        for line in lines:
            try:
                entry = json.loads(line)

                if entry['run'] == run_id:
                    logged.add(entry['step'])
            except (ValueError, KeyError, TypeError):
                continue                                      # a torn line, or the cut first line

        return logged
