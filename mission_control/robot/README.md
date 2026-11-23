# Robot scripts

Run from the laptop; each one acts on the NUC over ssh (`NUC=vector@vector-nuc` by default, `NUC=vector@192.168.123.5` on the robot wire; `SITE=stanley` by default). They are the safe, usual path for a robot run. The manual commands they wrap, and the steps no script does (deploy, the bridge, a single tree, bringing a machine up), are in [docs/run.md](../../docs/run.md); these scripts only make the usual steps one command each, with the checks built in.

| Script | Does | Checks |
|---|---|---|
| `start.sh` | Empties `~/orchestrator.log`, starts the orchestrator in tmux session `orchestrator` (`site:=$SITE`), waits up to 60 s until the planner and the recorder are up ([run.md, section 3](../../docs/run.md#3-start-the-orchestrator)) | Refuses when the session exists; says whether the agent bridge is connected, from the executor's link line in `~/orchestrator.log`. Never builds, never starts the robot stacks (preflight does) |
| `run_task.sh elevator1_target_floor_1` | `planner_cli run --use-tree freight_elevator_go_to_place --param elevator=elevator1 --param goal={elevator1_target_floor_1}` (the elevator is the first word of the key): sends the task, waits through preflight, answers the planner ([run.md, section 4](../../docs/run.md#4-run-a-task)) | Refuses when the executor is down, or a tree runs (a new task halts it); `--force` sends it anyway |
| `status.sh` | The executor's state (from `/orchestrator/status`, or `down`), the tmux sessions on the NUC, the last bridge link line of `~/orchestrator.log` (connected or dropped), the last 15 log lines ([run.md, section 8](../../docs/run.md#8-watch-a-run)) | |
| `cancel.sh` | Halts the running tree; the orchestrator stays up | |
| `stop.sh` | Cancel, `/stop` 1, autonomy off, release the arms, Idle, then Ctrl-C the orchestrator so the raw bag closes ([run.md, section 7](../../docs/run.md#7-stop)) | Leaves the robot stacks up (`deploy.sh stop nuc` etc. for those) |

`common.sh` holds what they share: the ssh target, the site, and the executor's state.

Start the agent bridge yourself, in your own terminal ([../README.md](../README.md)): started in the background, it cannot answer `operator_confirm`.
