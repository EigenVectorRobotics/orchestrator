# Run recorder and retrospection (design, not built)

Status: 2026-11-05. Built: `orchestrator/recording.py`, `orchestrator/run_recorder.py` (branch `feat/recorder`). Not built yet: planner attempts, the clock offsets, `retrospect`. Model: HomeBody's `session/recorder.py` and `live_recorder.py` (Stanford-TML/homebody, 2388aa4).

## Goal

- Every run on the robot leaves one folder on the NUC that has everything needed to understand it later: what code and contracts ran, what the planner and the agent saw and decided, what each skill did, and the sensor data around it.
- A **retrospection operator** (a person, a session, or both) reads these folders after the runs. It finds what caused each failure, drafts experience entries, and makes the counts for skill and observation graduation (`docs/knowledge.md`).
- It works with no cloud link (the elevator): it writes only to the NUC's disk.

## What we have now

- `/orchestrator/trace`: JSON events (`started` with the tree XML, `node`, `record`, `agent_start`, `agent_tool`, `agent_say`, `agent_decision`, `finished`, `halted`, `rejected`, `validation`, `error`, and the planner's `sent`).
- `record` events carry the full record of each finished call, with the action feedback over time.
- Agent calls go out on `/orchestrator/agent/request` and come back on `/orchestrator/agent/decision`.
- Press frames go to `capture_dir` (`~/.ros/orchestrator_captures`), and the agent's sandbox files to `agent_scratch_dir`.
- Nothing puts these together, and nothing records which code, contracts and parameters ran.

## Shape

A new node `run_recorder` (package `orchestrator`) on the NUC. It is passive: it only subscribes and writes files. It does not change what the tree does, so a recorder failure cannot stop a run.

```
~/orch_runs/<utc>-<id>/                 one folder per recorder session (one bring-up); small when no task runs
  provenance.json                       written at start
  events.jsonl                          every trace event, append only, one line each
  runs/<n>-<tree_id>/                   one folder per tree run (started to finished/halted/error)
    tree.xml                            the tree that ran
    task.json                           the task text, blind or not, where the tree came from (planner, send_tree)
    records.jsonl                       the full record of each call (feedback included)
    agent/<call_id>.json                each agent call: the request (failure, history, press), every tool call and reply, the decision
    planner/<attempt>.json              each planner attempt: the prompt, the reply, the validator errors
    captures/                           the press frames this run made (copied from capture_dir)
    bag/                                ros2 bag (MCAP) of the chosen topics, for this task only
    outcome.json                        status, abort reason, duration; written last, atomically
  finalization.json                     open | closed | failed, and why
```

### provenance.json

- `git`: for the orchestrator and each stack repo on the NUC (paths from a parameter): revision, branch, and a hash of the tracked files with local edits (as HomeBody's `source_digest`), so a run with uncommitted changes is not mistaken for a commit.
- `contracts`: `name`, `version`, `hash` of each contract that the executor loaded, and the hash of each agent skill document.
- `places`: the path and hash of the places file.
- `parameters`: the executor, planner and agent parameters (`orchestrator.yaml` and the overrides).
- `models`: planner and agent backend, model, base URL. Never the API key.
- `devices`: host name, ROS distro, `ROS_DOMAIN_ID`, and the clock offset between the NUC and the Orin and Thor (press frames use the Orin's stamps).

### events.jsonl

- Every `/orchestrator/trace` event as it comes, with the recorder's receive time added.
- `run_id` and `tree_id` are on every executor event of a tree run (built). Still to add: `skill_version` and `device` on `node` and `record` events, when contracts have a `version`.

### What the executor must give that the trace does not have now

1. **Full records:** already in the trace's `record` events; no new topic.
2. **Agent call bodies.** The external backend already publishes the whole request. The recorder subscribes to the request and decision topics. For a backend inside the executor (`openai_compatible`), the agent emits its messages and tool results on the same request topic shape.
3. **Planner attempts.** `planner_node` emits a `planner_attempt` event with the prompt, the reply and the validator errors of each attempt. Large (the catalog is long), so this goes on its own topic, not in the trace.
4. **Capture paths.** The `record` of a press already names its saved frames. The recorder copies them into the run folder when the run ends.

### bag/

- `ros2 bag record -s mcap` of: `/orchestrator/trace`, `/orchestrator/agent/*`, `/speak`, `/route_goal`, `/route_planner/status`, `/nav_state`, `/state_estimation`, `/odom_base_link`, `/right_ee_pose`, `/left_ee_pose`, `/track_3d/*`, `/slam_elevatorlio/status`, `/terrain_map` (low rate), the camera at 1 fps.
- The camera at full rate is about 17 MB/s raw on the NUC; record it at full rate only around a press (the press frames already cover the closest point).
- **One bag per task, not per session.** A task is one full rollout: from the task (or `send_tree`) to `finished`, `halted` or `error`. The recorder starts the bag when the tree starts and stops it when the tree ends. A bring-up with no task makes no bag, so testing the orchestrator does not fill the disk.

### Writes

- Line-buffered appends for `.jsonl` files; `outcome.json` and `finalization.json` written as `.tmp`, then renamed (as HomeBody).
- A write error marks the session `failed` in `finalization.json` and logs once; it never stops the tree.
- Nothing is deleted automatically. The operator clears old runs by hand when the disk gets full.

### Free space (housekeeping)

- `min_free_gb` (parameter, first guess 20 GB) on the disk of `~/orch_runs`.
- Checked at bring-up and before each task. Below the limit:
  - the recorder logs an error, and publishes `not ready` on `/orchestrator/recorder/status` (with the free space and the largest old runs);
  - the executor refuses a new tree when `require_recorder` is true (default true on the robot, false on the laptop), with the reason in the trace.
- Checked again while a bag records; below `min_free_gb / 4` it stops the bag (the events go on) and marks the run `bag_truncated`.
- `retrospect du` lists the runs by size, oldest first, so the operator can choose what to clear.

## Retrospection

A tool `retrospect` (a CLI first; a session uses it, as `agent_cli`):

- `retrospect list [--since]`: runs with their outcome, duration, failed skills, decisions.
- `retrospect show <run>`: the timeline: each call with its status, inputs, elapsed time, agent decisions, and links to frames.
- `retrospect blame <run>`: for a failed run, the chain from the first failed call to the end, with the evidence (feedback, frames, agent reasons). It proposes the cause; a person agrees or corrects it.
- `retrospect draft-entry <run>`: drafts an experience entry (`E-0xx`, the format of `manipulation_recovery.experience.md`) with links to the run. The operator approves the exact text before it goes in; this keeps the rule of that file.
- **Who approves:** the operator, for now. Later, the agent: see `docs/knowledge.md`, "Approval by the agent".
- `retrospect counts`: success and failure counts per `skill@version`, per decision, per place, for `docs/knowledge.md`.

## Open questions

- Do we copy the bundles to the laptop or the cloud after the test, and with what (rsync)?
