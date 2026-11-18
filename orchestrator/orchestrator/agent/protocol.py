"""What the executor and the agent bridge agree on: the link's framing and protocol, and each agent skill's spec.

The executor (agent/) and the bridge on the laptop (mission_control/agent_bridge.py) both import this module. It and
every agent skill's spec.py and prompt.py import only the standard library and each other, so the bridge loads them
from its checkout without ROS.

An agent skill's spec.py (in its skill folder) states:
  PHASES       phase -> the decisions it may end with
  GOING_ON     the decisions after which the tree goes on (the skill succeeds)
  FIELDS       decision fields beside decision and reason, name -> JSON type
  ANSWERED_BY  'model' (the bridge asks the model) or 'operator' (the bridge asks in its terminal)
  limits()     the limits the executor enforces, which the call carries so the prompt can state them

Each call carries PROTOCOL and its skill's limits. The bridge refuses a call of another protocol, so a laptop and a NUC
on different checkouts fail the call with a plain reason instead of drifting apart.
"""

import struct
import importlib

from types import ModuleType

from ..skills.loader import DEFAULT_PACKAGES, SkillLibrary


PROTOCOL   = 2                          # raise it when a call or an answer changes shape; both ends must match
HEADER     = struct.Struct('!II')       # the JSON length, then the length of the binary after it
MAX_JSON   = 16_000_000
MAX_BINARY = 64_000_000
PING_S     = 5.0                        # both ends ping this often


def schema(fields: dict, decisions) -> dict:
    """The JSON schema of one answer: one of decisions, a reason, and the skill's fields"""
    properties = {'decision': {'type': 'string', 'enum': list(decisions)}, 'reason': {'type': 'string'}}
    properties.update({name: {'type': kind} for name, kind in fields.items()})

    return {'type': 'object', 'properties': properties, 'required': ['decision', 'reason']}


def load_spec(skill: str, packages=DEFAULT_PACKAGES) -> ModuleType:
    module = SkillLibrary(packages).module(skill, 'spec')

    if module is None:
        raise ModuleNotFoundError(f'agent skill {skill} has no spec.py')

    return module


def load_prompt(skill: str, packages=DEFAULT_PACKAGES) -> ModuleType | None:
    return SkillLibrary(packages).module(skill, 'prompt')


def agent_skills(packages=DEFAULT_PACKAGES) -> dict[str, ModuleType]:
    """Every agent skill of the packages by name: the folders with a spec.py"""
    library = SkillLibrary(packages)
    found   = {}

    for package, directory in zip(library.packages, library.directories()):
        for folder in sorted(directory.iterdir()):
            if (folder / 'spec.py').exists():
                found[folder.name] = importlib.import_module(f'{package}.{folder.name}.spec')

    return found
