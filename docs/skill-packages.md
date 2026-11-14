# Device skill packages and skill announcement (design, not built)

Status: draft for review, 2026-11-02. Nothing in this note is built yet.

## Goal

- Each device (NUC, Orin, Thor) has its own skill package. The agent writes new skills into the package of the device that runs them.
- When a skill comes online, its device announces it on one shared topic. The registry on the NUC makes the live skill list from these announcements.
- The planner, the validator and the executor do not change: they still read `/skills/live` and the contracts in the registry.

## Packages

| Package | Device | Holds (examples) |
|---|---|---|
| `nuc_skills` | NUC | manipulation (`press_button`, `move_arm_to`), navigation |
| `orin_skills` | Orin | locomotion (`ready_walk`, `release_arms`, `set_profile`) |
| `thor_skills` | Thor | VLM questions (`door_open`, `ask_vlm`), `ground` |

- Each skill is one contract YAML (the schema of `skill_registry/contract.py`) and, when needed, adapter code.
- `device` in the contract must be the device of the package. The host refuses a contract for another device.
- Each package has one **skill host** node. It loads the package, starts the adapters, and announces the skills.
- The central `skill_registry/contracts/` stays as the fallback copy until each skill moves to its device package. Move them one at a time.

## Announcement

Transport: a **volatile** topic with repeats. It is not latched. The repeats are also the heartbeat.

| Topic | Type | QoS | Who publishes | When |
|---|---|---|---|---|
| `/skills/announce` | `std_msgs/String` (YAML) | reliable, volatile, depth 10 | each skill host | every 1 s, and at once when a skill is added, changed or removed, and on a request |
| `/skills/request` | `std_msgs/Empty` | reliable, volatile, depth 1 | the registry | one time at startup |
| `/skills/live` | `std_msgs/String` (JSON list) | reliable, transient-local, depth 1 | the registry | as now |

Why not latched:
- A latched message stays after its device dies, so a heartbeat is necessary anyway. The heartbeat already gives the list.
- DDS keeps history per publisher. With many skills on one topic, messages can be lost with no error.
- A transient-local subscriber does not connect to a volatile publisher, and this gives no error.
- Durability and liveliness work a little differently in Fast DDS (sim) and CycloneDDS (robot).

The startup request removes the wait for a late joiner (for example, a registry that restarts).

### Message: one per device, with the full contracts

```yaml
device: thor
host: thor_skills            # node name
boot_id: 3f2c9a              # new value each time the host starts
sequence: 1042               # +1 for each message from this boot
skills:                      # the full contracts of all skills that are up now
  - name: door_open
    kind: condition
    device: thor
    interface: {transport: vlm, ...}
    version: 1.2.0
    hash: 9be1c0
    maturity: tested
    ...
  - ...
```

- One message has **all** skills of the device. A skill that is not in the message is not live.
- The full contract is in the message, because the NUC cannot read files on Thor and the validator needs ports, resources and device.

### Registry rules

- **Live:** a skill is live if the last message from its device is less than `max_age_s` = 3 s old and has the skill.
- **Device down:** no message for 3 s means that all skills of that device go off `/skills/live`. Log it one time.
- **New boot** (`boot_id` changes): replace all contracts of that device with the new list.
- **Same name on two devices:** refuse the second one and log an error. One skill name has one device.
- **Contract changes in the same boot:** use the new contract, and log the change. (Now `Registry.announce` keeps the first contract and ignores changes.)
- **Bad contract:** refuse that skill only, and log it. The other skills in the message stay live.
- Contracts in the central fallback directory that are not announced follow the current graph check.

## Agent-written skills

### Where the agent runs and how a skill gets to its device

- The agent runs on the operator's laptop (`mission_control/agent_bridge.py`), over Tailscale. The NUC, Thor and Orin are on the same switch.
- A tool `sync_to_device(device, package)` copies the package to the device (rsync over SSH, keys set with `ssh-copy-id`) and tells the host there to load it. The agent does not get a raw shell on the other devices through this tool.
- The host loads new skills while it runs (no restart). It loads the contract, starts the adapter, and announces the skill in its next message.

### What the agent can use

- Any ROS interface that the stack exposes: topics, services and actions. It can use a ROS interface that is not a skill yet (for example a controller's command topic), so it is not limited to the current skill contracts.
- **Never** the Unitree low-level layer: no DDS `rt/*` topics (`rt/lowcmd`, `rt/lowstate`, ...) and no Unitree SDK calls (`unitree_sdk2`, `unitree_sdk2py`). These go around the stack's safety layers (`unitree_interface` modes, joint limits, the arm planner).
- Enforced in two places, not only in the prompt:
  - The contract check refuses an interface name that starts with `rt/`, and any transport other than ROS action, service, topic, watch or VLM.
  - `sync_to_device` refuses a package whose adapter code imports `unitree_sdk2py`, links `unitree_sdk2`, or makes a raw DDS participant (`cyclonedds`, `fastdds` APIs).

### Versions

- Each contract has `version` (semver) and the host adds `hash` (hash of the contract and adapter files).
- A change to the contract or the adapter is a new version. The use counts start again for the new version.
- The trace records `name@version` for each skill call, so a failure is linked to the exact version.

### Graduation

A skill goes up in maturity as it is used more and is reliable.

| Maturity | Planner | Real robot | How it gets here |
|---|---|---|---|
| `experimental` | sim only | no | new skill or new version |
| `wip` | yes, shown after `tested` skills | yes, in a tree with a fallback | sim: n ≥ 5 and reliability ≥ 0.6, then a human approves the first robot use |
| `tested` | yes, shown first | yes | robot: n ≥ 10 and reliability ≥ 0.8 |
| `deprecated` | hidden | no | n ≥ 10 and reliability ≤ 0.2, or a human sets it |

- **Reliability** is the Wilson lower bound of the success rate, so a skill does not go up after a small number of lucky successes. The numbers in the table are a first guess for review.
- **A tested skill goes down** to `wip` when its reliability over the last 10 uses is less than the limit.
- **Sim and robot counts are separate.** Success in the sim does not make a skill `tested`.
- **Success is not the skill's own report.** A use counts as a success only if a check skill after it (for example `press_judge`) confirms it, or a human marks it. Example: `PressController` reports success when the finger did not touch the button.
- **Where the counts live:** a stats file per device package (`stats.yaml`: name, version, sim and robot counts), written by the executor through the host. The announcement carries the maturity.
- The current maturity values (`tested`, `wip`, `untested`, `disabled`; `skill_registry/contract.py`) map to this table. `untested` becomes `experimental`, and `disabled` (hidden from the planner, refused in a tree) is close to `deprecated`.

### Similar work

- **Voyager** (Minecraft, LLM agent): a skill library of code. A skill goes into the library only after a self-check that the task is done. Without the self-check, results were 73 % worse.
- **LRLL** (robot manipulation): an LLM turns experience into new skills (Python functions) and adds them to the robot's skill library.
- **Playful-RATs** (2026, Berkeley): the closest to this design. Skills start `experimental`, go to `verified` after ≥ 3 uses with a success rate ≥ 0.5, and go to `deprecated` after ≥ 10 uses with a success rate ≤ 0.2. Verified skills can go down. Reliability uses the Wilson lower bound. The planner sees verified skills first and does not see deprecated skills.
- **Systems survey of agentic skills** (2026): stages (develop, staging, production, deprecation, removal), semantic versions, sandboxes, code signing and per-skill permissions.

Differences in our design:
- These works judge success in the sim or with an LLM critic. We use a check skill or a human.
- We add a human gate before the first robot use, and the agent can never use the low-level layer.

Sources:
- Voyager: https://arxiv.org/abs/2305.16291
- LRLL: https://arxiv.org/abs/2406.18746
- Playful-RATs: https://arxiv.org/abs/2606.19419 (section 3 and appendix, skill reliability)
- Survey: https://arxiv.org/abs/2608.29596

## Open questions

- Who can approve a skill for the robot: any teammate, or only the owner of that device's stack?
- Where does the trace go when there is no cloud link in the elevator: does the NUC keep the counts and sync them later?
