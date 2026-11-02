"""Turn a task into a valid behavior tree: the LLM writes it, the validator checks it, errors go back to the LLM"""

import re

from typing import Callable
from dataclasses import dataclass, field

from skill_registry import DEFAULT_ESSENTIAL_DEVICES, Contract, Registry, TreeValidator, ValidatorConfig

from .llm_backends import Backend, BackendError


TREE_PATTERN = re.compile(r'<tree>(.*?)</tree>', re.DOTALL)

EXAMPLE_TREE = '''<root BTCPP_format="4">
  <BehaviorTree ID="greet_at_door">
    <Sequence>
      <navigate_to goal="{elevator_door}"/>
      <Fallback>
        <RetryUntilSuccessful num_attempts="10">
          <Delay delay_msec="1000">
            <door_open/>
          </Delay>
        </RetryUntilSuccessful>
        <ForceFailure>
          <say text="The door is closed."/>
        </ForceFailure>
      </Fallback>
    </Sequence>
  </BehaviorTree>
</root>'''

SYSTEM_PROMPT = '''You plan tasks for a Unitree G1 humanoid robot. You write the plan as one behavior tree in BehaviorTree.CPP v4 XML. A validator checks the tree, and then the robot runs it. You do not see the robot while it runs, so the tree must handle failures itself.

# Tree format

- The root is <root BTCPP_format="4"> with one or more <BehaviorTree ID="..."> elements. The first one is the main tree. <SubTree ID="..."/> runs another one.
- Control nodes: Sequence, SequenceWithMemory, ReactiveSequence, Fallback, ReactiveFallback, Parallel (attributes success_count, failure_count), IfThenElse (2 or 3 children).
- Decorators (exactly one child): Inverter, ForceSuccess, ForceFailure, RetryUntilSuccessful (num_attempts), Repeat (num_cycles), KeepRunningUntilFailure, Timeout (msec), Delay (delay_msec), RunOnce.
- Built-in leaves: AlwaysSuccess, AlwaysFailure, Sleep (msec), SetBlackboard (value, output_key).
- Every other leaf is a skill from the catalog below, written as <skill_name port="value"/>. Give every required input. A value in braces, like "{call_panel}", reads the blackboard; an output port with "{key}" writes it.
- A pose literal is "x y yaw" in metres and radians in the map frame. Prefer the named places.

# Rules

- Use only skills from the catalog. If the task needs a skill that does not exist, say so and write the best tree you can with what exists.
- Skills on a device that can drop out (see "can drop out" below) must sit in a Fallback with a recovery child after them.
- Two branches of a Parallel must not use the same resource.
- Put a Delay of at least 1000 ms between attempts of a VLM skill; each question takes about a second.
- If the goal is not reached, the tree must end in failure. Do not hide a failure behind a Fallback that always succeeds; use ForceFailure on the recovery when the task did not happen.
- Asking a person for help with the say skill is a good recovery.
- A tree for "blind" mode runs with no internet. It cannot use skills that need the cloud.

# Your answer

First, in a few short sentences, say what the robot will do and how it handles the likely failures. Then give the tree inside <tree></tree> tags, with no other text inside the tags.

# Example

<tree>
''' + EXAMPLE_TREE + '''
</tree>

# Skill catalog

'''


def describe_contract(contract: Contract, essential_devices: set[str]) -> str:
    def describe_port(port) -> str:
        text = f'{port.name}: {port.type}'

        if port.choices:
            text += f' one of {port.choices}'

        if not port.required:
            text += f' (optional, default {port.default})'

        if port.description:
            text += f' - {port.description}'

        return text

    lines = [f'## {contract.name} ({contract.kind})', contract.description]

    if contract.inputs:
        lines.append('inputs: ' + '; '.join(describe_port(port) for port in contract.inputs))

    if contract.outputs:
        lines.append('outputs: ' + '; '.join(describe_port(port) for port in contract.outputs))

    if contract.pre:
        lines.append(f'needs before: {", ".join(contract.pre)}')

    if contract.check:
        lines.append(f'check after: {contract.check}')

    if contract.resources:
        lines.append(f'resources: {", ".join(contract.resources)}')

    if contract.timeout_s:
        lines.append(f'timeout: {contract.timeout_s:g} s')

    if contract.device not in essential_devices:
        lines.append(f'runs on {contract.device}: can drop out')

    if contract.needs_cloud:
        lines.append('needs the cloud')

    lines.append(f'maturity: {contract.maturity}')

    return '\n'.join(line for line in lines if line)


def build_system_prompt(registry: Registry, essential_devices: set[str]) -> str:
    """The same text for the same registry, so the provider can cache it"""
    catalog = '\n\n'.join(
        describe_contract(contract, essential_devices)
        for contract in sorted(registry, key=lambda contract: contract.name)
    )

    return SYSTEM_PROMPT + catalog


def build_task_message(task: str, places: dict, live_skills: set[str] | None, blind: bool) -> str:
    lines = [f'Task: {task}', '']

    if places:
        lines.append('Named places (on the blackboard): ' + ', '.join(sorted(places)))

    if live_skills is not None:
        lines.append('Skills that are up now (use only these): ' + ', '.join(sorted(live_skills)))

    lines.append('Mode: blind (no internet while it runs)' if blind else 'Mode: online')

    return '\n'.join(lines)


def extract_tree(text: str) -> str | None:
    match = TREE_PATTERN.search(text)

    return match.group(1).strip() if match else None


@dataclass
class PlanResult:
    ok:       bool
    tree:     str = ''
    attempts: int = 0
    message:  str = ''
    warnings: list[str] = field(default_factory=list)


class Planner:
    def __init__(
        self,
        backend: Backend,
        registry: Registry,
        essential_devices: set[str] | None = None,
        max_attempts: int = 3,
        on_event: Callable[..., None] | None = None
    ):
        self.backend           = backend
        self.registry          = registry
        self.essential_devices = essential_devices or set(DEFAULT_ESSENTIAL_DEVICES)
        self.max_attempts      = max_attempts
        self.on_event          = on_event or (lambda kind, text, **data: None)
        self.system_prompt     = build_system_prompt(registry, self.essential_devices)

    def plan(self, task: str, places: dict | None = None, live_skills: set[str] | None = None, blind: bool = False) -> PlanResult:
        validator = TreeValidator(
            self.registry,
            ValidatorConfig(
                essential_devices=self.essential_devices,
                blind=blind,
                live_skills=live_skills,
                known_keys=None if places is None else set(places)
            )
        )
        messages = [{'role': 'user', 'content': build_task_message(task, places or {}, live_skills, blind)}]

        self.on_event('task', task, blind=blind, backend=self.backend.name)

        for attempt in range(1, self.max_attempts + 1):
            try:
                reply = self.backend.complete(self.system_prompt, messages)
            except BackendError as error:
                self.on_event('error', str(error), level='error')
                return PlanResult(False, attempts=attempt, message=str(error))

            messages.append(reply.assistant)

            if reply.reasoning:
                self.on_event('reasoning', reply.reasoning, attempt=attempt)

            explanation = TREE_PATTERN.sub('', reply.text).strip()

            if explanation:
                self.on_event('plan', explanation, attempt=attempt)

            tree = extract_tree(reply.text)

            if tree is None:
                feedback = 'Your answer has no <tree></tree> block. Give the full tree inside the tags.'
                self.on_event('validation', 'no tree in the answer', level='warn', attempt=attempt)
                messages.append({'role': 'user', 'content': feedback})
                continue

            self.on_event('tree', f'tree, attempt {attempt}', tree=tree, attempt=attempt, usage=reply.usage)

            issues   = validator.validate(tree)
            errors   = [issue for issue in issues if issue.level == 'error']
            warnings = [f'{issue.path}: {issue.message}' for issue in issues if issue.level == 'warning']

            if not errors:
                self.on_event('valid', f'tree is valid after {attempt} attempt(s)', warnings=warnings)
                return PlanResult(True, tree, attempt, warnings=warnings)

            report = '\n'.join(f'- {issue.path}: {issue.message}' for issue in errors)

            self.on_event('validation', f'{len(errors)} errors:\n{report}', level='warn', attempt=attempt)
            messages.append({
                'role':    'user',
                'content': f'The validator rejected the tree:\n{report}\nFix these errors and give the full tree again.',
            })

        message = f'no valid tree after {self.max_attempts} attempts'
        self.on_event('error', message, level='error')

        return PlanResult(False, attempts=self.max_attempts, message=message)
