# Run recorder and retrospection (recorder built, retrospection not built)

Built: `orchestrator/orchestrator/recording.py` (no ROS: the folders and files), `orchestrator/orchestrator/run_recorder.py` (the node `orchestrator_recorder`, started by the launch unless `record:=false`). Not built yet: planner attempts, the `parameters`, `models` and `devices` parts of `provenance.json`, `skill_version` and `device` on trace events, `retrospect`. Model: HomeBody's `session/recorder.py` and `live_recorder.py` (Stanford-TML/homebody, 2388aa4).

## Goal

- Every run on the robot leaves one folder on the NUC that has everything needed to understand it later: what code and contracts ran, what the planner and the agent saw and decided, what each skill did, and the sensor data around it.
- A **retrospection operator** (a person, a session, or both) reads these folders after the runs. It finds what caused each failure and makes the counts for skill maturity.
- It works with no cloud link (the elevator): it writes only to the NUC's disk.

## What the recorder reads

- `/orchestrator/trace`: JSON events. The executor's: `started` (with the tree XML), `node`, `record`, `agent_start`, `agent_wait`, `agent_rule`, `agent_refused` (a decision the executor refused), `agent_camera`, `agent_reference`, `agent_say`, `agent_decision`, `agent_link`, `finished`, `halted`, `rejected`, `validation`, `preflight_skipped`, `error`. The planner's: `sent` and `preflight`. `run_id` and `tree_id` are on every executor event of a tree run.
- `record` events carry the full record of each finished call, with the action feedback over time.
- `/orchestrator/task`: the task message the planner got, kept as text for the next run's `task.json`.
- The agent topics. Each agent call goes out on `/orchestrator/agent/request` (latched: opened, then closed with its outcome); the executor's replies to the bridge's decisions go on `/orchestrator/agent/reply`; what the bridge reports (`started`, each `decision` with its usage, `failed`) and a `cancelled` go on `/orchestrator/agent/event`.
- Press frames, and the camera image a recovery call sends (`call_<id>_camera.jpg`), are written to `capture_dir` (`~/.ros/orchestrator_captures`). The recorder moves them into the run.
- The recorder is the only writer of `~/orch_runs`. The executor writes no call records of its own; the bridge keeps a copy on the laptop only with `--keep`.
- Not the recorder: the site's `raw_bag` (`orchestrator/config/sites/stanley.yaml`, lidar, IMU, SLAM and TF topics for replay) is recorded by the launch to `~/bags/` from launch to shutdown.

## Shape

The node `run_recorder` (package `orchestrator`, node name `orchestrator_recorder`) on the NUC. It is passive: it only subscribes and writes files. It does not change what the tree does, so a recorder failure cannot stop a run.

```
~/orch_runs/<utc>-<id>/                 one folder per recorder session (one bring-up), e.g. 20261117T221105Z-3f2c9a; small when no task runs
  provenance.json                       written at start
  events.jsonl                          every trace event, append only, one line each, with the recorder's receive time
  runs/<nnn>-<tree_id>/                 one folder per tree run (started to finished, halted or error), e.g. 001-preflight
    tree.xml                            the tree that ran
    task.json                           run_id, tree_id, blind, start time, and the planner's last task text with its time (null for send_tree)
    records.jsonl                       the full record of each call (feedback included)
    agent/<call_id>/request.json        the call as the bridge got it: protocol, skill, phase, button, question, base goal,
                                        allowed decisions, limits, failure, history, offset in force, press (judge, diagnose),
                                        stance (diagnose, stance_check), its images by file, and the task as text; written when
                                        the call opens, replaced when it closes, with its outcome and error
    agent/<call_id>/messages.jsonl      everything in order: opened, started (model, effort, prompt, system prompt,
                                        system_sha256, prompts_sha256, when the bridge got it), each decision (the
                                        answer, accepted or not, the executor's reply, cost, tokens, model time), each
                                        reply, failed, cancelled, closed (outcome, error)
    agent/<call_id>/summary.json        one record, rewritten after each message: model, effort, prompt, system_sha256,
                                        prompts_sha256, final decision, answers, refused, state (thinking, answered,
                                        failed), error, start, seconds, when the bridge got it, cost and tokens in all
    captures/                           the press frames this run named, and the camera images its recovery calls sent
                                        (moved from capture_dir when the run ends: copied, then the originals removed)
    bag/                                ros2 bag (MCAP) of BAG_TOPICS, for this tree run only
    bag_truncated                       only when the bag was stopped for space: the free space and the time
    outcome.json                        how it ended (finished, halted, error, closed), status, aborted, text, end time; written last, atomically
  finalization.json                     open | closed | failed, the first write error, and the time
```

- `<nnn>` counts the tree runs of the session from `001`. Preflight is a run of its own (`001-preflight`); the task text the planner sent goes into the `task.json` of the tree after it.
- A call is filed in the run it opened in. Messages that come after that run ended still go to its folder. A call outside any run is not recorded.
- Not written: `planner/<attempt>.json` (planner attempts, not built), and a duration (take it from `task.json` `started` and `outcome.json` `finished`).

### What an agent call record does and does not hold

- The prompt in `started` and `summary.json` is the user message the bridge built from the request (`mission_control/agent_prompts.py`, `judge_prompt` or `recovery_prompt`).
- `started` in `messages.jsonl` also holds the system prompt the model had (`system`, from `system_prompt` in `agent_prompts.py`, with the call's limits), its sha256 (`system_sha256`) and the sha256 of the `agent_prompts.py` the bridge loaded (`prompts_sha256`). `summary.json` keeps the two sha256s, not the text.
- The images the model got are named in `request.json` `images` and copied to `captures/`.
- The bridge's code is not in `provenance.json`: it runs on the laptop. The links to the bridge that answered are the request's `protocol` (`orchestrator/orchestrator/agent_spec.py` `PROTOCOL`) and, for a model call, `prompts_sha256`.
- An `operator_confirm` call has model `operator`; its prompt is the question.

### provenance.json

Built:

- `schema_version` (1) and `created_utc`.
- `git`: for each git checkout under `repo_roots` (default `/home/vector/orchestrator_ws/src` and `/home/vector/eigen_vector_ws/src`): path, revision, branch, `dirty`, and `edits`, a hash of the tracked diff and the untracked files (as HomeBody's `source_digest`), so a run with uncommitted changes is not mistaken for a commit. A copied folder such as `bringup` is not a checkout and is not listed.
- `contracts`: file name → hash of each `*.yaml` in `contracts_directory`. No contract `version`: contracts have none.
- `places`: the path and hash of the places file.
- `press_references`: the path, line count and hash of the press references the agent calls read (`~/orch_state/press_references.jsonl`, or the old `~/.ros/orchestrator_press_references.jsonl` while only it exists).

Not built:

- `parameters`: the executor, planner and agent parameters (`orchestrator.yaml`, the site file and the overrides).
- `models`: planner and agent backend, model, base URL. Never the API key. The agent's model is chosen on the laptop (`agent_bridge.py --model`); the record has it per call, in `started`.
- `devices`: host name, ROS distro, `ROS_DOMAIN_ID`, and the clock offset between the NUC and the Orin and Thor (press frames use the Orin's stamps).

### events.jsonl

- Every `/orchestrator/trace` event as it comes, with the recorder's receive time added (`received`).
- `run_id` and `tree_id` are on every executor event of a tree run (built). Still to add: `skill_version` and `device` on `node` and `record` events, when contracts have a `version`.

### What the executor gives

1. **Full records:** in the trace's `record` events (built).
2. **Agent calls:** the executor publishes the whole request and the bridge's reports; the recorder subscribes to the request, reply and event topics (built).
3. **Planner attempts** (not built): `planner_node` would emit a `planner_attempt` event with the prompt, the reply and the validator errors of each attempt. Large (the catalog is long), so on its own topic, not in the trace.
4. **Capture paths:** the `record` of a press and the request of an agent call name their saved frames. The recorder moves them into the run folder when the run ends (built).

### bag/

- `ros2 bag record -s mcap` of the topics in `BAG_TOPICS` (`orchestrator/orchestrator/run_recorder.py`, the `bag_topics` parameter's default): the trace, the agent request, reply and event topics, `/rosout`, `/speak`, `/route_goal`, `/route_planner/status`, `/nav_state`, `/state_estimation`, `/odom_base_link`, `/track_3d/is_tracking`, `/track_3d/selected_target_3d`, `/slam_elevatorlio/status`, and the raw and filtered button poses (`/track_3d/selected_normal_pose`, `/track_3d/selected_normal_pose_filtered/arm_upright`). `/terrain_map`, the route and local paths, TF and the camera are not recorded.
- The camera at full rate is about 17 MB/s raw on the NUC; record it at full rate only around a press (the press frames already cover the closest point).
- **One bag per tree run, not per session.** The recorder starts the bag at the executor's `started` and stops it at `finished`, `halted` or `error` (SIGINT, then SIGKILL after 10 s). Preflight is a tree run too, so it has its own bag. A bring-up with no tree makes no bag. The recorder's parameter `bag` (default true; not a launch argument) turns the bags off.

### Writes

- Line-buffered appends for `.jsonl` files; every `.json` file (`outcome.json`, `finalization.json`, `request.json`, `summary.json`) written as `.tmp`, then renamed (as HomeBody).
- The first write error marks the session `failed` in `finalization.json` and the status not ready (logged once, when the status changes); it never stops the tree that runs, but the executor refuses the next one while `require_recorder` is true.
- Nothing in `~/orch_runs` is deleted automatically (only the originals in `capture_dir`, once copied). The operator clears old runs by hand when the disk gets full.

### Free space (housekeeping)

- `min_free_gb` (parameter, 20 GB) on the disk of `~/orch_runs`.
- Checked at start and every 5 s. The recorder publishes `/orchestrator/recorder/status` (latched JSON: `ready`, `reason`, `free_gb`, `session`, and when not ready the 5 largest runs) each time the state changes. Not ready: below the limit, or after a write failed.
  - The recorder logs an error.
  - The executor refuses a new tree while the status is not ready or no recorder publishes it, when `require_recorder` is true (the launch sets it from `record`, default true), with the reason in the trace.
- While a bag records, below `min_free_gb / 4` the recorder stops the bag (the events go on) and writes `bag_truncated` in the run folder.
- `retrospect du` (not built) would list the runs by size, oldest first, so the operator can choose what to clear.

## Retrospection (not built)

A tool `retrospect` (a CLI first, for a person or a session):

- `retrospect list [--since]`: runs with their outcome, duration, failed skills, decisions.
- `retrospect show <run>`: the timeline: each call with its status, inputs, elapsed time, agent decisions, and links to frames.
- `retrospect blame <run>`: for a failed run, the chain from the first failed call to the end, with the evidence (feedback, frames, agent reasons). It proposes the cause; a person agrees or corrects it.
- `retrospect counts`: success and failure counts per `skill@version`, per decision, per place, for skill maturity.
- **Who approves** a proposed cause: the operator.

## Open questions

- Do we copy the bundles to the laptop or the cloud after the test, and with what (rsync)?
