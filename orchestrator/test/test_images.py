import io

from types import SimpleNamespace

import numpy as np
import pytest

from PIL import Image

from orchestrator.images import ImageError, draw_outlines, image_to_jpeg, image_to_rgb


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



def test_outlines_are_drawn_on_the_frame_in_their_color():
    buffer = io.BytesIO()
    Image.new('RGB', (200, 100)).save(buffer, format='JPEG')
    red    = (1.0, 0.0, 0.0)

    data, kind = draw_outlines(buffer.getvalue(), [(10, 50), (100, 50)], [red, red])
    out        = np.array(Image.open(io.BytesIO(data)))

    assert kind == 'image/jpeg' and out[50, 50, 0] > 200 and out[50, 50, 1] < 60      # on the line: red
    assert out[20, 50].max() < 30                                                       # off the line: as it was


def test_the_overlay_draws_on_the_frame_with_the_outlines_stamp():
    from rclpy.qos import DurabilityPolicy, ReliabilityPolicy
    from builtin_interfaces.msg import Time
    from geometry_msgs.msg import Point
    from std_msgs.msg import ColorRGBA, Header
    from visualization_msgs.msg import ImageMarker

    from orchestrator.agent_ros import RosReader
    from orchestrator.press_capture import FrameBuffer

    def jpeg(grey):
        buffer = io.BytesIO()
        Image.new('RGB', (200, 100), (grey, grey, grey)).save(buffer, format='JPEG')
        return buffer.getvalue()

    def outline(sec):
        return ImageMarker(header=Header(stamp=Time(sec=sec)), points=[Point(x=10.0, y=50.0), Point(x=100.0, y=50.0)],
                           outline_colors=[ColorRGBA(r=1.0, a=1.0)] * 2)

    frames = FrameBuffer()
    frames.add(3.0, jpeg(30))
    frames.add(4.0, jpeg(120))                                          # newer, but track_3d outlined frame 3

    publisher = SimpleNamespace(qos_profile=SimpleNamespace(durability=DurabilityPolicy.VOLATILE, reliability=ReliabilityPolicy.RELIABLE))
    node      = SimpleNamespace(get_publishers_info_by_topic=lambda topic: [publisher], destroy_subscription=lambda s: None,
                                create_subscription=lambda kind, topic, callback, qos: [callback(m) for m in (outline(1), outline(3))])

    data, _ = RosReader(node, lambda: None, frame_at=frames.at)._overlay()
    out     = np.array(Image.open(io.BytesIO(data)))

    assert abs(int(out[20, 50, 0]) - 30) < 8 and out[50, 50, 0] > 200   # frame 3's grey, the line on it; outline 1 had no frame

    late = iter([None, None])                                           # frame 3 reaches the buffer after its outline
    data, _ = RosReader(node, lambda: None, frame_at=lambda t: next(late, None) or frames.at(t))._overlay()

    assert abs(int(np.array(Image.open(io.BytesIO(data)))[20, 50, 0]) - 30) < 8
