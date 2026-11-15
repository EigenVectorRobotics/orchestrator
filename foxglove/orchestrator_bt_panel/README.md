# Orchestrator Behavior Tree panel (Foxglove)

A Foxglove panel that shows the orchestrator behavior tree from `/orchestrator/trace`: the node status (green success, red failure, yellow running), the current node, the last thing the robot said, and a log of the task, the plan, agent decisions, failed actions and the run end. Click a row with children to fold it.

- **Live** (foxglove_bridge on the NUC): the panel builds the tree from the `started` event of a run. The trace topic is volatile, so when Foxglove connects in the middle of a run, the panel builds the tree from the node paths it sees (SubTree names are not known then).
- **Playback** (a recorder bag, `~/orch_runs/<session>/runs/<n>-<tree>/bag`, which records `/orchestrator/trace`): the panel shows the tree at the playback time; seek works.

No build step: `dist/extension.js` is plain CommonJS and DOM.

## Install

```bash
python3 scripts/pack_foxglove_panel.py            # writes foxglove/orchestrator_bt_panel/eigenvector.orchestrator-bt-0.1.0.foxe
python3 scripts/pack_foxglove_panel.py --install  # also installs it into the Foxglove desktop app on this machine
```

Or drag the `.foxe` file into the Foxglove window. Then add the panel "Behavior Tree", or import the layout `foxglove_orchestrator_layout.json`.
