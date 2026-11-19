# Task mode: the agent works step by step

Status: built, 2026-11-05 to 2026-11-18. Related: `docs/skill-packages.md` ("Compositions"), `docs/recorder.md`, `docs/knowledge.md`.

## Goal

- For a new task, the agent works step by step: it calls a skill, looks at the result, and selects the next step, until the task is done or it cannot be done.
- One task is one run: one bundle, one bag, one scratch folder, one blackboard.
- A step is always one skill (decided 2026-11-05). A composition of skills is a skill too: a code skill now, a graduated tree later (`docs/skill-packages.md`, "Compositions").

## How it works

### The task desk

- The executor takes the task sentence on `/orchestrator/task` and opens a task (`TaskDesk` in `task_desk.py`). The open message carries the sentence, the places, the runbook, the contract grammar and the knowledge store's guide.
- One task has one `Context`, so the blackboard and the history go from one step to the next. For example, a pose from one skill is an input of the next.
- The agent's commands:
  - `run(skill, inputs, subgoal)`: the executor builds a one-leaf tree and validates it (contracts, live skills, the deny list, maturity on the robot). No fallback is needed, because the agent recovers. The executor ticks the tree and returns the result: status, outputs, the failure and its feedback. Inputs and outputs that go by the blackboard get the key `step<n>_<port>`, so a later step can use `{step1_detections}`. `/orchestrator/status` is `waiting` between steps.
  - `plan(subgoals)`, `write_skill(contract, code)`, `done(reason)` and `give_up(reason)`.
- **Two task agents** (launch argument `task_agent`): `session` (the default), a Claude Code or Codex session outside the executor that uses `agent_cli`; `codex`, a loop in the executor (`codex_agent.py`) that asks `codex exec` for each decision.
- **Plan first.** The agent first writes a task plan: a short list of subgoals. `plan` saves it; it goes in the trace and the run bundle. Each `run` names its subgoal. When a step fails or the situation changes, the agent plans again; the recorder keeps each version.

### One task is one run

- The executor emits `task_started` (with `run_id` and the task text) and `task_finished` (`done`, `gave_up`, `cancelled`, `halted`, `error`).
- Each step emits `started` and `finished`, with a step number.
- The recorder opens the run on `task_started` and closes it on `task_finished`. `runs/<n>-<task>/` gets `steps.jsonl`, one line per step.

### Limits

- `max_steps` (for example 40) and `max_task_s` (for example 600 s) per task; at the limit, the task ends as `gave_up`.
- `/orchestrator/cancel` halts the running step and ends the task.
- A skill that the robot may not use is refused by the validator in `run`.

### Step gate on the robot

- The goal is a robot that tries its own skills. On the real robot, a person approves each step at first (decided 2026-11-05). In the sim, there is no gate.
- The executor sends the proposed step (skill, inputs, subgoal) latched on `/orchestrator/step_request` and waits. The operator approves or refuses it from a terminal (`agent_cli step wait`, then `agent_cli step approve|refuse --reason R`). A refusal goes back to the agent as the step's result (`status: refused`), with the operator's reason. A step that waits when the task ends is refused.
- Built (`StepGate` in `step_gate.py`). Launch argument `step_gate`: `auto` (the default) is on unless `fake:=true`. `agent_cli` answers only at a tty, so the agent session cannot approve its own step by mistake. This is a guard against mistakes only: the executor takes a decision from anyone who publishes on `/orchestrator/step_decision` (`ros2 topic pub`, or `script` for a tty). The real limit is the session's permissions (`docs/skill-discovery.md`, "The session too"): shell commands only through `agent_cli`, without `step approve|refuse`.
- **Mission Control is the second client** (`mission_control_node.py`, built 2026-11-10): a person clicks Approve or Refuse in a browser, with no tty check, because a browser has no terminal. Every decision carries the client's name (`by`: `agent_cli`, `mission_control`, `the executor`), which the gate puts in the trace, so the record says who answered.
- The adapter's port is on the NUC, where the agent's session also has a shell, so **a command needs the node's token** (built 2026-11-10). The node makes a token at start and logs the link that holds it (`http://127.0.0.1:8080/?t=...`); the page keeps it for the tab and sends it with every command. A command without it is refused, and so is one that is not `application/json` from this host. A read (the state, the camera frame) needs no token. `mission_control_token:=off` takes the check away, for a bench with no agent on it.
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

## Recovery agents

- The task agent recovers by itself, because it sees each failure. Whether a separate recovery skill does better is an experiment (`docs/skill-packages.md`).

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
