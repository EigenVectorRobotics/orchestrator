# Classes and interfaces

Who owns what inside the executor, how long each object lives, and the interfaces between the parts. The API reference has every class in full; this page shows how they fit together.

## Who owns what

Most objects live as long as the executor. Each tree run owns its tree, and each skill call owns its handle.

```mermaid
flowchart TB
  subgraph LIFE["Lives as long as the executor"]
    EXE["ExecutorNode<br/>admission: recorder gate, validation, preflight gate"]
    TRC["Tracer<br/>/orchestrator/trace, /orchestrator/log"]
    TR["TreeRunner<br/>builds, starts, ticks, cancels"]
    SR["SkillRunner<br/>Skill hooks, the Agent"]
    CTX["SkillContext<br/>everything that talks to the outside"]
    DRV["9 drivers<br/>action, service, topic, watch, vlm,<br/>python, compare, alive, command"]
    RES["ROS clients, publishers, TopicWatches,<br/>Camera, Tf, VlmClient, thread pools"]
    SH["shared skill state<br/>PressState, Tracking"]
    SK["Skill objects<br/>one per skill.py"]
    AG["Agent<br/>one call at a time"]
    AS["AgentSkill objects<br/>one per agent skill"]
    BE["PushAgentBackend + PushLink"]
  end
  subgraph RUN["One per tree run"]
    BT["BehaviorTree"]
    BTC["BTContext<br/>blackboard, history, last_failure"]
    LEAF["SkillLeaf, AgentLeaf<br/>control nodes, decorators"]
  end
  subgraph CALL["One per call"]
    H["Handle<br/>ActionHandle, ServiceHandle, ..."]
    AH["AgentHandle and AgentSession"]
  end
  EXE --> TRC
  EXE --> TR
  EXE --> SR
  EXE -->|"builds, hands to SkillRunner"| BE
  SR --> CTX
  SR --> SK
  SR --> AG
  CTX --> DRV
  CTX --> RES
  CTX --> SH
  AG --> AS
  AG --> BE
  TR --> BT
  BT --> BTC
  BT --> LEAF
  LEAF -->|"start(contract, inputs)"| SR
  LEAF -->|"start(contract, inputs, context)"| AG
  LEAF --> H
  LEAF --> AH
  DRV -.->|"borrow clients"| RES
  SK -.->|"use"| CTX
  AS -.->|"use"| CTX
```

The executor knows no skill state. A rejected or finished tree leaves everything in the top box untouched, so the next tree finds its clients ready.

## Interfaces between the parts

Each boundary is a small base class or protocol. A part depends on the interface, so the tests swap in fakes without ROS.

| Interface | Defined in | Implemented by | Used by |
|---|---|---|---|
| `SkillRunner` protocol: `start(contract, inputs)`, `agent` | `orchestrator/orchestrator/tree/leaf.py` | `skills.runtime.SkillRunner`, test fakes | `SkillLeaf`, `make_leaf_factory` |
| `AgentRunner` protocol: `start(contract, inputs, context, path)` | `orchestrator/orchestrator/tree/leaf.py` | `agent.api.Agent` | `AgentLeaf` |
| `LeafFactory` protocol | `orchestrator/orchestrator/tree/engine.py` | `make_leaf_factory()` | `BehaviorTree`, `TreeRunner` |
| `Trace`, `Logger` protocols | `orchestrator/orchestrator/tree/runner.py` | `Tracer`, the node's logger | `TreeRunner` |
| `Handle`: `poll()`, `cancel()`, `outputs`, `message` | `orchestrator/orchestrator/skills/api.py` | the drivers' handles, `InViewHandle`, `ReachHandle` | `SkillLeaf` polls it each tick |
| `Skill`: `setup`, `before`, `start`, `wrap`, `shutdown` | `orchestrator/orchestrator/skills/api.py` | each `skill.py` in `g1_skills` | `SkillRunner` |
| `Driver`: `prepare`, `start` | `orchestrator/orchestrator/skills/transports.py` | the nine built-in drivers | `SkillContext` only |
| `AgentSkill`: `evidence`, `rule`, `check`, `outputs`, ... | `orchestrator/orchestrator/agent/api.py` | each `agent.py` in `g1_skills` | `Agent`, `AgentSession` |
| `AgentBackend` protocol: `run(task, session, ...)` | `orchestrator/orchestrator/agent/api.py` | `PushAgentBackend` | `Agent` |
| `Backend` protocol: `complete(system, messages)` | `orchestrator/orchestrator/planning/backends.py` | Claude, OpenAI-compatible, scripted, external | `Planner` |
| `Contract` | `skill_registry/skill_registry/contract.py` | every `contract.yaml` | registry, validator, planner, context, leaves |

## Skill runtime

`SkillRunner` runs a skill's own hooks around the context's call. `SkillContext` owns the drivers and every client they borrow.

```mermaid
classDiagram
  direction TB
  class SkillRunner {
    +ctx: SkillContext
    +library: SkillLibrary
    +skills: dict
    +agent: Agent
    +prepare(contract)
    +start(contract, inputs) Handle
    +shutdown()
  }
  class SkillRunnerConfig {
    +image_topic: str
    +camera_info_topic: str
    +vlm: VlmClientConfig
    +artifacts_dir: str
    +from_node(node)$ SkillRunnerConfig
  }
  class SkillContext {
    +node
    +camera: Camera
    +tf: Tf
    +vlm_client: VlmClient
    +drivers: dict
    +prepare(contract)
    +start(contract, inputs) Handle
    +shared(key, factory)
    +submit(pool, function) Future
    +watch(spec) TopicWatch
    +action_client(name, type)
    +service_client(name, type)
    +publisher(name, type)
  }
  class SkillLibrary {
    +packages: list
    +module(skill, part) module
    +find(skill, part, base) type
  }
  class Skill {
    +contract: Contract
    +ctx: SkillContext
    +config: dict
    +setup()
    +before(inputs) Handle
    +start(inputs) Handle
    +wrap(handle, inputs) Handle
    +shutdown()
  }
  class Driver {
    +prepare(contract, ctx)
    +start(contract, inputs, ctx) Handle
  }
  class Handle {
    +status: Status
    +message: str
    +outputs: dict
    +poll() Status
    +cancel()
  }
  SkillRunner ..> SkillRunnerConfig : built from
  SkillRunner *-- SkillContext
  SkillRunner --> SkillLibrary : finds skill.py
  SkillRunner "1" *-- "*" Skill
  SkillContext "1" *-- "9" Driver
  Skill --> SkillContext
  Driver ..> Handle : creates
  Driver <|-- ActionDriver
  Driver <|-- ServiceDriver
  Driver <|-- TopicDriver
  Driver <|-- WatchDriver
  Driver <|-- VlmDriver
  Driver <|-- PythonDriver
  Driver <|-- CompareDriver
  Driver <|-- AliveDriver
  Driver <|-- CommandDriver
  Handle <|-- DoneHandle
  Handle <|-- FutureHandle
  Handle <|-- ServerHandle
  ServerHandle <|-- ActionHandle
  ServerHandle <|-- ServiceHandle
  Handle <|-- TopicHandle
  Handle <|-- VlmHandle
  Skill <|-- PressButton
  Skill <|-- SetPressOffset
  Skill <|-- ButtonInView
  Skill <|-- CheckReach
  Handle <|-- InViewHandle
  ServiceHandle <|-- ReachHandle
```

`PressButton`, `SetPressOffset`, `ButtonInView`, `CheckReach` and their handles live in `g1_skills`. `ButtonInView` and `CheckReach` use the `python` transport, so they start every call themselves.

## Contracts, validation and planning

A `Contract` is the one description of a skill. The registry, the validator, the planner prompt and the context all read it.

```mermaid
classDiagram
  direction LR
  class Contract {
    +name: str
    +kind: str
    +device: str
    +interface: Interface
    +inputs: list~Port~
    +outputs: list~Port~
    +resources: list
    +timeout_s: float
    +fallback: list
    +needs_cloud: bool
    +maturity: str
    +record: bool
    +config: dict
  }
  class Port {
    +name: str
    +type: str
    +required: bool
    +choices: list
    +field: str
  }
  class Interface {
    +transport: str
    +name: str
    +ros_type: str
    +success_when: Watch
    +failure_when: HeldValue
    +fixed: dict
    +skill: str
    +requires: dict
  }
  class Watch {
    +topic: str
    +field: str
    +equals
    +latched: bool
    +consecutive: int
  }
  class HeldValue {
    +field: str
    +equals
    +hold_s: float
  }
  class Registry {
    +from_directories(dirs) Registry
    +get(name) Contract
  }
  class TreeValidator {
    +validate(xml) list~Issue~
  }
  class ValidatorConfig {
    +essential_devices: set
    +blind: bool
    +live_skills: set
    +known_keys: set
  }
  class Planner {
    +plan(task, places, live_skills, blind, floors, robot_floor) PlanResult
  }
  class Backend {
    <<protocol>>
    +complete(system, messages) Reply
  }
  Contract *-- Interface
  Contract *-- Port
  Interface o-- Watch : success_when
  Interface o-- HeldValue : failure_when
  Registry "1" o-- "*" Contract
  TreeValidator --> Registry
  TreeValidator --> ValidatorConfig
  Planner --> Registry
  Planner --> Backend
  Planner ..> TreeValidator : one per plan
  Backend <|.. ClaudeBackend
  Backend <|.. OpenAICompatibleBackend
  Backend <|.. ScriptedBackend
  Backend <|.. ExternalPlannerBackend
```

The planner, the executor, `send_tree` and `validate_tree` share one validator. Only the planner checks that a skill is live.

## Tree engine and runner

`orchestrator.tree` interprets BehaviorTree.CPP v4 XML in Python. `TreeRunner` runs one tree at a time and leaves the choice of tree to its caller.

```mermaid
classDiagram
  direction TB
  class TreeRunner {
    +tree: BehaviorTree
    +build(xml) BehaviorTree
    +start(tree, detail)
    +tick()
    +cancel()
    +halt()
  }
  class BehaviorTree {
    +id: str
    +root: TreeNode
    +context: BTContext
    +tick() Status
    +halt()
  }
  class BTContext {
    +blackboard: dict
    +history: list
    +last_failure: dict
    +abort_reason: str
    +run_id: str
    +record(summary, full)
    +abort(reason)
  }
  class TreeNode {
    +tag: str
    +path: str
    +children: list
    +status: Status
    +tick() Status
    +halt()
  }
  class SkillLeaf {
    +contract: Contract
    +runner: SkillRunner
    +handle: Handle
  }
  class AgentLeaf {
    +runner: Agent
  }
  TreeRunner --> BehaviorTree : runs one
  BehaviorTree *-- TreeNode : root
  BehaviorTree *-- BTContext
  TreeNode --> BTContext
  TreeNode o-- TreeNode : children
  TreeNode <|-- Sequence
  Sequence <|-- SequenceWithMemory
  Sequence <|-- Fallback
  TreeNode <|-- ReactiveSequence
  ReactiveSequence <|-- ReactiveFallback
  TreeNode <|-- Parallel
  TreeNode <|-- IfThenElse
  TreeNode <|-- Decorator
  Decorator <|-- Inverter
  Decorator <|-- ForceSuccess
  ForceSuccess <|-- ForceFailure
  Decorator <|-- RetryUntilSuccessful
  Decorator <|-- Repeat
  Decorator <|-- KeepRunningUntilFailure
  Decorator <|-- Timeout
  Decorator <|-- Delay
  Decorator <|-- RunOnce
  TreeNode <|-- SkillLeaf
  SkillLeaf <|-- AgentLeaf
  TreeNode <|-- AlwaysSuccess
  TreeNode <|-- AlwaysFailure
  TreeNode <|-- SetBlackboard
  TreeNode <|-- Sleep
```

`SkillLeaf` enforces the contract's `timeout_s` and cancels the handle when it runs out. An `AgentLeaf`'s runner is the `Agent`. An `abort` decision calls `BTContext.abort()`, which fails the whole tree. `Timeout`, `Delay` and `Sleep` also share the `Timed` mixin, which keeps their start time.

## Agent engine and bridge

The executor checks every decision the bridge returns, from the model or the operator. Each `AgentSkill` supplies evidence, rules and checks, and `spec.py` fixes the allowed answers.

```mermaid
classDiagram
  direction TB
  class Agent {
    +backend: AgentBackend
    +ctx: SkillContext
    +skill(contract) AgentSkill
    +start(contract, inputs, context, path) AgentHandle
  }
  class AgentSkill {
    +contract: Contract
    +spec: module
    +ctx: SkillContext
    +refuse(call)
    +evidence(call, evidence)
    +rule(call, evidence)
    +request_fields(call, evidence) dict
    +check(decision, session) str
    +outputs(decision, call, evidence) dict
  }
  class AgentSession {
    +skill: AgentSkill
    +call: AgentCall
    +evidence: dict
    +decision: dict
    +decide(arguments) ToolOutput
  }
  class AgentHandle {
    +going_on: set
    +poll() Status
    +cancel()
  }
  class AgentBackend {
    <<protocol>>
    +run(task, session, should_stop, on_event)
  }
  class PushLink {
    +send(header, blobs) bool
    +open_call(header, blobs)
    +close_call()
  }
  class SkillSpec {
    <<spec.py>>
    PHASES
    GOING_ON
    FIELDS
    ANSWERED_BY
    limits()
  }
  class Bridge {
    <<mission_control>>
    +serve()
  }
  Agent --> AgentBackend
  Agent "1" *-- "*" AgentSkill
  Agent ..> AgentHandle : returns
  Agent ..> AgentSession : one per bridge call
  AgentSession --> AgentSkill
  AgentSkill --> SkillSpec
  AgentBackend <|.. PushAgentBackend
  PushAgentBackend --> PushLink
  PushLink .. Bridge : TCP 8771
  Bridge ..> SkillSpec : answer schema
  AgentSkill <|-- PressJudge
  AgentSkill <|-- ManipulationRecovery
  AgentSkill <|-- OperatorConfirm
```

What each decision does to the leaf and the tree, and what the model sees, is in `docs/agents.md`.
