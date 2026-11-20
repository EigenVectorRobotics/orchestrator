# Run recorder and retrospection (design, partly built)

Status: 2026-11-20. Built: `orchestrator/recording.py`, `orchestrator/run_recorder.py`, the agent's words in each run, and `retrospect list`, `show` and `blame` (`orchestrator/retrospect.py`). Not built yet: the clock offsets, `counts` and `du`. Model: HomeBody's `session/recorder.py` and `live_recorder.py` (Stanford-TML/homebody, 2388aa4).

## Goal

- Every run on the robot leaves one folder on the NUC that has everything needed to understand it later: what code and contracts ran, what the agent saw and decided, what each skill did, and the sensor data around it.
- A **retrospection operator** (a person, a session, or both) reads these folders after the runs. It finds what caused each failure, writes claims as knowledge notes, and makes the counts for skill and observation graduation (`docs/knowledge.md`).
- It works with no cloud link (the elevator): it writes only to the NUC's disk.

## What we have now

- `/orchestrator/trace`: JSON events (`task_started` and `task_finished` around each task, `started` with the step's tree XML and the step number, `node`, `record`, `plan`, `finished`, `halted`, `rejected`, `validation`, `error`).
- `record` events carry the full record of each finished call, with the action feedback over time.
- The task desk's commands and replies go on `/orchestrator/task/*`, and the step gate's on `/orchestrator/step_*`.
- The agent's notes go to `agent_scratch_dir/<run_id>/` (default `~/orch_knowledge/scratch`).
- Nothing puts these together, and nothing records which code, contracts and parameters ran.

## Shape

A new node `run_recorder` (package `orchestrator`) on the NUC. It is passive: it only subscribes and writes files. It does not change what a step does, so a recorder failure cannot stop a run.

```
~/orch_runs/<utc>-<id>/                 one folder per recorder session (one bring-up); small when no task runs
  provenance.json                       written at start
  events.jsonl                          every trace event, append only, one line each
  runs/<n>-<name>/                      one folder per task (task_started to task_finished)
    task.json                           the task text, blind or not, the run_id
    steps/<k>-<tree_id>.xml             the tree of each step
    steps.jsonl                         one line per step: tree, start, end (finished, halted, error), status
    records.jsonl                       the full record of each call (feedback included)
    plans.jsonl                         each version of the task plan
    words.jsonl                         what the agent thought and said (reasoning summaries, messages), from its Codex session logs; the recorder reads them every 2 s, and until 5 s after the end
    scratch/                            the agent's notes and checks of this run (moved from agent_scratch_dir/<run_id> at the end of the run)
    bag/                                ros2 bag (MCAP) of the chosen topics, for this task only
    outcome.json                        how the task ended (done, gave_up, halted, cancelled, error), the step count; written last, but words.jsonl can grow for about 7 s more
  finalization.json                     open | closed | failed, and why
```

### provenance.json

- `git`: for the orchestrator and each stack repo on the NUC (paths from a parameter): revision, branch, and a hash of the tracked files with local edits (as HomeBody's `source_digest`), so a run with uncommitted changes is not mistaken for a commit.
- `contracts`: `name`, `version`, `hash` of each contract that the executor loaded, and the hash of each code skill.
- `places`: the path and hash of the places file.
- `parameters`: the executor's parameters (`orchestrator.yaml` and the overrides).
- `models`: the task agent and its model. Never the API key.
- `devices`: host name, ROS distro, `ROS_DOMAIN_ID`, and the clock offset between the NUC and the Orin and Thor (camera frames use the Orin's stamps).

### events.jsonl

- Every `/orchestrator/trace` event as it comes, with the recorder's receive time added.
- `run_id` and `tree_id` are on every executor event of a task (built). Still to add: `skill_version` and `device` on `node` and `record` events, when contracts have a `version`.

### What the executor must give that the trace does not have now

1. **Full records:** already in the trace's `record` events; no new topic.
2. **The task desk's messages.** The bag records the task and step-gate topics.

### bag/

- `ros2 bag record -s mcap` of the orchestrator's own topics (`/orchestrator/trace`, `/orchestrator/task/*`, `/orchestrator/step_*`; `BAG_TOPICS` in `run_recorder.py`) and the robot stack's topics in the parameter `stack_topics` (`config/orchestrator.yaml`). Not yet: `/terrain_map` (low rate), the camera at 1 fps.
- The camera at full rate is about 17 MB/s raw on the NUC, so it is not in the bag.
- **One bag per task, not per session.** A task is one full rollout: from `task_started` to `task_finished`. The recorder starts the bag when the task starts and stops it when the task ends. A bring-up with no task makes no bag, so testing the orchestrator does not fill the disk.

### Writes

- Line-buffered appends for `.jsonl` files; `outcome.json` and `finalization.json` written as `.tmp`, then renamed (as HomeBody).
- A write error marks the session `failed` in `finalization.json` and logs once; it never stops the tree.
- Nothing is deleted automatically. The operator clears old runs by hand when the disk gets full.

### Free space (housekeeping)

- `min_free_gb` (parameter, first guess 20 GB) on the disk of `~/orch_runs`.
- Checked at bring-up and before each task. Below the limit:
  - the recorder logs an error, and publishes `not ready` on `/orchestrator/recorder/status` (with the free space and the largest old runs);
  - the executor refuses a new task when `require_recorder` is true (default true on the robot, false on the laptop), with the reason in the trace.
- Checked again while a bag records; below `min_free_gb / 4` it stops the bag (the events go on) and marks the run `bag_truncated`.
- `retrospect du` lists the runs by size, oldest first, so the operator can choose what to clear.

## Retrospection

A tool `retrospect` (a CLI first; a session uses it, as `agent_cli`):

- `retrospect list [--since]`: runs with their outcome, step count, failed steps and the task (built).
- `retrospect show <run>` (a run_id or a key, built): it shows the plans. Then it shows each step with its calls and the agent's grade of the step. A call has its status, inputs, time and outputs, and a failure has its last feedback. Then come the refusals, what the run left, and its folder. Not yet: links to frames.
- `retrospect blame <run>` (built): the evidence of a failure, in time order. It starts at the step before the first failed step (the decision that led there) and goes to the end. It gives each step, its calls and the agent's words from `words.jsonl`. Of the feedback of a call, it gives the state at each change and the last sample. The state is the sample with no fractions and no time fields. It does not say a cause: the agent finds the cause and grades the steps. Not yet: frames (the bag has no camera, and the clock offsets are not known).
- No `draft-entry`: after `blame`, the agent writes what it found with `knowledge note "TEXT" --by agent:task --run <run_id> --evidence step:<n> --about skill=<name>`. The claim then links to the run. It goes in the store at once, as an observation.
- **Who approves a fact:** the operator (`knowledge approve`), for now. Later, the agent: see `docs/knowledge.md`, "Approval by the agent".
- `retrospect counts`: success and failure counts per `skill@version`, per decision, per place, for `docs/knowledge.md`.

## Open questions

- Do we copy the bundles to the laptop or the cloud after the test, and with what (rsync)?
