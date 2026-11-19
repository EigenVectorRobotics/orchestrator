# System

The machines, the packages and how a task becomes robot motion. Start here for the big picture, then read [Classes and interfaces](classes.md) for the code inside the executor.

## Deployment

The NUC runs the whole orchestrator. The Orin sees, the Thor answers VLM questions, and the operator laptop answers agent calls.

```mermaid
flowchart LR
  subgraph LAP["Operator laptop"]
    BR["agent_bridge.py<br/>Bridge, Model, Operator"]
    BROW["Browser"]
  end
  subgraph NUC["NUC: Ubuntu 24.04, Jazzy"]
    REG["registry_node"]
    PLN["planner_node"]
    EXE["executor_node"]
    REC["run_recorder"]
    RV["Run Viewer :8780"]
    STK["Robot stack<br/>navigation, arm control, body EKF, door_state_node"]
  end
  subgraph ORIN["Orin: Ubuntu 22.04, Humble"]
    CAM["realsense2_camera"]
    TRK["seed_bbox, track_3d<br/>gripper pose estimator"]
  end
  subgraph THOR["Thor: Jazzy"]
    VLM["vlm_server :8090"]
    COS["Cosmos daemon (edgellm)"]
    GND["ground_server"]
  end
  CC["Claude Code session<br/>planner_cli"]
  API[("Anthropic API")]
  EXE <-->|"TCP :8771 push link<br/>JSON + images"| BR
  BR -->|"Claude Agent SDK"| API
  PLN -->|"claude backend"| API
  CC <-->|"planner request and answer"| PLN
  EXE -->|"HTTP /v1 chat"| VLM
  VLM --> COS
  EXE <-->|"ROS actions, services, topics"| STK
  CAM -->|"color image_raw"| EXE
  TRK -->|"boxes, 3D pose"| EXE
  EXE -.->|"thor_up checks"| GND
  REC -->|"~/orch_runs"| RV
  BROW --> RV
```

The executor reads the raw camera stream from the Orin. Without a site file, no bridge address is set and agent skills fail.

## Packages

The core names no skill. It imports contracts from `skill_registry` and finds skill code in `g1_skills` by name at runtime.

```mermaid
flowchart TB
  G1["g1_skills<br/>one folder per skill, and lib/"]
  ORC["orchestrator<br/>executor, planner, agent engine, recorder"]
  TREE["orchestrator.tree<br/>engine, leaves, TreeRunner"]
  SR["skill_registry<br/>contract model, validator, tree library"]
  MC["mission_control<br/>agent bridge, robot scripts"]
  RVW["run_viewer<br/>plain Python, no ROS package"]
  G1 -->|"imports skills.api, agent.api,<br/>sensing, common, tree.engine"| ORC
  ORC --> TREE
  ORC -->|"imports Contract, Registry,<br/>TreeValidator"| SR
  TREE -->|"imports Contract, Registry"| SR
  SR -.->|"reads every contract.yaml"| G1
  ORC -.->|"SkillLibrary imports skill.py,<br/>agent.py, spec.py by name"| G1
  MC -->|"imports agent.protocol,<br/>common.config"| ORC
  MC -.->|"protocol loads spec.py, prompt.py"| G1
  RVW -.->|"reads run folders and live echoes"| ORC
```

Solid arrows are imports; dashed arrows are files found at runtime. `tree/leaf.py` still imports the agent and skill APIs for `AgentLeaf`, the last tie before `tree/` can stand alone.

## From a task to robot motion

A sentence becomes a validated tree, and every step lands on the trace. The recorder keeps all of it.

```mermaid
flowchart LR
  TASK(["/orchestrator/task"])
  REG["registry_node"]
  PLN["planner_node"]
  LLM["LLM backend"]
  EXE["executor_node"]
  BR["agent bridge"]
  ROB["Robot<br/>actions, services, topics"]
  VLM["VLM on Thor"]
  TR(["/orchestrator/trace"])
  AG(["/orchestrator/agent/<br/>request, reply, event"])
  REC["run_recorder"]
  RUNS[("~/orch_runs")]
  TASK --> PLN
  REG -->|"/skills/live"| PLN
  PLN <-->|"prompt and XML tree"| LLM
  PLN -->|"/orchestrator/tree<br/>preflight, then task tree"| EXE
  EXE -->|"skill calls"| ROB
  EXE -->|"vlm skills"| VLM
  EXE <-->|"agent calls"| BR
  EXE --> TR
  PLN --> TR
  TR -->|"preflight result"| PLN
  EXE --> AG
  TR --> REC
  AG --> REC
  TASK --> REC
  REC --> RUNS
```

The planner learns the preflight result from the trace. The topic names live in `orchestrator/orchestrator/common/topics.py`.

## A skill folder

Each file in a skill folder has one reader in the core. A contract alone is a working skill.

```mermaid
flowchart LR
  subgraph F["g1_skills/g1_skills/SKILL/"]
    C["contract.yaml<br/>kind, device, ports, interface, timeout_s, maturity"]
    S["skill.py<br/>one Skill subclass"]
    A["agent.py<br/>one AgentSkill subclass"]
    SP["spec.py<br/>PHASES, GOING_ON, FIELDS, limits()"]
    P["prompt.py<br/>system prompt and user message"]
  end
  LIB["g1_skills/lib<br/>PressState, Tracking, text"]
  REGI["Registry and TreeValidator"]
  PLNR["Planner prompt"]
  CTX["SkillContext<br/>prepares and calls the transport"]
  RUN["SkillRunner<br/>runs the Skill hooks"]
  AGT["Agent and AgentSession"]
  BRG["agent bridge"]
  C --> REGI
  C --> PLNR
  C --> CTX
  S --> RUN
  A --> AGT
  SP --> AGT
  SP --> BRG
  P --> BRG
  S -.-> LIB
  A -.-> LIB
```

Only `python` skills must have `skill.py`. The executor and the bridge both read `spec.py`, so both enforce the same decisions.
