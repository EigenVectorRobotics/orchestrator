# Sequences

How the executor starts, how a tree is admitted and run, how a task is planned, and how one skill call and one agent call unfold. Read [Classes and interfaces](classes.md) first for the objects these diagrams name.

## Executor startup

Everything skills share is built once, before any tree arrives. Each contract's clients are made early, so a tree's first call finds them ready.

```mermaid
sequenceDiagram
  participant EX as ExecutorNode
  participant SR as SkillRunner
  participant CTX as SkillContext
  participant D as Driver
  participant LIB as SkillLibrary
  participant S as Skill in skill.py
  participant AG as Agent
  participant TR as TreeRunner
  EX->>EX: load_skills gives the registry, places, essential devices
  EX->>EX: SkillRunnerConfig.from_node declares the skill parameters
  EX->>EX: build the PushAgentBackend, or None without a bridge host
  EX->>SR: SkillRunner(node, registry, config, library, backend)
  SR->>SR: build Camera and VlmClient from the config
  SR->>CTX: SkillContext(node, camera, vlm_client, artifacts_dir, drivers)
  Note over CTX: keeps them, builds Tf and the nine drivers
  loop each contract in the registry
    SR->>CTX: prepare(contract)
    CTX->>CTX: watch(success_when) if any
    CTX->>D: prepare(contract, ctx) makes its clients
    SR->>LIB: find(skill, "skill", Skill)
    opt the folder has skill.py
      SR->>S: Skill(contract, ctx) then setup()
    end
  end
  SR->>AG: Agent(backend, ctx, library) when there is a backend
  EX->>TR: TreeRunner(make_leaf_factory(registry, runner), trace, ...)
  EX->>EX: subscribe to the tree, cancel and recorder topics, start the 10 Hz timer
```

A contract whose `prepare` fails is only logged. It fails later, when a tree calls it.

## A tree arrives

The executor decides whether a tree may run; `TreeRunner` runs it. A refused tree leaves the running one alone.

```mermaid
sequenceDiagram
  participant T as /orchestrator/tree
  participant EX as ExecutorNode
  participant V as TreeValidator
  participant TR as TreeRunner
  participant F as leaf factory
  participant BT as BehaviorTree
  T->>EX: XML, or JSON with skip_preflight
  EX->>EX: recorder_refusal(require_recorder, recorder up, status)
  EX->>V: validate(xml) with blind, essential devices, places
  V-->>EX: issues, each traced
  alt refused by the recorder gate or validation
    EX-->>T: trace rejected, status rejected
  else admitted so far
    EX->>TR: build(xml)
    TR->>BT: BehaviorTree(xml, leaf factory, new BTContext)
    loop each skill node
      BT->>F: build_leaf(skill, path, attributes, context)
      F-->>BT: AgentLeaf for an agent skill, else SkillLeaf
    end
    EX->>EX: preflight_gate(tree id, passed, skip, required)
    alt the gate is closed
      EX-->>T: trace rejected, status rejected
    else open
      EX->>TR: start(tree)
      TR->>TR: halt and replace the running tree
      TR-->>EX: status running, trace started
      loop 10 Hz timer
        EX->>TR: tick()
        TR->>BT: tick()
      end
      TR->>EX: on_finished(tree id, status)
      EX->>EX: a preflight sets preflight_passed
    end
  end
```

Starting a preflight closes the gate until it succeeds. `send_tree --skip-preflight` runs a task tree anyway and traces `preflight_skipped`.

## Planning a task

The planner gates on preflight, then loops the LLM and the validator until the tree is clean or three attempts are spent.

```mermaid
sequenceDiagram
  autonumber
  actor Op as Operator
  participant PN as planner_node
  participant EX as executor_node
  participant PL as Planner
  participant LLM as Backend
  participant V as TreeValidator
  Op->>PN: /orchestrator/task "Go to floor 2"
  PN->>EX: /orchestrator/tree with preflight.xml
  EX-->>PN: the trace shows the preflight's end
  Note over PN,EX: a failed preflight stops here
  PN->>PL: plan(task, places, live_skills, blind)
  loop at most max_attempts = 3
    PL->>LLM: complete(system prompt, messages)
    LLM-->>PL: XML tree, or use_tree id with parameters
    PL->>PL: expand the library tree and its replace tags
    PL->>V: validate(xml)
    V-->>PL: issues
    alt errors
      PL->>PL: add the errors to the messages
    else clean
      PL-->>PN: PlanResult with the tree
    end
  end
  PN->>EX: /orchestrator/tree with the task tree
```

The system prompt carries every contract, the places, the live skills and the tree library.

## One skill call

A leaf starts its call once, then polls the handle every tick. The skill's own code runs around the context's call.

```mermaid
sequenceDiagram
  participant BT as BehaviorTree
  participant L as SkillLeaf
  participant R as SkillRunner
  participant S as Skill in skill.py
  participant CTX as SkillContext
  participant D as Driver
  participant H as Handle
  BT->>L: tick()
  L->>L: resolve_inputs from attributes and blackboard
  L->>R: start(contract, inputs)
  R->>S: before(inputs)
  alt refused
    S-->>R: a failed DoneHandle
  else goes on
    R->>S: start(inputs)
    S-->>R: None, so the transport runs
    R->>CTX: start(contract, inputs)
    CTX->>D: start(contract, inputs, ctx)
    D-->>CTX: handle
    CTX-->>R: handle
    R->>S: wrap(handle, inputs)
    S-->>R: the same or a replaced handle
  end
  R-->>L: handle
  loop every tick
    BT->>L: tick()
    L->>H: poll()
    H-->>L: RUNNING, SUCCESS or FAILURE
  end
  opt timeout_s passed
    L->>H: cancel()
  end
  L->>BT: record the call, then SUCCESS or FAILURE
```

A skill without `skill.py` goes straight to the context. Action handles keep their feedback, so a failure carries its whole trajectory as evidence.

## One agent call

Rules answer first when the evidence is plain. Otherwise the bridge asks a model, and the executor refuses bad decisions until one passes.

```mermaid
sequenceDiagram
  participant AL as AgentLeaf
  participant AG as Agent
  participant SK as AgentSkill in agent.py
  participant SS as AgentSession
  participant BE as PushAgentBackend
  participant BR as agent bridge
  participant M as Claude model or operator
  AL->>SK: refuse(call)
  AL->>AG: start(contract, inputs, context)
  AG-->>AL: AgentHandle, the work runs in a thread
  AG->>SK: evidence(call, evidence)
  AG->>SK: rule(call, evidence)
  alt a rule decides
    SK-->>AG: decision and speech
  else ask the bridge
    AG->>BE: run(task, session)
    BE->>BR: open the call with evidence and images
    BR->>M: system prompt and user message from prompt.py
    loop until accepted, the third refusal fails the call
      M-->>BR: decision as JSON, schema from FIELDS
      BR->>BE: decision
      BE->>SS: decide(arguments)
      SS->>SK: check(decision)
      alt refused
        BE->>BR: refused, with the reason
        BR->>M: decide again, same schema
      else accepted
        BE->>BR: accepted
      end
    end
    AG->>SK: after_decision()
  end
  AG->>SK: outputs()
  AL->>AG: poll the handle each tick
```

A decision in `GOING_ON` gives SUCCESS, and `abort` fails the whole tree. With no word from the bridge for 60 s the call fails, and later calls fail at once until it returns.
