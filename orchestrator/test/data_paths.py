"""Copies of main's contracts, trees and agent skills that the tests use (skill_registry/test/data)"""

from pathlib import Path

DATA         = Path(__file__).resolve().parents[2] / 'skill_registry' / 'test' / 'data'
CONTRACTS    = DATA / 'contracts'
TREES        = DATA / 'trees'
AGENT_SKILLS = DATA / 'agent_skills'
