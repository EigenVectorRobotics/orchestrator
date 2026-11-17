# Mission control

What runs on the operator's computer: the agent bridge that answers every agent call of the robot. Nothing here is built on the robot.

| File | What it does |
|---|---|
| `agent_bridge.py` | Answers the executor's `press_judge` and `manipulation_recovery` calls through the Claude Agent SDK, with the frames as images. Asks the operator in its terminal for `operator_confirm`. |
| `agent_prompts.py` | The bridge's decision rules (`JUDGE_SYSTEM`, `RECOVERY_SYSTEM`) and how each call's evidence becomes the prompt. |
| `test/test_agent_bridge.py` | Tests of the `operator_confirm` path, with a pipe as stdin. |

## Set up once

The bridge needs Python 3.10 or newer, a Claude Code login on this computer, and Tailscale to the robot's NUC. It reads the site file from this repository, so run it from a checkout of the branch the NUC runs.

```bash
python3 -m venv ~/.venvs/agent_bridge
~/.venvs/agent_bridge/bin/pip install -r mission_control/requirements.txt
```

## Run the bridge

```bash
~/.venvs/agent_bridge/bin/python mission_control/agent_bridge.py --site stanley
```

It listens on the site's `agent_bridge` address (`orchestrator/config/sites/stanley.yaml`, the laptop's Tailscale address and port 8771), with `claude-opus-5-5` at low effort (`--model`, `--effort`). The executor, launched with the same site, connects there. For another laptop, change `agent_bridge.host` in the site file and rebuild on the NUC; `--listen` and `--port` override the site for a test. The record of each call is kept on the NUC by the run recorder (model, prompt, decisions, usage). With `--keep` the bridge also keeps each call in `~/agent_calls/<call id>/`, with timings in `~/agent_calls/bridge_timing.jsonl`, for debugging on the laptop.

## Answer operator_confirm

An `operator_confirm` call never goes to the model. The bridge rings the bell and prints the question in its window. Type `y` or `n`, then an optional reason, and Enter: `y gantry removed`. The bridge keeps answering the link while it waits. When the call times out (120 s) or is cancelled, the bridge drops the question.

`COLCON_IGNORE` keeps colcon from building this folder on the robot.
