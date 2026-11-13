"""Turn a task into a valid behavior tree: the LLM writes it, the validator checks it, errors go back to the LLM"""

import re

from xml.etree import ElementTree
from typing import Callable
from pathlib import Path
from dataclasses import dataclass, field

from skill_registry import DEFAULT_ESSENTIAL_DEVICES, Contract, Registry, TreeValidator, ValidatorConfig, load_tree

from .llm_backends import Backend, BackendError


TREE_PATTERN     = re.compile(r'<tree>(.*?)</tree>', re.DOTALL)
USE_TREE_PATTERN = re.compile(r'<use_tree\s+id="([^"]+)"((?:\s+\w+="[^"]*")*)\s*(?:/>|>(.*?)</use_tree>)', re.DOTALL)
REPLACE_PATTERN  = re.compile(r'<replace\s+subtree="([^"]+)"\s+with="([^"]+)"\s*/>')
BLOCK_PATTERN    = re.compile(r'<BehaviorTree\s+ID="([^"]+)"\s*>.*?</BehaviorTree>', re.DOTALL)
ATTRIBUTE_PATTERN  = re.compile(r'(\w+)="([^"]*)"')
PARAMETERS_PATTERN = re.compile(r'<Sequence\s+name="parameters"\s*>(.*?)</Sequence>', re.DOTALL)
PARAMETER_PATTERN  = re.compile(r'<SetBlackboard((?:\s+\w+="[^"]*")*)\s*/>')
TREE_ID_PATTERN  = re.compile(r'<BehaviorTree\s+ID="([^"]+)"')
COMMENT_PATTERN  = re.compile(r'<!--(.*?)-->', re.DOTALL)
SUMMARY_CHARS    = 160

TREE_LIBRARY_HEADER = '''

# Existing trees

These trees exist already. If one of them does the task as it is, answer <use_tree id="..."/> with its ID instead of writing a tree, after your short explanation. Otherwise write a new tree.
A tree with parameters lists them with their default values, as id(name="value", ...). If the tree does the task with other values, give them as attributes: <use_tree id="..." name="value"/>; the parameters you leave out keep their defaults. A place is written {place_name}. A parameter named <name>_floor is the floor of the place in parameter <name>: leave it out, and the planner sets it from the place.
A tree also lists its subtrees, with the blackboard keys each one reads and writes. To use a tree with one of its subtrees swapped for another existing tree (one that reads and writes the same keys), write <use_tree id="..." name="value"><replace subtree="subtree_id" with="other_tree_id"/></use_tree>; several replace lines are fine.

'''

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
- Recover by escalating, not by repeating. A retry with the same inputs helps only with a transient failure, so retry a step that way at most once. Then redo the step before it (walk to the place again, then try again). Then try another way if one exists, such as the other arm (left_arm) or another place. If that also fails, let the tree fail.
- The robot must stand still while an arm moves. Put a Sleep of at least 1000 ms between navigate_to and an arm skill, so the robot has stopped swaying.
- Name the exact target in a text input. press_button's target_description is a grounding query: "up button", "down button", "floor 1", "floor 2", "floor 3", or "object:<description>"; other text is refused.
- Asking a person for help with the say skill is a good recovery.
- When the task enters an elevator, the door can open at any time. Put the walk to the call panel, the call button press and the wait for the door in a ReactiveFallback whose first child is door_open, so an open door halts them at once. Right after it, say that the door is open and the robot walks in. Then run release_arms and the walk into the cabin in a Parallel, with a Delay of 1000 ms before the walk. End the wait with a Sleep and AlwaysFailure, so a door that stays closed fails the tree.
- To leave the elevator, do the same in the cabin: put the floor button press and the wait for the ride in a ReactiveFallback whose first child is a Sequence of inside_cabin, at_floor with the target floor number and door_open. Right after it, say that the robot is at the floor and walks out. Then, in a Parallel: release_arms at once (in ForceSuccess: the arms can be released already), a wait until in_elevator is false (slam_elevatorlio ends LIO's elevator mode at the open door), a wait until localized, and the walk out: first to the door-out place of the target floor (1 m out of the door), say that the robot stops to relocalize, relocalize_here, then on to the target floor's target place (target_floor_N, or the place the task names; its z is that floor).
- A goal place on another floor than the robot (the task message gives the floor of each place and of the robot) needs the freight elevator; a goal on the robot's floor does not.
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


SPEECH_CHARS_PER_S = 13.0          # about how fast the Unitree TTS speaks English
SPEECH_GAP_S       = 0.5


def speech_left_s(
    text: str,
    started: float,
    now: float,
) -> float:
    """Seconds until a sentence that started at started is spoken, plus a gap; 0 when it is done."""
    return max(0.0, started + len(text) / SPEECH_CHARS_PER_S + SPEECH_GAP_S - now)


def summarize_tree(
    xml: str,
) -> tuple[str, str]:
    """The main tree's ID and the first sentence of the file's first comment"""
    tree_id = TREE_ID_PATTERN.search(xml)
    comment = COMMENT_PATTERN.search(xml)
    text    = ' '.join(comment.group(1).split()) if comment else ''
    first   = text.split('. ')[0].rstrip('.')
    first   = first if len(first) <= SUMMARY_CHARS else first[:SUMMARY_CHARS].rsplit(' ', 1)[0] + ' …'

    return (tree_id.group(1) if tree_id else ''), first


def tree_parameters(
    xml: str,
) -> dict[str, str]:
    """Parameter name -> default value, from the SetBlackboard nodes in the Sequence named parameters"""
    block = PARAMETERS_PATTERN.search(xml)

    if not block:
        return {}

    attributes = [dict(ATTRIBUTE_PATTERN.findall(match)) for match in PARAMETER_PATTERN.findall(block.group(1))]
    return {each['output_key']: each['value'] for each in attributes if 'output_key' in each and 'value' in each}


def override_parameters(
    xml: str,
    values: dict[str, str],
) -> str:
    """The tree with the given parameters set to new values"""
    block = PARAMETERS_PATTERN.search(xml)
    text  = PARAMETER_PATTERN.sub(lambda match: _override_one(match.group(0), values), block.group(1))

    return xml[:block.start(1)] + text + xml[block.end(1):]


def _override_one(
    element: str,
    values: dict[str, str],
) -> str:
    attributes = dict(ATTRIBUTE_PATTERN.findall(element))
    name       = attributes.get('output_key')

    if name not in values or 'value' not in attributes:
        return element

    return re.sub(r'(\svalue=)"[^"]*"', lambda match: f'{match.group(1)}"{values[name]}"', element, count=1)


def fill_floor_parameters(
    xml: str,
    floors: dict[str, str],
    given: set[str],
) -> tuple[str, dict[str, str], list[str]]:
    """Set each <name>_floor parameter to the floor of the place in parameter <name>; a given value that differs is an error"""
    parameters = tree_parameters(xml)
    values     = {}
    errors     = []

    for name, value in parameters.items():
        place = parameters.get(name.removesuffix('_floor'), '') if name.endswith('_floor') else ''
        key   = place[1:-1] if place.startswith('{') and place.endswith('}') else ''
        floor = floors.get(key)

        if floor is None or value == floor:
            continue

        if name in given:
            errors.append(f'{name}="{value}", but {{{key}}} is on floor {floor}')
        else:
            values[name] = floor

    return (override_parameters(xml, values) if values else xml), values, errors


def subtree_interfaces(
    xml: str,
    registry: Registry,
) -> dict[str, tuple[list[str], list[str]]]:
    """Subtree ID -> (blackboard keys it reads, keys it writes), with the subtrees it calls, for every BehaviorTree but the main one"""
    own = {}

    for tree_id, block in [(match.group(1), match.group(0)) for match in BLOCK_PATTERN.finditer(xml)]:
        reads, writes, calls = [], [], []

        for element in ElementTree.fromstring(block).iter():
            if element.tag == 'SubTree':
                calls.append(element.get('ID'))

            contract = registry.get(element.tag)
            outputs  = {port.name for port in contract.outputs} if contract else set()

            for name, value in element.attrib.items():
                key = value[1:-1] if value.startswith('{') and value.endswith('}') else ''

                if element.tag == 'SetBlackboard' and name == 'output_key':
                    writes.append(value.strip('{}'))
                elif key and name in outputs:
                    writes.append(key)
                elif key:
                    reads.append(key)

        own[tree_id] = (reads, writes, calls)

    return {tree_id: _interface(tree_id, own, ()) for tree_id in list(own)[1:]}


def _interface(
    tree_id: str,
    own: dict[str, tuple[list[str], list[str], list[str]]],
    visiting: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """The keys a subtree reads before it writes them, and the keys it writes, its called subtrees included"""
    reads, writes, calls = own[tree_id]
    reads, writes        = list(reads), list(writes)

    for called in calls:
        if called in own and called not in visiting:
            called_reads, called_writes = _interface(called, own, (*visiting, tree_id))
            reads, writes = reads + called_reads, writes + called_writes

    return [key for key in dict.fromkeys(reads) if key not in writes], list(dict.fromkeys(writes))


def replace_subtree(
    xml: str,
    subtree_id: str,
    other_xml: str,
) -> str:
    """The tree with subtree_id swapped for the main tree of other_xml; other_xml's own subtrees are added"""
    blocks       = {match.group(1): match for match in BLOCK_PATTERN.finditer(xml)}
    other_blocks = [(match.group(1), match.group(0)) for match in BLOCK_PATTERN.finditer(other_xml)]
    main_id, main_block = other_blocks[0]
    added        = []

    for tree_id, block in other_blocks[1:]:
        if tree_id in blocks and tree_id != subtree_id and blocks[tree_id].group(0) != block:
            raise ValueError(f'both trees have a different subtree {tree_id}')

        if tree_id not in blocks:
            added.append(block)

    target = blocks[subtree_id]
    new    = main_block.replace(f'ID="{main_id}"', f'ID="{subtree_id}"', 1)
    xml    = xml[:target.start()] + new + xml[target.end():]
    end    = xml.rindex('</root>')

    return xml[:end] + ''.join(f'  {block}\n' for block in added) + xml[end:]


def load_tree_library(
    directory: str | Path,
) -> dict[str, tuple[str, str]]:
    """Tree ID -> (summary, XML) for every tree file in directory"""
    library = {}

    for path in sorted(Path(directory).glob('*.xml')):
        xml              = load_tree(path)
        tree_id, summary = summarize_tree(xml)

        if tree_id:
            library[tree_id] = (summary, xml)

    return library


def _signature(
    xml: str,
) -> str:
    """(name="default", ...) for a tree with parameters, else nothing"""
    parameters = tree_parameters(xml)

    return '(' + ', '.join(f'{name}="{value}"' for name, value in parameters.items()) + ')' if parameters else ''


def _subtree_lines(
    xml: str,
    registry: Registry,
) -> str:
    """One line per subtree: its ID and the keys it reads and writes"""
    lines = ''

    for tree_id, (reads, writes) in subtree_interfaces(xml, registry).items():
        keys   = '; '.join(part for part in (f'reads {", ".join(reads)}' if reads else '', f'writes {", ".join(writes)}' if writes else '') if part)
        lines += f'\n  - subtree {tree_id}' + (f' ({keys})' if keys else '')

    return lines


def build_system_prompt(
    registry: Registry,
    essential_devices: set[str],
    library: dict[str, tuple[str, str]] | None = None,
) -> str:
    """The same text for the same registry and trees, so the provider can cache it"""
    catalog = '\n\n'.join(
        describe_contract(contract, essential_devices)
        for contract in sorted(registry, key=lambda contract: contract.name)
        if contract.maturity != 'disabled'
    )

    if not library:
        return SYSTEM_PROMPT + catalog

    trees = '\n'.join(
        f'- {tree_id}{_signature(xml)}: {summary}' + _subtree_lines(xml, registry)
        for tree_id, (summary, xml) in sorted(library.items())
    )

    return SYSTEM_PROMPT + catalog + TREE_LIBRARY_HEADER + trees


def build_task_message(
    task: str,
    places: dict,
    live_skills: set[str] | None,
    blind: bool,
    floors: dict[str, str] | None = None,
    robot_floor: str | None = None,
) -> str:
    floors = floors or {}
    lines  = [f'Task: {task}', '']

    if places:
        lines.append('Named places (on the blackboard): ' + ', '.join(f'{key} (floor {floors[key]})' if key in floors else key for key in sorted(places)))

    if robot_floor is not None:
        lines.append(f'The robot is on floor {robot_floor}.')

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
    reused:   str = ''          # the ID of the existing tree the model chose, or empty for a new tree


class Planner:
    def __init__(
        self,
        backend: Backend,
        registry: Registry,
        essential_devices: set[str] | None = None,
        max_attempts: int = 3,
        on_event: Callable[..., None] | None = None,
        library: dict[str, tuple[str, str]] | None = None
    ):
        self.backend           = backend
        self.registry          = registry
        self.essential_devices = essential_devices or set(DEFAULT_ESSENTIAL_DEVICES)
        self.max_attempts      = max_attempts
        self.on_event          = on_event or (lambda kind, text, **data: None)
        self.library           = library or {}
        self.system_prompt     = build_system_prompt(registry, self.essential_devices, self.library)

    def plan(
        self,
        task: str,
        places: dict | None = None,
        live_skills: set[str] | None = None,
        blind: bool = False,
        floors: dict[str, str] | None = None,
        robot_floor: str | None = None,
    ) -> PlanResult:
        validator = TreeValidator(
            self.registry,
            ValidatorConfig(
                essential_devices=self.essential_devices,
                blind=blind,
                live_skills=live_skills,
                known_keys=None if places is None else set(places)
            )
        )
        messages = [{'role': 'user', 'content': build_task_message(task, places or {}, live_skills, blind, floors, robot_floor)}]

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

            choice = USE_TREE_PATTERN.search(reply.text)
            reused = choice.group(1) if choice else ''

            if reused and reused not in self.library:
                feedback = f'There is no existing tree "{reused}". Choose an ID from the list, or write a new tree.'
                self.on_event('validation', f'unknown existing tree {reused}', level='warn', attempt=attempt)
                messages.append({'role': 'user', 'content': feedback})
                continue

            overrides = dict(ATTRIBUTE_PATTERN.findall(choice.group(2))) if reused else {}
            unknown   = sorted(overrides.keys() - tree_parameters(self.library[reused][1]).keys()) if reused else []

            if unknown:
                feedback = f'The tree "{reused}" has no parameter {", ".join(unknown)}. Use its parameters only, or write a new tree.'
                self.on_event('validation', f'unknown parameters {unknown} of {reused}', level='warn', attempt=attempt)
                messages.append({'role': 'user', 'content': feedback})
                continue

            tree = override_parameters(self.library[reused][1], overrides) if overrides else self.library[reused][1] if reused else extract_tree(reply.text)
            swaps = REPLACE_PATTERN.findall(choice.group(3) or '') if reused else []
            wrong = ''

            for subtree_id, other_id in swaps:
                if subtree_id not in subtree_interfaces(tree, self.registry):
                    wrong = f'The tree "{reused}" has no subtree {subtree_id}.'
                elif other_id not in self.library:
                    wrong = f'There is no existing tree "{other_id}" to put in place of {subtree_id}.'
                else:
                    try:
                        tree = replace_subtree(tree, subtree_id, self.library[other_id][1])
                    except ValueError as error:
                        wrong = f'{other_id} cannot replace {subtree_id}: {error}.'

                if wrong:
                    break

            if wrong:
                self.on_event('validation', wrong, level='warn', attempt=attempt)
                messages.append({'role': 'user', 'content': wrong + ' Fix the answer, or write a new tree.'})
                continue

            if reused:
                self.on_event('reuse', f'existing tree {reused}' + (f' with {overrides}' if overrides else '') + (f' replacing {swaps}' if swaps else ''), attempt=attempt)

            if tree is None:
                feedback = 'Your answer has no <tree></tree> block. Give the full tree inside the tags.'
                self.on_event('validation', 'no tree in the answer', level='warn', attempt=attempt)
                messages.append({'role': 'user', 'content': feedback})
                continue

            tree, filled, mismatches = fill_floor_parameters(tree, floors or {}, set(overrides) if reused else set(tree_parameters(tree)))

            if mismatches:
                feedback = 'Floor parameters that do not match their places: ' + '; '.join(mismatches) + '. Leave the floor parameter out, or give the floor of the place.'
                self.on_event('validation', 'floor parameters do not match: ' + '; '.join(mismatches), level='warn', attempt=attempt)
                messages.append({'role': 'user', 'content': feedback})
                continue

            if filled:
                self.on_event('parameters', f'floor parameters set from their places: {filled}', attempt=attempt)

            self.on_event('tree', f'tree, attempt {attempt}', tree=tree, attempt=attempt, usage=reply.usage)

            issues   = validator.validate(tree)
            errors   = [issue for issue in issues if issue.level == 'error']
            warnings = [f'{issue.path}: {issue.message}' for issue in issues if issue.level == 'warning']

            if not errors:
                self.on_event('valid', f'tree is valid after {attempt} attempt(s)', warnings=warnings)
                return PlanResult(True, tree, attempt, warnings=warnings, reused=reused)

            report = '\n'.join(f'- {issue.path}: {issue.message}' for issue in errors)

            self.on_event('validation', f'{len(errors)} errors:\n{report}', level='warn', attempt=attempt)
            messages.append({
                'role':    'user',
                'content': (f'The existing tree {reused} does not pass the validator now:\n{report}\n'
                            'Write a new tree inside <tree></tree> tags.') if reused else
                           f'The validator rejected the tree:\n{report}\nFix these errors and give the full tree again.',
            })

        message = f'no valid tree after {self.max_attempts} attempts'
        self.on_event('error', message, level='error')

        return PlanResult(False, attempts=self.max_attempts, message=message)
