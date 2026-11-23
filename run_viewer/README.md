# Run Viewer

A read-only browser view of a robot run: the behavior tree as nodes, the map with the nav state, the camera with the EdgeTAM masks, and each agent call. Light theme only.

The viewer runs on your laptop. The stack (orchestrator, nav, camera) runs on the NUC, the Orin and the Thor. The laptop needs only ssh to the NUC: no ROS, no install on the robot, no copy of this folder on the robot.

## Watch from a laptop

You need:

- Python 3.10 or newer on the laptop. The viewer uses only the standard library.
- An ssh key that the NUC takes: `ssh -o BatchMode=yes vector@vector-nuc true` must work with no password prompt.
- On the NUC: `run_recorder` running (it writes `~/orch_runs/<session>/events.jsonl`) and `~/eigen_vector_ws/env.sh`.
- On the Orin: `~/eigen_vector_ws/env.sh` with OpenCV. The Orin takes only the NUC's key, so the viewer goes to the Orin through the NUC.

Start it from this folder:

```bash
python3 scripts/start_viewer.py --robot vector@vector-nuc --camera-host vector@vector-nuc,unitree@192.168.123.164
```

Then open http://localhost:8780 in a browser on the laptop. Stop it with Ctrl+C.

What happens:

- The trace and the agent call summaries come over ssh (`tail -F` and a small file watcher on the NUC).
- `scripts/live_echo.py` and `scripts/state_echo.py` go to the NUC as `python3 -c` at each start. They only subscribe to topics and print JSON lines. They do not publish.
- `scripts/camera_echo.py` goes to the Orin the same way. The raw image (1280x720 rgb8, 6 Hz, about 17 MB/s) stays on the Orin. Only a 640 px JPEG at 2 Hz (about 30 KB/s) comes back.
- The ssh streams use `ssh -tt`. When the viewer stops (Ctrl+C or `kill`), the remote commands stop too.
- When an echo stops (robot reboot, network loss), the viewer starts it again after 5 s. When the viewer restarts, an open page loads again by itself.

If the page stays empty, read the grey status line under the header:

| Status line | Meaning |
|---|---|
| `cannot reach vector@vector-nuc: ...` | ssh failed: check the network and the key |
| `no events.jsonl on vector@vector-nuc; is run_recorder running?` | ssh works, but there is no recorder session |
| `vector@vector-nuc:/home/vector/orch_runs/.../events.jsonl` | the viewer follows this session; the tree shows when the executor starts a tree |

Each person can run their own viewer. The viewers do not change anything on the robot.

### Other ways to open it

- The viewer on the NUC, the page on a laptop: run `python3 scripts/start_viewer.py --robot local --camera-host unitree@192.168.123.164` on the NUC, then on the laptop `ssh -N -L 8780:127.0.0.1:8780 vector@vector-nuc` and open http://localhost:8780.
- On the NUC, as preflight starts it: the `launch_run_viewer` contract runs `scripts/start_viewer.py --robot local` from `~/orchestrator_ws/src/orchestrator/run_viewer/` in tmux `run_viewer`, on 127.0.0.1 and the tailscale address, port 8780.
- Other people on a VPN such as tailscale: add `--host <tailscale address>` (and `--host 127.0.0.1` to keep the local address). The viewer has no login, so use only an address on a private network.

## Panes

- **Tree · top level:** the main tree as nodes. A SubTree is one dashed box (`+N` is the number of nodes in it). A wrapper SubTree that is the only one shown (for example `ride_to_goal`) opens by itself, so the skills show. The branch that runs now has an orange border. Click a SubTree to show it in the pane below.
- **press_button level:** the SubTree that holds the `press_button` leaf. It opens by itself when that subtree runs. Follow opens only the branch that runs now. Click a node with children to fold it. If the tree has no `press_button`, the whole tree shows here. The pane has a fixed height, and the tree panes do not scroll by themselves: Focus scrolls both to the node that runs now. When Follow folds or opens a branch, the nodes move to their new places in `--move` (in the page). The node that runs now is the last leaf that reported `running`: a condition that only returns success or failure (for example a guard that a ReactiveSequence checks at each tick) does not take it.
- **Map:** the prior map of the floor that the robot is localized on, faint, because only the route planner uses it. On top: the route planner path (blue dashes), the waypoint (orange), the collision check circle (orange dashes; centre the robot, radius `min(distance to the goal + goal_clear_range, adjacent_range)`, as `local_planner` crops its check), the `/terrain_map` obstacles in it (red) and the others within `adjacent_range` (pale red), `/free_paths` (pale green), the local planner path (green), the robot, and the arm goal (purple arrow: the last pose of `/traj` in `map`; grey after 30 s). Robot centres the view on the robot, Floor shows the whole floor. Drag to pan, scroll to zoom, double-click to centre again.
- **Camera · EdgeTAM masks:** the colour frame with the outlines of `/track_3d/mask_outlines`, one colour per track. The caption says when the frame is more than 3 s old.
- **Agent:** one card per agent call, newest first (the last 40). The card is made from the trace (`agent_start`, `agent_wait`, `agent_rule`, `agent_decision`), the live request and reply topics, and the call's `summary.json`. Its rows: *Input*: the phase and the button (from the `Button:` line of the request's task text). *Failed at* (*judges* for a judge call): the BT node of the request's failure, or for a judge call the `press_button` it judges; click it to show it in the trees. *Decision*: the decision and its numbers, `rejected` when the executor refused it, `rule` when a rule answered without the model, or "waiting" with the time; click ▸ to read the reason. *Cost*: state, seconds, cost, tokens in and out, model time and the number of refused answers, from the call's `summary.json`. *The whole call* opens the call's record from `runs/<run>/agent/<call>/`: the request (task text, allowed decisions), the images the model got, the failure, stance, press and history, every message in order (opened, started with the model, effort and prompt, each decision with accepted or refused and its cost, each reply, failed or cancelled, closed with its outcome) and the summary. The prompt shown is the user message the bridge built. The system prompt and its sha256 are recorded in the call's `messages.jsonl` (`started`), but the page does not show them. The header shows the model (for example Opus 5.5) of the last call with a summary. An open call has an orange background and counts its waiting time.
- **Timing, Chat, Decisions:** time per node, skill or subtree; the planner and agent messages; agent decisions, branches and failures.
- The session menu in the header opens one of the last 10 recorder sessions (`?session=<name>`); the newest is the live one. The run menu shows an earlier tree run of the session.

## Data sources

| Data | From |
|---|---|
| Tree, decisions, timing, chat, agent call start, rule and decision | the newest `~/orch_runs/*/events.jsonl` on the NUC, written by `run_recorder`; the viewer moves to a newer session by itself |
| Agent model, prompt, state, time, tokens, cost (the cards) | `<session>/runs/<run>/agent/<call>/summary.json`, written by `run_recorder`; a summary shows once the trace reaches its answer while its run is open |
| One whole agent call (on request) | `request.json`, `messages.jsonl` and `summary.json` in the same folder; its images from `runs/<run>/captures/`, or from the path the request names while the run is open |
| Agent request and reply, live | `/orchestrator/agent/request`, `/orchestrator/agent/reply` |
| Floor, pose, route, waypoint, local path, terrain, free paths | `/slam_elevatorlio/floor`, `/odom_base_link`, `/route_planner/path`, `/way_point` or `/goal_pose`, `/path`, `/terrain_map`, `/free_paths`; the check parameters from `/local_planner` |
| Arm goal | `/traj` (frame `foot_world`), `/tf`, `/tf_static` |
| Camera and masks | `/camera/camera/color/image_raw` (its `/compressed` when it has a publisher), `/track_3d/mask_outlines` |
| Elevator side, door | `/slam_elevatorlio/elevator_side`, `/door_state` |
| Floor maps | the session's `provenance.json` names the places file; `scripts/make_floor_maps.py` makes them on the robot host from the map that the places file names |

## Options

| Option | Default | Meaning |
|---|---|---|
| `--robot` | `vector@vector-nuc` | ssh host of the recorder and the nav topics, or `local` when the viewer runs on that host |
| `--camera-host` | `--robot` | host of the camera; `a,b` goes to `b` through `a`; `none` turns the camera off |
| `--ros-env` | `~/eigen_vector_ws/env.sh` | ROS environment on the robot host |
| `--camera-env` | `--ros-env` | ROS environment on the camera host |
| `--runs` | `~/orch_runs` | the recorder's session folders on the robot host |
| `--trace` | newest `events.jsonl` | one trace file on the robot host to show, for example an old session |
| `--trace-topic` | off | read the trace from `/orchestrator/trace`, not from `events.jsonl`: use it to replay a recorder bag |
| `--calls` | none | with `--trace-topic`: the session folder whose agent call summaries to show |
| `--host` | `127.0.0.1` | address to listen on; give it more than once for more |
| `--port` | `8780` | port |

## Replay a recorder bag

Copy a session folder from the NUC's `~/orch_runs`. On a machine with ROS 2 that is not on the robot's network, use its own domain, for example `export ROS_DOMAIN_ID=91 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST` in an env file:

```bash
python3 scripts/start_viewer.py --robot local --trace-topic --calls <session> --ros-env <env file> --camera-host none --port 8790
```

```bash
ros2 bag play <session>/runs/<run>/bag
```

The recorder bag has the trace, the agent request, reply and event topics and `/odom_base_link`, but not the route, paths, terrain, floor or camera topics the viewer reads (`BAG_TOPICS` in `orchestrator/orchestrator/recording/node.py`), so the map shows only the robot and the camera pane stays empty. The bag starts after the tree's `started` event, so the tree panes also stay empty. To see a copied session, use `--robot local --runs <folder of sessions>` and pick it in the session menu: all its events show at once, not in real time.

## Floor maps

When the page opens a session, the viewer runs `scripts/make_floor_maps.py` on the robot host (it needs numpy and PyYAML) and keeps the result per places file. The script reads the places file that the session's `provenance.json` names and draws one plan per floor of its `floors` table from the pcd in its `map:`: white floor, dark walls, grey low objects. The ground of a floor is the most filled 2 cm z bin in its range. If `/slam_elevatorlio/floor` is silent, the page uses the floor whose ground is nearest the robot's z.

## Record the page to a video

```bash
node scripts/record_viewer.mjs http://localhost:8780 run.mp4 90
```

It needs Node 22 or newer, ffmpeg and Chrome (set `CHROME` to the binary if it is not at `/Applications/Google Chrome.app`). It takes a 1600x2100 screenshot of the page every 0.5 s in a headless Chrome of its own, so the video plays in real time. It stops after 90 minutes, or earlier with `touch run.mp4.stop`. The file plays while it records, and more than one recorder can run at the same time.

## Files

| File | Job |
|---|---|
| `scripts/start_viewer.py` | start the readers and the web server |
| `scripts/live_echo.py` | on the robot host: nav state (2 Hz), agent request and reply, the trace topic with `--trace-topic` |
| `scripts/camera_echo.py` | on the camera host: the JPEG and the mask outlines (2 Hz) |
| `scripts/state_echo.py` | on the robot host: elevator side and door state |
| `scripts/make_floor_maps.py` | on the robot host: the floor PNGs, their bounds and the places of a places file, as one JSON line |
| `scripts/record_viewer.mjs` | record the page to an MP4 |
| `src/run_viewer/sources.py` | the ssh streams, the trace parser, and the session folders (`Sessions`) |
| `src/run_viewer/store.py` | the event list, its folded snapshot for a new page, and the latest nav and camera values |
| `src/run_viewer/server.py` | the page, the event stream (`/stream`), `/latest`, `/sessions`, `/call`, `/frame` and `/maps` |
| `src/run_viewer/static/index.html` | the page |
