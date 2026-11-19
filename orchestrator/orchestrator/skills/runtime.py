from typing import Callable, TypeVar
from dataclasses import dataclass, field
from concurrent.futures import Future, ThreadPoolExecutor

from rclpy.node import Node
from rclpy.action import ActionClient
from rosidl_runtime_py.utilities import get_message

from skill_registry import Contract, Registry
from skill_registry.contract import Watch

from ..agent.api import Agent, AgentBackend
from ..common.config import ARTIFACTS_DIR
from ..common.topics import COMMAND_QOS
from ..sensing import Camera, Tf
from ..vlm.client import VlmClient, VlmClientConfig
from .api import DoneHandle, Handle, Skill
from .loader import SkillLibrary
from .transports import Driver, TopicWatch, default_drivers


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


@dataclass
class SkillRunnerConfig:
    """The skills' settings: the camera, the VLM, and where skills save files for the agent and the recorder"""

    image_topic:       str = '/camera/camera/color/image_raw'
    camera_info_topic: str = '/camera/camera/color/camera_info'
    vlm:               VlmClientConfig = field(default_factory=lambda: VlmClientConfig(timeout_s=40.0))   # Cosmos: one request at a time
    artifacts_dir:     str = ARTIFACTS_DIR

    @classmethod
    def from_node(cls, node: Node) -> 'SkillRunnerConfig':
        """Declare the skills' parameters on node and read them"""
        default = cls()
        value   = lambda name, fallback: node.declare_parameter(name, fallback).value

        return cls(
            image_topic=value('image_topic', default.image_topic),
            camera_info_topic=value('camera_info_topic', default.camera_info_topic),
            vlm=VlmClientConfig(
                base_url=value('vlm_base_url', default.vlm.base_url),
                model=value('vlm_model', default.vlm.model),
                timeout_s=value('vlm_timeout_s', default.vlm.timeout_s)
            ),
            artifacts_dir=value('capture_dir', default.artifacts_dir)
        )


class SkillRunner:
    """Owns what skills share (the context, each transport's driver, each Skill and the Agent) and starts every call of
    a non-agent skill: the skill's own before, start and wrap around its transport's driver. Agent skills run through
    agent, which is None without a backend"""

    def __init__(self, node: Node, registry: Registry, config: SkillRunnerConfig | None = None,
                 library: SkillLibrary | None = None, agent_backend: AgentBackend | None = None,
                 on_agent_event: Callable[..., None] | None = None, speak: Callable[[str], None] | None = None,
                 drivers: dict[str, Driver] | None = None):
        config       = config or SkillRunnerConfig()
        camera       = Camera(node, config.image_topic, config.camera_info_topic)
        self.ctx     = SkillContext(node, camera, VlmClient(config.vlm), artifacts_dir=config.artifacts_dir)
        self.library = library or SkillLibrary()
        self.drivers = drivers or default_drivers()
        self.skills: dict[str, Skill] = {}
        self.agent   = None if agent_backend is None else Agent(agent_backend, self.ctx, self.library, on_agent_event, speak)

        for contract in registry:
            self.prepare(contract)

    def prepare(self, contract: Contract):
        """Make clients, subscriptions and the skill's own state early, so a tree's first call finds them ready"""
        interface = contract.interface
        driver    = self.drivers.get(interface.transport)

        try:
            if interface.success_when is not None:
                self.ctx.watch(interface.success_when)

            if driver is not None:
                driver.prepare(contract, self.ctx)

            skill_class = self.library.find(contract.name, 'skill', Skill)

            if skill_class is not None:
                self.skills[contract.name] = skill_class(contract, self.ctx)
                self.skills[contract.name].setup()
        except (*CALL_ERRORS, ImportError) as error:
            self.ctx.logger.warn(f'{contract.name}: {error}; it fails if a tree uses it')

    def start(self, contract: Contract, inputs: dict) -> Handle:
        skill     = self.skills.get(contract.name)
        driver    = self.drivers.get(contract.interface.transport)

        try:
            if skill is not None and (refused := skill.before(inputs)) is not None:
                return refused

            handle = skill.start(inputs) if skill is not None else None

            if handle is None and driver is None:
                return DoneHandle(False, f'transport {contract.interface.transport} has no driver in this runner')

            handle = handle if handle is not None else driver.start(contract, inputs, self.ctx)

            return skill.wrap(handle, inputs) if skill is not None else handle
        except CALL_ERRORS as error:
            return DoneHandle(False, f'cannot call {contract.name}: {error}')

    def shutdown(self):
        for skill in self.skills.values():
            skill.shutdown()

        if self.agent is not None:
            self.agent.shutdown()

        self.ctx.shutdown()
