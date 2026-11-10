# Task mode: the agent works step by step (design, not built)

Status: draft for review, 2026-11-05. Related: `docs/skill-packages.md` ("Compositions"), `docs/recorder.md`, `docs/knowledge.md`.

## Goal

- For a new task, the agent works step by step: it calls a skill, looks at the result, and selects the next step, until the task is done or it cannot be done.
- One task is one run: one bundle, one bag, one scratch folder, one blackboard.
- A behavior tree is not the normal output. Trees come later, from steps that worked (`docs/skill-packages.md`, "Compositions"), and the agent calls a graduated tree as one skill.
- A step is always one skill. A small tree comes only from graduation (decided 2026-11-05).

## Now

- `planner_node` gets a task, the LLM writes a full tree, the validator checks it, and the tree goes to the executor on `/orchestrator/tree`.
- The executor runs one tree at a time. Each tree gets a new `Context` (blackboard, history, `run_id`, scratch folder).
- The recorder opens a run on `started` and closes it on `finished`, `halted` or `error`: one run per tree.
- The planner's LLM backends (`llm_backends.py`) have no tools. The agent's backends (`agent_backends.py`: external, openai_compatible, scripted) run a tool loop.

## Proposal

### The task agent runs in the executor

- A new agent skill, `task`, runs on the agent's tool loop and backends. It is the planner in task mode.
- It runs in the executor, so one task has one `Context`: the blackboard and the history go from one step to the next (for example, a pose from one skill is an input of the next).
- Its tools:
  - `run_skill(name, inputs)`: the executor builds a one-leaf tree, validates it (the same validator: contracts, live skills, the `rt/*` ban, maturity on the robot; no fallback is needed, because the agent recovers), ticks it, and returns the result: status, outputs, the failure and its feedback. A graduated tree is a skill, so it runs the same way. Inputs and outputs that go by the blackboard get the key `step<n>_<port>`, so a later step can use `{step1_detections}`. An abort ends only that step. Built in `orchestrator/tasks.py` (2026-11-05); `/orchestrator/status` is `waiting` between steps.
  - the read tools of the agent now: `read_file`, `ros_read`, `get_image`, `run_python`, `note`, `say`;
  - `done(summary)` and `give_up(reason)`: they end the task.
- The facts and beliefs and the run's scratch folder go into its prompt, as for the agent now.
- **The backend is a choice, per run**: Claude, Astra, Kimi, a local model, and others. The task agent uses only the neutral tool loop, so a new provider is one backend class. Now there are `external` (a Claude Code session), `openai_compatible` (Kimi and most local servers) and `scripted`; a Claude API backend is still to be built.
- **Two modes** (launch argument `mode`, decided 2026-11-05):
  - `task` (the default): the executor takes the task sentence on `/orchestrator/task` and starts the task agent; `planner_node` does not start.
  - `tree`: as before. `planner_node` writes one full tree for the sentence, and the executor runs it. `send_tree` works in both modes.
- **Plan first.** The agent first writes a task plan: a short list of subgoals (for example: walk to the call panel, call the elevator, walk in, press floor 1, ride, walk out). A tool `plan(subgoals)` saves it; it goes in the trace, the run bundle and `plan.md` in the scratch folder. Each `run_skill` names its subgoal. When a step fails or the situation changes, the agent calls `plan` again; the recorder keeps each version. The plan is text, not a tree: the subgoals with the steps that worked for them are the material for compositions later.

### One task is one run

- The executor emits `task_started` (with `run_id` and the task text) and `task_finished` (`done`, `gave_up`, `cancelled`, `error`).
- Each `run_skill` emits `started` and `finished` as now, with a step number.
- The recorder opens the run on `task_started` and closes it on `task_finished`. A tree sent alone (`send_tree`) is a task of one step, so the recorder handles both modes the same way.
- `runs/<n>-<task>/` gets `steps.jsonl` (one line per step: skill, inputs, result) in place of one `tree.xml`.

### Limits

- `max_steps` (for example 40) and `max_task_s` (for example 600 s) per task; at the limit, the task ends as `gave_up`.
- `/orchestrator/cancel` halts the running step and ends the task.
- A skill that the robot may not use (`experimental`) is refused by the validator in `run_skill`, as in a tree.

### Step gate on the robot

- The goal is a robot that tries its own skills. On the real robot, a person approves each step at first (decided 2026-11-05). In the sim, there is no gate.
- The executor sends the proposed step (skill, inputs, subgoal) latched on `/orchestrator/step_request` and waits. The operator approves or refuses it from a terminal (`agent_cli step wait`, then `agent_cli step approve|refuse --reason R`). A refusal goes back to the agent as the step's result (`status: refused`), with the operator's reason. A step that waits when the task ends is refused.
- Built (`StepGate` in `task_agent.py`). Launch argument `step_gate`: `auto` (the default) is on unless `fake:=true`. `agent_cli` answers only at a tty, so the agent session cannot approve its own step by mistake. This is a guard against mistakes only: the executor takes a decision from anyone who publishes on `/orchestrator/step_decision` (`ros2 topic pub`, or `script` for a tty). The real limit is the session's permissions (`docs/skill-discovery.md`, "The session too"): shell commands only through `agent_cli`, without `step approve|refuse`.
- **Mission Control is the second client** (`mission_control_node.py`, built 2026-11-10): a person clicks Approve or Refuse in a browser, with no tty check, because a browser has no terminal. Every decision carries the client's name (`by`: `agent_cli`, `mission_control`, `the executor`), which the gate puts in the trace, so the record says who answered.
- The adapter's port is on the NUC, where the agent's session also has a shell, so **a command needs the node's token** (built 2026-11-10). The node makes a token at start and logs the link that holds it (`http://127.0.0.1:8080/?t=...`); the page keeps it for the tab and sends it with every command. A command without it is refused, and so is one that is not `application/json` from this host. A read (the state, the camera frame) needs no token. `token:=off` takes the check away, for a bench with no agent on it.
- **Inside a code skill** the gate asks once for the code skill, and then for every call it makes that can act on the robot (decided 2026-11-10). A read never asks. `skill_registry/gate.yaml` holds the exception: the few acting skills the operator marked quiet, such as `say`. It asks by default, because on this branch the agent writes the contracts, so nobody can list the interfaces in advance. That file is the operator's, like `deny.yaml`. The request is a normal step request with `in_code` set.
- The recorder keeps each request and answer. Later, as for facts, the gate can get narrower: first for skills at `tested`, then more, when the operator's answers show that it is safe.

### Loss of the cloud

A segment is not blind by design. The cloud can drop at any time, and the robot must handle it. Some places are known to drop it (the cabin), so the agent can anticipate it.

- **Handle, for now (decided 2026-11-05):** when a call to the agent's backend fails, the running step goes to its end (a skill needs no cloud), and the executor starts no new step. The robot says what happened and asks for help on `/speak` (for example "I lost my connection in the elevator. Please press floor 1 for me."). It then waits for the cloud, and the agent continues from the recorded state when the cloud is back.
- **A local model, to be tested:** Cosmos Reason 2 8B can run on Thor. It is not known whether it can do the task agent's reasoning. An experiment decides it: run the same recorded cases with the cloud model and with Cosmos Reason on Thor, and compare the decisions.
- **Anticipate:** that a place has no cloud is a claim in the knowledge store (for example `{place: freight_elevator_cabin}`, key `cloud`, value `false`). Before the agent goes there, it sees the claim in its prompt and can prepare.
- **Retrospect:** after a run, the recorder's bundle shows what happened when the cloud dropped. From many runs, the agent learns what the robot must do without it.
- **Build the routine:** anticipation and retrospection give, in time, the right routine for each place without the cloud. It is one of two kinds, and both graduate as skills do:
  1. a **fallback skill**: what to do when the cloud drops in a step (for example stop safely and wait for the doors);
  2. a **whole routine as a BT**: a tree that covers the part without the cloud and runs to its end without the agent.
- Until such a routine graduates, a drop is handled as in "Handle, for now".
- The executor's blind mode (`/orchestrator/tree_blind`) stays for a tree that the agent sends when it expects no cloud.

## Recovery agents

- `manipulation_recovery` and `press_judge` stay as skills. The task agent can call them, or do the recovery itself, because it sees each failure.
- The experiment (`docs/skill-packages.md`) will show which one works better.

## Similar work: HomeBody

Stanford-TML/homebody (2388aa4), the task session (`session/session.py`):

- **Step by step, as here:** one skill per turn, one `decide` tool call, the result goes into the history (the last 24 steps in the prompt). No plan or tree. The `done` skill ends the task.
- **No step gate:** no person approves a step (Codex runs with `approval_policy="never"`). The operator has pause, stop and end. A person confirms only a handover (an object still in a hand).
- **Ask for help:** `ask_operator` (off by default) and `request_help` exist only in exploration, not in the task session. `request_help` holds the robot and ends exploration as incomplete.
- **Loss of the model:** the model is called only between skills, so a running skill goes to its end. While the model thinks, a heartbeat commands base velocity 0. A call times out at 180 s. Busy errors (429, 5xx) get 3 retries at 20, 40 and 60 s; a network drop is not retried and ends the task (`provider_error`). There is no local model.
- **Limits:** `max_steps` = 80; no time limit for a task (exploration: 420 s). Cancel is checked at each step, during the model wait and in the skills. After a pause, a late model reply is discarded.
- **Recording:** one session folder per process, `tasks/task-NNN/` per task, with each step's prompt, request and reply.

What we take: the model only between steps; a stop command to the base while the agent thinks; a call timeout; retries for busy errors only; a late reply after a cancel is discarded; each step's prompt and reply in the bundle.

Where we differ: a step gate on the robot; on a network drop, the robot asks for help on `/speak` and waits, and does not end the task; a time limit for each task.

## Open questions

- Can Cosmos Reason 2 8B on Thor do the task agent's reasoning, for all of it or only for some decisions? (The experiment in "Loss of the cloud".)
- What text does the robot say when the cloud drops: fixed text per place, or text the agent wrote in advance when it anticipated the drop?
