from .base import Transport
from .alive import AliveTransport, CommandTransport
from .action import ActionTransport
from .compare import CompareTransport
from .python import PythonTransport
from .service import ServiceTransport
from .topic import TopicTransport
from .vlm import VlmTransport
from .watch import WatchTransport


def default_transports() -> dict[str, Transport]:
    """Every built-in transport by name; agent and tree are run by their own leaves"""
    transports = [ActionTransport(), ServiceTransport(), TopicTransport(), WatchTransport(), CompareTransport(),
                  AliveTransport(), CommandTransport(), VlmTransport(), PythonTransport()]

    return {transport.name: transport for transport in transports}
