"""Raw camera images to JPEG. No ROS needed: a message is anything with height, width, step, encoding and data.

The Orin's RealSense publishes only raw frames (/camera/camera/color/image_raw, RGB8, 1280x720 at 6 fps; no
compressed transport is installed there), so the executor encodes what it keeps.
"""

import io
import math
import zlib
import struct
import time
import threading

import numpy as np


CHANNELS = {'rgb8': 3, 'bgr8': 3, 'rgba8': 4, 'bgra8': 4, 'mono8': 1}


class ImageError(ValueError):
    pass


def image_to_rgb(message) -> np.ndarray:
    """An HxWx3 uint8 RGB array from a sensor_msgs/Image"""
    channels = CHANNELS.get(message.encoding)

    if channels is None:
        raise ImageError(f'image encoding {message.encoding} is not supported')

    pixels = np.frombuffer(bytes(message.data), dtype=np.uint8).reshape(message.height, message.step)
    pixels = pixels[:, :message.width * channels].reshape(message.height, message.width, channels)

    if message.encoding == 'mono8':
        return np.repeat(pixels, 3, axis=2)

    if message.encoding.startswith('bgr'):
        pixels = pixels[:, :, 2::-1]

    return np.ascontiguousarray(pixels[:, :, :3])


def _png(rgb: np.ndarray) -> bytes:
    """A minimal PNG writer, for when neither Pillow nor OpenCV is there"""
    height, width, _ = rgb.shape
    raw              = b''.join(b'\0' + rgb[row].tobytes() for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def draw_outlines(jpeg: bytes, points, colors, width: int = 3) -> tuple[bytes, str]:
    """jpeg with a line from points[2k] to points[2k+1] in colors[2k] (r, g, b in 0..1): a LINE_LIST, as JPEG"""
    from PIL import Image, ImageDraw

    image = Image.open(io.BytesIO(jpeg)).convert('RGB')
    draw  = ImageDraw.Draw(image)

    for start, end, color in zip(points[0::2], points[1::2], colors[0::2]):
        draw.line([start, end], fill=tuple(round(255 * channel) for channel in color), width=width)

    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=90)

    return buffer.getvalue(), 'image/jpeg'


def encode_rgb(rgb: np.ndarray, max_side: int | None = None, quality: int = 90) -> tuple[bytes, str]:
    """JPEG through Pillow or OpenCV, else PNG. With max_side, the long side is cut to it by striding"""
    if max_side:
        step = max(1, math.ceil(max(rgb.shape[:2]) / max_side))
        rgb  = np.ascontiguousarray(rgb[::step, ::step])

    try:
        from PIL import Image

        buffer = io.BytesIO()
        Image.fromarray(rgb).save(buffer, format='JPEG', quality=quality)

        return buffer.getvalue(), 'image/jpeg'
    except ImportError:
        pass

    try:
        import cv2

        ok, encoded = cv2.imencode('.jpg', rgb[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, quality])

        if ok:
            return encoded.tobytes(), 'image/jpeg'
    except ImportError:
        pass

    return _png(rgb), 'image/png'


def image_to_jpeg(message) -> bytes:
    """A sensor_msgs/Image as JPEG at full size (PNG only if no encoder is installed)"""
    return encode_rgb(image_to_rgb(message))[0]


def stamp_seconds(stamp) -> float:
    """A header stamp as float seconds, the key of a frame buffer"""
    return stamp.sec + stamp.nanosec * 1e-9


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

    def at(self, t: float) -> bytes | None:
        """The frame with exactly this stamp, e.g. the one track_3d outlined"""
        with self._lock:
            return next((data for stamp, data in self._frames if stamp == t), None)


class Camera:
    """The latest camera frame, encoded only when someone asks for one: the robot publishes faster than a person
    looks or a VLM asks. Any message with height, width, step, encoding and data; max_side None keeps the full size"""

    def __init__(self, topic: str = '', max_side: int | None = None, quality: int = 90):
        self.topic     = topic
        self.max_side  = max_side
        self.quality   = quality
        self._lock     = threading.Lock()
        self._message  = None
        self._at       = 0.0
        self._encoded  = None                        # (the message it came from, bytes, media type)
        self.error     = ''                          # why the last frame could not be encoded

    def put(self, message):
        with self._lock:
            self._message, self._at, self._encoded = message, time.time(), None

    def state(self) -> dict:
        """What the page shows next to the frame"""
        with self._lock:
            message = self._message

            if message is None:
                return {'have': False, 'topic': self.topic}

            return {'have': True, 'topic': self.topic, 'stamp': self._at, 'age_s': round(time.time() - self._at, 1),
                    'width': message.width, 'height': message.height}

    def frame(self) -> tuple[bytes, str] | None:
        """The latest frame as JPEG, or None; the encoding runs on the reader's thread, not on the spin thread"""
        with self._lock:
            message, encoded = self._message, self._encoded

        if message is None:
            return None

        if encoded is not None and encoded[0] is message:
            return encoded[1], encoded[2]

        try:
            body, media_type = encode_rgb(image_to_rgb(message), max_side=self.max_side, quality=self.quality)
        except ImageError as error:
            self.error = str(error)
            return None

        with self._lock:
            if self._message is message:
                self._encoded = (message, body, media_type)

        return body, media_type
