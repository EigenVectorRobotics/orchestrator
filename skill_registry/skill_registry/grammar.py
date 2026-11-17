"""The contract grammar as text, for the agent that writes skills (write_skill, docs/skill-discovery.md).

The fields and the allowed values come from the parser itself (contract.py), so what the agent is told and what
write_skill takes cannot drift apart. The rules are prose, and a refusal says the same thing in the same words: a
new rule in parse_contract needs its line here too. The task desk carries this text in the open message, as it
carries the runbook, so a session with agent_cli and the task loop in the executor both read it. Everything about
what write_skill takes is here, the code skills too; task_loop.SYSTEM says only how to decide.
"""

from .contract import NAME_RULE, READ_ONLY, ROS_TRANSPORTS, Contract, HeldValue, Interface, Port, Watch, field_text, one_of


EXAMPLE = '''name: example_skill          # the shape only; this interface does not exist
kind: action
device: nuc
description: One sentence on what the skill does
interface:
  transport: service
  name: /example/do_thing
  ros_type: example_interfaces/srv/DoThing
inputs:
- {name: target, type: string, description: what to act on}
- {name: tries, type: int, required: false, default: 1}
outputs:
- {name: result, type: string, description: a top-level field of the response}
timeout_s: 5'''


def contract_grammar() -> str:
    """What write_skill takes: the fields of a contract, the allowed values, the rules and one example"""
    return f'''A skill contract is a YAML mapping. * marks a field that every contract gives.
contract: {field_text(Contract)}
interface: {field_text(Interface)}
a port, an item of inputs or outputs: {field_text(Port)}
success_when: {field_text(Watch)}
failure_when: {field_text(HeldValue)}
Rules: {NAME_RULE}. An action or code skill needs a timeout_s above 0.
A condition only reads ({one_of(READ_ONLY)}). An {one_of(ROS_TRANSPORTS)} skill needs the ROS name and the
ros_type it takes. A watch needs success_when. An agent skill names its skill document. A compare skill needs
the inputs value and equals. A vlm skill needs a question; a {{field}} in a question or a compare_question must be
an input; success_answer must be one of choices; frames: press_capture judges the frames a press kept. failure_when
reads action feedback, so it needs an action skill; capture kind is press. An enum port needs choices, and one
value per choice if it gives values. A port fills the message field of its own
name, or the one in field; an output is read from a top-level field of the result, or, on a watch, from any
field of the message it reads; a pose output comes back as {{x, y, z, yaw, frame}}. Whatever maturity you give, your
skill loads untested.
A code skill is a skill whose body is Python: the contract has interface: {{transport: code}}, and the Python goes
in code, as `def run(skills, inputs)`. It calls skills.run("<skill>", input=value), which gives back a result with
.ok, .message and .outputs, and it returns a dict of its own outputs. Use it for a policy that needs a loop, a
branch or arithmetic over several skills. It runs in a sandbox: no ROS, no network, no files of its own. The robot
moves only through the skills it calls, and a person is asked again before any of those that move it.
Example:
{EXAMPLE}'''
