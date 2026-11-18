# Mission control

What runs on the operator's computer: the agent bridge that answers every agent call of the robot, and the scripts that run the robot from there. Nothing here is built on the robot.

| File | What it does |
|---|---|
| `agent_bridge.py` | Answers every agent skill's calls: the model (Claude Agent SDK, frames as images) for a skill whose `spec.py` says `ANSWERED_BY = 'model'`, the operator in its terminal for `operator_confirm`. |
| `robot/` | Scripts that start, run, watch, cancel and stop a robot run on the NUC over ssh (`robot/README.md`). |
| `test/test_agent_bridge.py` | Tests of the `operator_confirm` path (with a pipe as stdin), the protocol check, the warm models, the prompt built from a call, and the system prompt and prompts-file hash a call reports. |

Nothing about a skill is written here. Each agent skill is a folder of `g1_skills/g1_skills/` with `spec.py` (its phases, decisions, decision fields and limits) and `prompt.py` (its system prompt and the prompt built from a call); the bridge finds them at start and takes the link's framing from `orchestrator/orchestrator/agent/protocol.py`. All of these use only the standard library, so the bridge runs without ROS. The model is told only what the skill's `prompt.py` gives it, with no tools and no documents; what each call does and how the tree uses the answer is in [docs/agents.md](../docs/agents.md). How a robot run uses the bridge is in [docs/run.md](../docs/run.md#4-run-a-task).

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

It listens on the site's `agent_bridge` address (`orchestrator/config/sites/stanley.yaml`, the laptop's Tailscale address and port 8771), with `claude-opus-5-5` at low effort (`--model`, `--effort`). The executor, launched with the same site, connects there: the bridge prints `executor connected from ...`, and the executor logs `connected to the agent bridge at ...` (or `the agent bridge link dropped: ...`) to `~/orchestrator.log` on the NUC, where `robot/start.sh` and `robot/status.sh` read it. For another laptop, change `agent_bridge.host` in the site file and rebuild on the NUC; `--listen` and `--port` override the site for a test. When it starts a call, the bridge sends the executor a `started` message with the model, the effort, the prompt, the system prompt and its sha256, and the sha256 of the `agent_prompts.py` it loaded; the run recorder on the NUC keeps these per call, with the decisions and the usage, in the run's `agent/<call id>/` ([docs/recorder.md](../docs/recorder.md)). So a record shows which prompts answered it, even when the laptop's checkout has moved on since. With `--keep` the bridge also keeps each call in `~/agent_calls/<call id>/`, with timings in `~/agent_calls/bridge_timing.jsonl`, for debugging on the laptop.

## Keep the bridge on the NUC's branch

Run the bridge from a checkout of the branch the NUC runs, and restart it after a pull. Each call carries:

- `protocol`, `protocol.PROTOCOL` on the NUC. A bridge with another `PROTOCOL` fails the call at once with both numbers ("protocol mismatch: ..."): pull the NUC's branch on the laptop and restart the bridge.
- `phase` and `decisions`, and the fields the skill adds (its `agent.py`): for the press skills `button`, `base_goal` and `offset_in_force` (the offset the last press used); for `operator_confirm`, `question`.
- `limits`, what the executor enforces (the skill's `spec.limits()`). The prompt and the recovery's system prompt state these, so a limit changed on the NUC alone still reaches the model. A call whose limits differ from the bridge's own starts a model without warm-up.

## Answer operator_confirm

An `operator_confirm` call never goes to the model. The bridge rings the bell and prints the question in its window. Type `y` or `n`, then an optional reason, and Enter: `y gantry removed`. The bridge keeps answering the link while it waits. When the call times out (`timeout_s` in `g1_skills/g1_skills/operator_confirm/contract.yaml`, 120 s) or is cancelled, the bridge drops the question.

`COLCON_IGNORE` keeps colcon from building this folder on the robot.
