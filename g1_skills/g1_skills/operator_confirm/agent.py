from orchestrator.agent.api import AgentCall, AgentSkill


class OperatorConfirm(AgentSkill):
    """A yes/no question for the operator, answered in the agent bridge's terminal"""

    def task_lines(self, call: AgentCall) -> list[str]:
        return [f'Question: {call.inputs.get("question", "")}',
                'The operator answers in the agent bridge: confirmed when they say yes, not_confirmed when they say no. '
                'Without a reply the call times out.']

    def request_fields(self, call: AgentCall, evidence: dict) -> dict:
        return {'question': call.inputs.get('question', '')}
