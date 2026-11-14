# Mission control

What runs on the operator's computer: the agent bridge that answers every agent call of the robot. Nothing here is built on the robot.

| File | What it does |
|---|---|
| `agent_bridge.py` | Answers the executor's `press_judge` and `manipulation_recovery` calls through the Claude Agent SDK, with the frames as images. Asks the operator in its terminal for `operator_confirm`. |
| `agent_prompts.py` | The bridge's decision rules (`JUDGE_SYSTEM`, `RECOVERY_SYSTEM`) and how each call's evidence becomes the prompt. |
| `test/test_agent_bridge.py` | Tests of the `operator_confirm` path, with a pipe as stdin. |

## Set up once

The bridge needs Python 3.10 or newer, a Claude Code login on this computer, and Tailscale to the robot's NUC.

```bash
python3 -m venv ~/.venvs/agent_bridge
~/.venvs/agent_bridge/bin/pip install -r mission_control/requirements.txt
```

## Run the bridge

```bash
~/.venvs/agent_bridge/bin/python mission_control/agent_bridge.py
```

It listens on the laptop's Tailscale address, 100.107.208.25:8771 (`--listen`, `--port`), with `claude-opus-5-5` at low effort (`--model`, `--effort`). The orchestrator's default backend connects to it; for another laptop, set `agent_push_host` to its address. Each call is kept in `~/agent_calls/<call id>/`, with timings in `~/agent_calls/bridge_timing.jsonl`.

## Answer operator_confirm

An `operator_confirm` call never goes to the model. The bridge rings the bell and prints the question in its window. Type `y` or `n`, then an optional reason, and Enter: `y gantry removed`. The bridge keeps answering the link while it waits. When the call times out (120 s) or is cancelled, the bridge drops the question.

`COLCON_IGNORE` keeps colcon from building this folder on the robot.
