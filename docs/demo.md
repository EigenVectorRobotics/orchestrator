# Elevator demo checklist

The G1 rides the freight elevator from one floor to a place on the other floor; one operator runs it from a laptop on Tailscale. Each step links to the full procedure in [run.md](run.md).

1. **Before the demo:** robot powered in the gantry; NUC, Orin and Thor on; NUC code built; laptop on Tailscale with the bridge set up; Foxglove on `ws://vector-nuc:8765`; Run Viewer at `http://vector-nuc:8780`. [Before you start](run.md#1-before-you-start)
2. **Start the bridge** in your own terminal and keep it in view: `~/.venvs/agent_bridge/bin/python mission_control/agent_bridge.py --site stanley`. [Run a task, step 1](run.md#4-run-a-task)
3. **Start the orchestrator:** `mission_control/robot/start.sh`; wait for `ready` and `agent bridge: connected`. [Start the orchestrator](run.md#3-start-the-orchestrator)
4. **Stand the robot** in ready locomotion, remove the gantry, put it near the call panel on its floor. [Run a task, step 2](run.md#4-run-a-task)
5. **Send the task:** `mission_control/robot/run_task.sh target_floor_1` (floor 2 to 1), `target_floor_2` for the way back. [Run a task, step 3](run.md#4-run-a-task)
6. **Preflight:** keep the robot still while the stacks start; on "Please relocalize me", send `/initialpose` from Foxglove within 2 minutes; on "not in ready locomotion", stand it up and send the task again. [Run a task, step 4](run.md#4-run-a-task)
7. **The ride:** watch the Run Viewer; if the robot asks you to press a button, press it; to stop, `mission_control/robot/cancel.sh`. [Run a task, step 6](run.md#4-run-a-task)
8. **Stop:** `mission_control/robot/stop.sh`, then Ctrl-C the bridge. [Stop](run.md#7-stop)
9. **Results:** `~/orch_runs/<session>/runs/` on the NUC (each agent call in its run's `agent/<call id>/`), the raw bag in `~/bags/`. [Run a task, step 7](run.md#4-run-a-task) and [recorder.md](recorder.md)

If something fails: [troubleshooting.md](troubleshooting.md).
