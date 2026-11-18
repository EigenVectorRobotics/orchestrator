"""Raw camera images to JPEG. No ROS needed: a message is anything with height, width, step, encoding and data.

The Orin's RealSense publishes only raw frames (/camera/camera/color/image_raw, RGB8, 1280x720 at 6 fps; no
compressed transport is installed there), so the executor encodes what it keeps.
"""

import io
import math
import zlib
import struct

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
