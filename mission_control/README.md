# Mission control

What runs on the operator's computer: the agent bridge that answers the robot's agent calls, and the tools a person or a Claude Code session uses to answer them from the laptop. Nothing here is built on the robot.

| File | What it does |
|---|---|
| `agent_bridge.py` | Answers the executor's `press_judge` and `manipulation_recovery` calls (`agent_backend:=push`) through the Claude Agent SDK, with the frames as images. |
| `agent_prompts.py` | The bridge's decision rules (`JUDGE_SYSTEM`, `RECOVERY_SYSTEM`) and how each call's evidence becomes the prompt. |
| `agent_session.py` | Runs `agent_cli` on the robot over ssh, copies the frames, and sends decisions (`agent_backend:=external`). |

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

It listens on the laptop's Tailscale address, 100.107.208.25:8771 (`--listen`, `--port`), with `claude-opus-5-5` at low effort (`--model`, `--effort`). Launch the orchestrator on the NUC with `agent_backend:=push`; for another laptop, set `agent_push_host` to its address. Each call is kept in `~/agent_calls/<call id>/`, with timings in `~/agent_calls/bridge_timing.jsonl`.

`COLCON_IGNORE` keeps colcon from building this folder on the robot.
