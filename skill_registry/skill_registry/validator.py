import xml.etree.ElementTree as ElementTree

from dataclasses import dataclass, field

from .contract import DEFAULT_ESSENTIAL_DEVICES
from .registry import Registry


# The BehaviorTree.CPP v4 built-in nodes that orchestrator.behavior_tree runs
CONTROL_NODES = {
    'Sequence', 'SequenceWithMemory', 'ReactiveSequence',
    'Fallback', 'ReactiveFallback',
    'Parallel', 'IfThenElse',
}
DECORATOR_NODES = {
    'Inverter', 'ForceSuccess', 'ForceFailure', 'Repeat', 'RetryUntilSuccessful',
    'KeepRunningUntilFailure', 'Timeout', 'Delay', 'RunOnce',
}
BUILTIN_LEAVES = {'AlwaysSuccess', 'AlwaysFailure', 'SetBlackboard', 'Sleep'}
INTEGER_ATTRIBUTES = {'num_attempts', 'num_cycles', 'msec', 'delay_msec', 'success_count', 'failure_count'}
REQUIRED_ATTRIBUTES = {
    'RetryUntilSuccessful': ['num_attempts'],
    'Repeat':               ['num_cycles'],
    'Timeout':              ['msec'],
    'Delay':                ['delay_msec'],
    'Sleep':                ['msec'],
    'SetBlackboard':        ['value', 'output_key'],
}
FALLBACK_NODES = {'Fallback', 'ReactiveFallback'}
GENERIC_LEAVES = {'Action', 'Condition'}             # <Action ID="skill"/> form
RESERVED_PORTS = {'name', 'ID', '_description', '_skipIf', '_successIf', '_failureIf', '_while', '_onSuccess', '_onFailure', '_onHalted', '_post'}


@dataclass
class Issue:
    level:   str                 # 'error' or 'warning'
    path:    str                 # tree ID and node path, e.g. ride/Sequence[0]/press_button
    message: str
    code:    str = ''            # machine-readable kind, e.g. 'maturity'; consumers filter on this, not on message


@dataclass
class ValidatorConfig:
    essential_devices: set[str] = field(default_factory=lambda: set(DEFAULT_ESSENTIAL_DEVICES))
    blind:             bool = False         # true: the tree must run with no cloud link
    live_skills:       set[str] | None = None   # None: do not check liveness


@dataclass
class _Frame:
    element: ElementTree.Element
    path:    str
    parents: list['_Frame']


class TreeValidator:
    """Check a BehaviorTree.CPP v4 XML tree against the skill registry before it runs"""

    def __init__(self, registry: Registry, config: ValidatorConfig | None = None):
        self.registry = registry
        self.config   = config or ValidatorConfig()

    def validate(self, xml_text: str) -> list[Issue]:
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError as error:
            return [Issue('error', '', f'XML does not parse: {error}')]

        if root.tag != 'root' or root.get('BTCPP_format') != '4':
            return [Issue('error', '', 'root must be <root BTCPP_format="4">')]

        self._trees = {tree.get('ID'): tree for tree in root.findall('BehaviorTree')}

        if not self._trees:
            return [Issue('error', '', 'no <BehaviorTree> found')]

        for tree_id, tree in self._trees.items():
            if len(tree) != 1:
                return [Issue('error', tree_id, 'a BehaviorTree needs exactly one root node')]

        # Walk from the main tree and expand each SubTree where it is called, so a subtree's skills
        # are judged with their real ancestors (a Fallback around the call protects them).
        main_id     = root.get('main_tree_to_execute') or next(iter(self._trees))
        self._used  = {main_id}
        main_root   = self._trees[main_id][0]
        issues      = self._walk(_Frame(main_root, f'{main_id}/{main_root.tag}', []))

        for tree_id in self._trees.keys() - self._used:
            issues.append(Issue('warning', tree_id, f'tree {tree_id} is never called', 'unused'))

        return issues

    def is_valid(self, xml_text: str) -> bool:
        return not any(issue.level == 'error' for issue in self.validate(xml_text))

    # ========== Walk ==========

    def _walk(self, frame: _Frame) -> list[Issue]:
        element = frame.element
        tag     = element.tag
        issues  = []

        for attribute in REQUIRED_ATTRIBUTES.get(tag, []):
            if attribute not in element.attrib:
                issues.append(Issue('error', frame.path, f'{tag} needs attribute {attribute}'))

        for attribute in INTEGER_ATTRIBUTES & element.attrib.keys():
            if not element.attrib[attribute].lstrip('-').isdigit():
                issues.append(Issue('error', frame.path, f'{tag} {attribute} must be an integer, not {element.attrib[attribute]}'))

        if tag in CONTROL_NODES or tag in DECORATOR_NODES:
            if tag == 'IfThenElse' and len(element) not in (2, 3):
                issues.append(Issue('error', frame.path, 'IfThenElse needs 2 or 3 children'))

            if tag in DECORATOR_NODES and len(element) != 1:
                issues.append(Issue('error', frame.path, f'{tag} needs exactly one child'))

            if tag == 'Parallel':
                issues += self._check_parallel(frame)

            for index, child in enumerate(element):
                child_frame = _Frame(child, f'{frame.path}[{index}]/{child.tag}', [*frame.parents, frame])
                issues     += self._walk(child_frame)

            return issues

        if tag in BUILTIN_LEAVES:
            return issues

        if len(element):
            issues.append(Issue('error', frame.path, f'skill {tag} cannot have children'))

        if tag == 'SubTree':
            tree_id = element.get('ID')

            if tree_id not in self._trees:
                return [Issue('error', frame.path, f'unknown SubTree {tree_id}')]

            if any(parent.element.tag == 'SubTree' and parent.element.get('ID') == tree_id for parent in frame.parents):
                return [Issue('error', frame.path, f'SubTree {tree_id} calls itself')]

            self._used.add(tree_id)
            subtree_root = self._trees[tree_id][0]

            return self._walk(_Frame(subtree_root, f'{frame.path}:{tree_id}/{subtree_root.tag}', [*frame.parents, frame]))

        skill_name = element.get('ID') if tag in GENERIC_LEAVES else tag

        return self._check_skill(frame, skill_name)

    def _check_skill(self, frame: _Frame, skill_name: str) -> list[Issue]:
        contract = self.registry.get(skill_name)

        if contract is None:
            return [Issue('error', frame.path, f'unknown skill {skill_name}')]

        issues     = []
        attributes = {key: value for key, value in frame.element.attrib.items() if key not in RESERVED_PORTS}

        for port_name in contract.required_inputs:
            if port_name not in attributes:
                issues.append(Issue('error', frame.path, f'{skill_name}: missing input {port_name}'))

        known_ports = {port.name for port in [*contract.inputs, *contract.outputs]}

        for port_name, value in attributes.items():
            if port_name not in known_ports:
                issues.append(Issue('error', frame.path, f'{skill_name}: unknown port {port_name}'))
                continue

            port        = contract.input(port_name)
            is_variable = value.startswith('{') and value.endswith('}')   # blackboard entry, checked at run time

            if port and port.type == 'enum' and not is_variable and value not in port.choices:
                issues.append(Issue('error', frame.path, f'{skill_name}: {port_name}={value} is not one of {port.choices}'))

        if self.config.blind and contract.needs_cloud:
            issues.append(Issue('error', frame.path, f'{skill_name} needs the cloud, but this tree must run blind'))

        if contract.device not in self.config.essential_devices and not self._is_protected(frame):
            issues.append(Issue('error', frame.path, f'{skill_name} runs on {contract.device}, which can drop out; put it in a Fallback with a recovery after it'))

        if self.config.live_skills is not None and skill_name not in self.config.live_skills:
            issues.append(Issue('error', frame.path, f'{skill_name} is not live now'))

        if contract.maturity != 'tested':
            issues.append(Issue('warning', frame.path, f'{skill_name} is {contract.maturity}', 'maturity'))

        return issues

    # ========== Rules ==========

    def _is_protected(self, frame: _Frame) -> bool:
        """True if some Fallback ancestor has a recovery child after the branch that holds this node"""
        chain = [*frame.parents, frame]

        for depth, ancestor in enumerate(chain[:-1]):
            if ancestor.element.tag not in FALLBACK_NODES:
                continue

            branch   = chain[depth + 1].element
            siblings = list(ancestor.element)

            if siblings.index(branch) < len(siblings) - 1:
                return True

        return False

    def _check_parallel(self, frame: _Frame) -> list[Issue]:
        owners: dict[str, int] = {}
        issues = []

        for index, child in enumerate(frame.element):
            for resource in self._resources(child, set()):
                if resource in owners and owners[resource] != index:
                    issues.append(Issue('error', frame.path, f'resource {resource} is used by branch {owners[resource]} and branch {index} at the same time'))

                owners.setdefault(resource, index)

        return issues

    def _resources(self, element: ElementTree.Element, visited_trees: set[str]) -> set[str]:
        if element.tag == 'SubTree':
            tree_id = element.get('ID')

            if tree_id in visited_trees or tree_id not in self._trees:
                return set()

            return set().union(*(self._resources(child, visited_trees | {tree_id}) for child in self._trees[tree_id]))

        skill_name = element.get('ID') if element.tag in GENERIC_LEAVES else element.tag
        contract   = self.registry.get(skill_name)
        resources  = set(contract.resources) if contract else set()

        for child in element:
            resources |= self._resources(child, visited_trees)

        return resources
