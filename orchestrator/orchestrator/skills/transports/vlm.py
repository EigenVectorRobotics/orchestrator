from skill_registry import Contract

from ...tree.engine import Status
from ..api import DoneHandle, Handle
from ..context import SkillContext
from ...vlm.client import VlmClientError
from .base import Transport


class VlmHandle(Handle):
    def __init__(self, future, question: str, success_answer: str):
        super().__init__()

        self.future         = future
        self.question       = question
        self.success_answer = success_answer

    def poll(self) -> Status:
        if self.status != Status.RUNNING or not self.future.done():
            return self.status

        try:
            answer = self.future.result()
        except VlmClientError as error:
            self._finish(False, str(error))
            return self.status

        self.outputs = {'answer': answer}
        self._finish(not self.success_answer or answer == self.success_answer, f'{self.question} -> {answer}')

        return self.status


class VlmTransport(Transport):
    """A closed question about the latest camera frame, to the VLM server"""

    name = 'vlm'

    def start(self, contract: Contract, inputs: dict, ctx: SkillContext) -> Handle:
        interface = contract.interface

        if ctx.camera.jpeg is None:
            return DoneHandle(False, f'no camera image on {ctx.camera.topic}')

        choices  = interface.choices or [choice.strip() for choice in inputs.get('choices', '').split(';') if choice.strip()]
        question = interface.question.format(**inputs)

        if not choices:
            return DoneHandle(False, 'a VLM question needs choices')

        future = ctx.submit('vlm', ctx.vlm_client.ask, ctx.camera.jpeg, question, choices)

        return VlmHandle(future, question, interface.success_answer)
