from std_msgs.msg import String

from orchestrator.common.topics import LATCHED_QOS
from orchestrator.skills.api import DoneHandle, Handle, Skill


class SetElevator(Skill):
    def setup(self):
        """One latched publisher on the contract's topic"""
        self.publisher = self.ctx.node.create_publisher(String, self.contract.interface.name, LATCHED_QOS)

    def start(self, inputs: dict) -> Handle:
        """Publish the elevator name; done at once"""
        self.publisher.publish(String(data=inputs['elevator']))

        return DoneHandle(True, f'elevator {inputs["elevator"]} on {self.contract.interface.name}')
