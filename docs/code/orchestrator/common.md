# orchestrator/common

Helpers every part of the core uses. They hold no state of the executor, and most need no ROS.

- **`geometry.py`**: Quaternions, frame transforms and planar moves, without numpy.
- **`images.py`**: ROS images to RGB arrays and JPEG.
- **`config.py`**: Where files live on the robot, named places from either places format, and site profiles (`config/sites/`).
- **`topics.py`**: Every orchestrator topic name and QoS profile.
