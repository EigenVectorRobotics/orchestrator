"""The made-up skills that the tests use (skill_registry/test/skills, one folder per skill)"""

from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / 'skill_registry' / 'test' / 'skills'
