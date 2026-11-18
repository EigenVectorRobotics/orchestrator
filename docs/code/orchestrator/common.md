# orchestrator/common

Helpers every part of the core uses. They hold no state of the executor, and most need no ROS.

- **`chat_completions.py`**: One POST to an OpenAI-compatible chat endpoint.
- **`geometry.py`**: Quaternions, frame transforms and planar moves, without numpy.
- **`images.py`**: ROS images to RGB arrays and JPEG.
- **`paths.py`**: Where files live on the robot: artifacts, state kept across runs, run records.
- **`places.py`**: Named places from either places file format.
- **`site.py`**: Site profiles (`config/sites/`): places, planner, bridge address, raw bag.
- **`topics.py`**: Every orchestrator topic name and QoS profile.
