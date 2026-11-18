import threading


class FrameBuffer:
    """The last few seconds of camera frames: (ROS time, JPEG or PNG bytes)"""

    def __init__(self, keep_s: float = 8.0):
        self.keep_s  = keep_s
        self._frames = []
        self._lock   = threading.Lock()

    def add(self, t: float, data: bytes):
        with self._lock:
            self._frames.append((t, data))
            self._frames = [frame for frame in self._frames if frame[0] >= t - self.keep_s]

    def first_after(self, t: float):
        with self._lock:
            return next((frame for frame in self._frames if frame[0] >= t), None)
