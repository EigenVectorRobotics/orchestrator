from skill_registry import Registry

from ..agent.leaf import AgentLeaf, AgentRunner
from .engine import Context, TreeError, TreeNode
from .leaf import SkillLeaf, SkillRunner


def make_leaf_factory(registry: Registry, runner: SkillRunner, agent: AgentRunner | None = None):
    """Build each skill leaf of a tree: agent skills run through the agent, every other skill through the runner"""
    def build_leaf(skill_name: str, path: str, attributes: dict, context: Context) -> TreeNode:
        contract = registry.get(skill_name)

        if contract is None:
            raise TreeError(f'{path}: unknown skill {skill_name}')

        if contract.interface.transport == 'agent':
            return AgentLeaf(contract, agent, path, attributes, context)

        return SkillLeaf(contract, runner, path, attributes, context)

    return build_leaf
