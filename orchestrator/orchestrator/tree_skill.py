"""A tree skill: a BehaviorTree of skills that runs as one skill (docs/skill-packages.md, "A tree skill").

A tree is just a skill. The step gate asks once for the whole tree, and its leaves never ask. The tree is ticked
with the step, on the thread that ticks the step's tree: nothing inside it waits for a person, and a code skill
inside it runs on its own thread. Each leaf is a skill leaf, so the contracts, the timeouts and the recorder see each
part.

The tree reads its inputs as blackboard keys with the port names, and it sees the step's blackboard (the places of
the task). When it ends, each output port takes the blackboard key with its name; nothing else goes back. When it
fails, its message and its outputs name the part that failed.
"""

from skill_registry import Contract, Registry, tree_file

from .behavior_tree import BehaviorTree, Context, Status, TreeError
from .skill_leaf import DoneHandle, SkillLeaf


class TreeRunner:
    """Starts tree skills: it holds the library and the leaf factory that builds each part"""

    def __init__(self, registry: Registry, skills_dir: str):
        self.registry   = registry
        self.skills_dir = skills_dir
        self.build_leaf = None             # set once the leaf factory exists: each part of a tree is a leaf

    def start(self, contract: Contract, inputs: dict, context: Context, chain: str = '', inside: tuple = ()):
        """chain: the path of leaves down to this call; inside: the compositions it runs in, this one last
        (skill_leaf.Transports checks its depth)"""
        path = tree_file(contract.name, self.skills_dir)

        try:
            return TreeHandle(self, contract, path.read_text(), inputs, context, chain or contract.name,
                              inside or (contract.name,))
        except OSError as error:
            return DoneHandle(False, f'no tree file {path}: {error}')
        except (TreeError, ValueError) as error:      # a node builder reads an attribute as a number
            return DoneHandle(False, f'the tree of {contract.name} does not build: {error}')


class TreeHandle:
    """One run of a tree skill: each poll ticks its tree once"""

    def __init__(self, runner: TreeRunner, contract: Contract, xml_text: str, inputs: dict, context: Context, chain: str,
                 inside: tuple):
        self.message = ''
        self.outputs = {}
        self.status  = Status.RUNNING

        self._contract = contract
        self._failed_leaf = None                 # the last leaf that failed in the last tick: what the tree failed on

        def on_event(node, event):
            if isinstance(node, SkillLeaf) and event['status'] == Status.FAILURE.value:
                self._failed_leaf = node

            context.on_event(node, event)

        def build(name, path, attributes, inner):
            leaf        = runner.build_leaf(name, f'{chain}/{path}', attributes, inner)
            leaf.inside = inside                 # its own compositions: a part that runs this tree again is refused
            return leaf

        self._context = context.child({**context.blackboard, **inputs}, on_event)   # its parts go to the recorder too
        self._tree    = BehaviorTree(xml_text, build, self._context)

    def poll(self) -> Status:
        if self.status != Status.RUNNING:
            return self.status

        self._failed_leaf = None
        self.status       = self._tree.tick()

        if self.status == Status.SUCCESS:
            blackboard   = self._context.blackboard
            self.outputs = {port.name: blackboard[port.name] for port in self._contract.outputs if port.name in blackboard}
        elif self.status == Status.FAILURE:
            self._failed()

        return self.status

    def cancel(self):
        self._tree.halt()
        self.status = Status.FAILURE if self.status == Status.RUNNING else self.status

    def _failed(self):
        """The part that failed: the last leaf that failed in the tick that ended the tree, with its message. A part
        that failed in an earlier tick and that a Fallback recovered is not it"""
        part = self._failed_leaf

        if part is None:
            self.message = f'{self._contract.name} failed, and no part of it failed (a control node did, e.g. a Timeout)'
            return

        self.outputs['failed_part'] = {'skill': part.contract.name, 'path': part.path, 'message': part.message,
                                       'inputs': part.inputs}
        self.message = f'{part.contract.name} failed at {part.path}: {part.message or "no message"}'
