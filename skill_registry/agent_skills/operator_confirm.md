---
name: operator_confirm
description: Ask the operator a yes/no question from the tree and decide from their answer only.
runs_when: [the tree needs a person to confirm something, e.g. that the gantry is removed]
runs_on: a Claude Code session through agent_cli (default)
decisions: [confirmed, not_confirmed]
---

# Operator confirm

The tree asks a person a yes/no question (the `question` input, also in `agent_cli show`) and waits up to 120 s. You are the relay: you do not judge the answer from sensors.

1. Ask the operator the question in the session, word for word.
2. Wait for their reply. Do not answer for them, and do not guess from images or topics.
3. They say yes: `agent_cli decide confirmed --reason "<their words>" --send`. They say no: `agent_cli decide not_confirmed --reason "<their words>" --send`.
4. No reply: do nothing. The call times out after 120 s and the tree says why it stopped.

`decide --send` is the only thing you publish.
