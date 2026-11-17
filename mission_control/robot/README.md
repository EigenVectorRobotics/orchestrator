# Robot scripts

Run from the laptop; each one acts on the NUC over ssh (`NUC=vector@vector-nuc` by default, `NUC=vector@192.168.123.5` on the robot wire; `SITE=stanley` by default). They are the safe, usual path for a robot run. A session operating the robot keeps every other command too (`docs/operations.md`); these scripts only make the usual steps one command each, with the checks built in.

| Script | Does | Checks |
|---|---|---|
| `start.sh` | Starts the orchestrator in tmux session `orchestrator` (`site:=$SITE`), waits until the planner and the recorder are up | Refuses when the session exists; says whether the agent bridge is connected. Never builds |
| `run_task.sh target_floor_1` | `planner_cli run --use-tree freight_elevator_go_to_place --param goal={target_floor_1}` | Refuses when the executor is down, or a tree runs (a new task halts it); `--force` sends it anyway |
| `status.sh` | The executor's state, the tmux sessions, the bridge link, the last log lines | |
| `cancel.sh` | Halts the running tree | |
| `stop.sh` | Cancel, `/stop` 1, autonomy off, release the arms, Idle, then Ctrl-C the orchestrator so the raw bag closes | Leaves the robot stacks up (`deploy.sh stop nuc` etc. for those) |

Start the agent bridge yourself, in your own terminal (`../README.md`): started in the background, it cannot answer `operator_confirm`.
