import io

from types import SimpleNamespace

import numpy as np
import pytest

from PIL import Image

from orchestrator.common.images import ImageError, image_to_jpeg, image_to_rgb


def _message(pixels: np.ndarray, encoding: str, padding: int = 0) -> SimpleNamespace:
    height, width = pixels.shape[:2]
    rows          = pixels.reshape(height, -1)
    step          = rows.shape[1] + padding

    return SimpleNamespace(height=height, width=width, step=step, encoding=encoding,
                           data=np.hstack([rows, np.zeros((height, padding), np.uint8)]).tobytes())


def test_rgb_and_bgr_frames_come_out_as_rgb_even_with_row_padding():
    rgb = np.zeros((4, 6, 3), np.uint8)
    rgb[..., 0] = 200                                           # red

    assert (image_to_rgb(_message(rgb, 'rgb8', padding=2)) == rgb).all()
    assert (image_to_rgb(_message(rgb[..., ::-1].copy(), 'bgr8')) == rgb).all()


def test_a_raw_frame_becomes_a_jpeg_of_the_same_size():
    rgb  = np.full((48, 64, 3), (10, 200, 30), np.uint8)
    jpeg = Image.open(io.BytesIO(image_to_jpeg(_message(rgb, 'rgb8'))))

    assert jpeg.format == 'JPEG' and jpeg.size == (64, 48)
    assert jpeg.getpixel((32, 24))[1] > 180


def test_an_unknown_encoding_is_refused():
    with pytest.raises(ImageError):
        image_to_rgb(_message(np.zeros((2, 2, 3), np.uint8), '16UC1'))
