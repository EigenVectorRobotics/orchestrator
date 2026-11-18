from typing import Callable, TypeVar
from concurrent.futures import Future, ThreadPoolExecutor

from rclpy.node import Node
from rclpy.action import ActionClient
from rosidl_runtime_py.utilities import get_message

from skill_registry import Contract, Registry
from skill_registry.contract import Watch

from ..common.config import ARTIFACTS_DIR
from ..common.topics import COMMAND_QOS
from ..sensing import Camera, Tf
from ..vlm.client import VlmClient
from .api import DoneHandle, Handle, Skill
from .loader import SkillLibrary
from .transports import TopicWatch, Transport, default_transports


T = TypeVar('T')


class SkillContext:
    """What every transport and skill of one executor shares: the node, ROS clients made once, the camera, TF, thread
    pools, and shared state that skills hand to each other"""

    def __init__(self, node: Node, camera: Camera, vlm_client: VlmClient | None = None, server_wait_s: float = 3.0,
                 artifacts_dir: str = ARTIFACTS_DIR):
        self.node          = node
        self.camera        = camera
        self.tf            = Tf(node)
        self.vlm_client    = vlm_client or VlmClient()
        self.server_wait_s = server_wait_s
        self.artifacts_dir = artifacts_dir

        self._shared:          dict[str, object]       = {}
        self._action_clients:  dict[str, ActionClient] = {}
        self._service_clients: dict[str, object]       = {}
        self._publishers:      dict[str, object]       = {}
        self._watches:         dict[tuple, TopicWatch] = {}
        self._pools = {'vlm': ThreadPoolExecutor(max_workers=2), 'commands': ThreadPoolExecutor(max_workers=2)}

    @property
    def logger(self):
        return self.node.get_logger()

    def now(self) -> float:
        """ROS time in seconds: the clock of message stamps"""
        return self.node.get_clock().now().nanoseconds * 1e-9

    def shared(self, key: str, factory: Callable[[], T]) -> T:
        """One object per key for the whole executor, made by factory on first use"""
        if key not in self._shared:
            self._shared[key] = factory()

        return self._shared[key]

    def submit(self, pool: str, function, *args) -> Future:
        """Run function in a thread of pool: vlm (questions) or commands (alive checks and stack launches)"""
        return self._pools[pool].submit(function, *args)

    def watch(self, spec: Watch) -> TopicWatch:
        key = (spec.topic, spec.latched)

        if key not in self._watches:
            self._watches[key] = TopicWatch(self.node, spec.topic, spec.ros_type, spec.latched)

        return self._watches[key]

    def action_client(self, name: str, action_type) -> ActionClient:
        if name not in self._action_clients:
            self._action_clients[name] = ActionClient(self.node, action_type, name)

        return self._action_clients[name]

    def service_client(self, name: str, service_type):
        if name not in self._service_clients:
            self._service_clients[name] = self.node.create_client(service_type, name)

        return self._service_clients[name]

    def publisher(self, name: str, ros_type: str):
        if name not in self._publishers:
            self._publishers[name] = self.node.create_publisher(get_message(ros_type), name, COMMAND_QOS)

        return self._publishers[name]

    def shutdown(self):
        for pool in self._pools.values():
            pool.shutdown(wait=False, cancel_futures=True)


CALL_ERRORS = (AttributeError, ValueError, TypeError, KeyError, ModuleNotFoundError)


class SkillRunner:
    """Starts every call of a non-agent skill: the skill's own before, start and wrap around its transport"""

    def __init__(self, ctx: SkillContext, registry: Registry, library: SkillLibrary | None = None,
                 transports: dict[str, Transport] | None = None):
        self.ctx        = ctx
        self.library    = library or SkillLibrary()
        self.transports = transports or default_transports()
        self.skills: dict[str, Skill] = {}

        for contract in registry:
            self.prepare(contract)

    def prepare(self, contract: Contract):
        """Make clients, subscriptions and the skill's own state early, so a tree's first call finds them ready"""
        interface = contract.interface
        transport = self.transports.get(interface.transport)

        try:
            if interface.success_when is not None:
                self.ctx.watch(interface.success_when)

            if transport is not None:
                transport.prepare(contract, self.ctx)

            skill_class = self.library.find(contract.name, 'skill', Skill)

            if skill_class is not None:
                self.skills[contract.name] = skill_class(contract, self.ctx)
                self.skills[contract.name].setup()
        except (*CALL_ERRORS, ImportError) as error:
            self.ctx.logger.warn(f'{contract.name}: {error}; it fails if a tree uses it')

    def start(self, contract: Contract, inputs: dict) -> Handle:
        skill     = self.skills.get(contract.name)
        transport = self.transports.get(contract.interface.transport)

        try:
            if skill is not None and (refused := skill.before(inputs)) is not None:
                return refused

            handle = skill.start(inputs) if skill is not None else None

            if handle is None and transport is None:
                return DoneHandle(False, f'transport {contract.interface.transport} is not supported by this runner')

            handle = handle if handle is not None else transport.start(contract, inputs, self.ctx)

            return skill.wrap(handle, inputs) if skill is not None else handle
        except CALL_ERRORS as error:
            return DoneHandle(False, f'cannot call {contract.name}: {error}')

    def shutdown(self):
        for skill in self.skills.values():
            skill.shutdown()

        self.ctx.shutdown()
