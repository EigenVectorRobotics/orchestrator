# orchestrator/sensing

Streams that several skills read. The skill context makes one of each.

- **`camera.py`**: The head camera: the latest frame as JPEG, its size, and recent frames.
- **`frames.py`**: The buffer of recent frames by stamp, without ROS.
- **`tf.py`**: The executor's one TF buffer: lookups, poses in another frame, and plain-number transforms from any thread.
