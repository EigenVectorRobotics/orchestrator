# Trees and the press slot

The library trees the planner reuses, the preflight tree, and the state machine every button press follows. The XML lives in `skill_registry/trees/` and `skill_registry/preflight/`.

## Library tree: subtree calls

One library tree rides the freight elevator. It is built from small subtrees, and `press_panel_button` serves both panels.

```mermaid
flowchart LR
  subgraph TRF["trees/"]
    FE["freight_elevator_go_to_place"]
  end
  subgraph EL["subtrees/elevator.xml"]
    RTG["ride_to_goal"]
    FS["floor_settings"]
    CAW["call_and_wait"]
    CE["call_elevator"]
    PAR["press_and_ride"]
    PFB["press_floor_button"]
  end
  subgraph WK["subtrees/walk.xml"]
    WI["walk_in"]
    WO["walk_out"]
  end
  subgraph PR["subtrees/press.xml"]
    PPB["press_panel_button"]
    SC["stance_check"]
    PNR["press_and_recover"]
    RO["recover_once"]
    LK["look"]
    LH["look_here"]
    PA["prepare_arm"]
    EH["ekf_hold"]
  end
  FE --> RTG
  RTG --> FS
  RTG --> CAW
  RTG --> WI
  RTG --> PAR
  RTG --> WO
  CAW --> CE
  PAR --> PFB
  CE -->|"call panel"| PPB
  PFB -->|"inside panel"| PPB
  PPB --> SC
  PPB --> PNR
  PNR --> RO
  RO --> LK
  SC --> LK
  SC --> LH
  LK --> LH
  LH --> PA
  LH --> EH
```

The planner may pick this tree with `use_tree`, set its parameters, or swap a subtree. The walk subtrees are described in [Architecture](../architecture.md#walking-into-and-out-of-the-cabin).

## Preflight tree

Preflight brings every machine up and proves the robot is ready. The executor runs no task tree until it passes.

```mermaid
flowchart LR
  PF["preflight"]
  V["viewer<br/>launch_run_viewer"]
  SU["stacks_up"]
  N["nuc_stack<br/>nuc_up or launch_nuc_stack"]
  O["orin_stack<br/>orin_up or launch_orin_stack"]
  T["thor_stack<br/>thor_up or launch_thor_stack"]
  SN["stale_nodes<br/>no_duplicate_nodes"]
  LU["localization_up<br/>healthy, or ask to relocalize"]
  RU["robot_up<br/>robot_ready, relocalize_here"]
  PF --> V
  PF --> SU
  SU --> N
  SU --> O
  SU --> T
  PF --> SN
  PF --> LU
  PF --> RU
```

A preflight that does not succeed closes the gate again. The gate itself is in [Sequences](sequences.md#a-tree-arrives).

## The press slot

Every button press runs this slot. It checks the stance first, then presses up to four times with a diagnosis between failures.

```mermaid
stateDiagram-v2
  state "set_press_offset with the panel base offset" as Base
  state "look: walk to goal, settle 2.5 s, look_here" as Look
  state "manipulation_recovery in phase stance_check" as StanceRec
  state "press: set_press_offset, reset_filters, press_button, press_judge" as Press
  state "manipulation_recovery in phase diagnose, or an executor rule" as Diagnose
  state "reposition: walk and look, up to 3 times" as Repo
  state "release arms and say press_ask" as Ask
  state "the tree fails" as Abort
  [*] --> Base
  Base --> Look
  Look --> Press : button_in_view and check_reach pass
  Look --> StanceRec : a step failed
  StanceRec --> Look : continue or reposition, 4 rounds at most
  StanceRec --> Ask : 4 rounds failed
  StanceRec --> Abort : abort
  Press --> [*] : press_judge says pressed
  Press --> Diagnose : failed or not_pressed, presses 1 to 3
  Press --> Ask : press 4 failed
  Diagnose --> Press : retry, offset_retry or switch_arm
  Diagnose --> Repo : reposition
  Repo --> Press : a look passes
  Repo --> Ask : every look failed
  Diagnose --> Ask : ask_help or no decision
  Ask --> [*]
  Abort --> [*]
```

`continue` reruns `look_here` where the robot stands, and `reposition` walks to the new goal first. The full rules are in [Agent skills](../agents.md#the-press-slot).
