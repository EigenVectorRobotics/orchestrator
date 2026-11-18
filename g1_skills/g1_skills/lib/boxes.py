import threading


class BoxBuffer:
    """The target button's box per camera frame: (stamp, (x_min, y_min, x_max, y_max) in pixels)"""

    def __init__(self, keep_s: float = 8.0):
        self.keep_s = keep_s
        self._boxes = []
        self._lock  = threading.Lock()

    def add(self, t: float, box: tuple):
        with self._lock:
            self._boxes.append((t, box))
            self._boxes = [entry for entry in self._boxes if entry[0] >= t - self.keep_s]

    def at(self, t: float, max_age_s: float):
        """The box of the frame stamped t, or the latest before it within max_age_s"""
        with self._lock:
            earlier = [entry for entry in self._boxes if t - max_age_s <= entry[0] <= t + 1e-3]

        return earlier[-1][1] if earlier else None
