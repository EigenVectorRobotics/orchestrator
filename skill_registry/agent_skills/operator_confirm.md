---
name: operator_confirm
description: Ask the operator a yes/no question from the tree and decide from their answer only.
runs_when: [the tree needs a person to confirm something, e.g. that the gantry is removed]
runs_on: the agent bridge on the laptop (mission_control/agent_bridge.py), in its terminal; never the model
decisions: [confirmed, not_confirmed]
---

# Operator confirm

The tree asks a person a yes/no question (the `question` input) and waits up to 120 s. No model answers it: the bridge is the relay, and nothing is judged from sensors.

1. The bridge rings the bell and prints the question in its window, word for word.
2. The operator types `y` or `n`, then an optional reason, and Enter. Any other line asks again.
3. `y` sends `confirmed`, and `n` sends `not_confirmed`, with the reason `Operator: <their words>`.
4. No reply: the call times out after 120 s, the bridge drops the question, and the tree says why it stopped.
