"""Success rate of Thor's button grounding (seed_bbox -> GroundBoxes -> Cosmos) on sim head camera frames.

The robot is put at random poses near each press pose (+-10 cm along and across, +-10 deg), the g1_head view is rendered
and streamed as sim_orin.py streams it (640x480 centre crop, JPEG q95), and the query goes to /keyboard_input as the
press skill sends it. The box on /init_bbox (or seed_bbox's "no accepted box" on /rosout) is scored against the sim's
button caps projected into the frame: correct (box centre on the queried cap), plate (the box holds the cap centre but
its centre is off the cap), wrong (on another button's cap), other, or none.

Run with nothing else on ROS domain 73 but Thor's perception (thor_perception.sh), on vector-pc (vector_pc.env):
    ROS_STATIC_PEERS=100.94.122.122 python3 grounding_test.py <places.yaml> <results.json> [samples per pose] [frames dir]
        [JSON {query: text sent instead}: then only those queries run, e.g. '{"floor_2": "object:..."}']
"""
import io
import json
import math
import sys
import time

import mujoco
import numpy as np
import rclpy
import yaml

from PIL import Image as PilImage, ImageDraw
from rcl_interfaces.msg import Log
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from vision_msgs.msg import Detection2DArray

from elevator_sim import ElevatorEnvironment

PANELS = {"call_panel": (("up", "hall_up"), ("down", "hall_down")), "panel": (("floor_1", "floor_1"), ("floor_2", "floor_2"))}


def caps(
    env: ElevatorEnvironment,
) -> dict:
    """Sim button id -> (cap centre in the world, cap radius) for the buttons of the robot's panel."""
    found = {}
    for button in ("hall_up", "hall_down", "floor_1", "floor_2"):
        try:
            info = env.button_info(button)
        except ValueError:
            continue
        body = env.model.jnt_bodyid[mujoco.mj_name2id(env.model, mujoco.mjtObj.mjOBJ_JOINT, info["joint"])]
        radius = max(float(env.model.geom_size[g][0]) for g in range(env.model.ngeom) if env.model.geom_bodyid[g] == body)
        found[button] = (np.asarray(info["position"]), radius)

    return found


def frame(
    env: ElevatorEnvironment,
) -> tuple[bytes, np.ndarray, tuple]:
    """The g1_head frame as sim_orin streams it (JPEG q95), world <- optical frame, and fx, fy, cx, cy."""
    rgb = env.render("g1_head", width=1280, height=720)
    rgb = np.asarray(PilImage.fromarray(rgb[:, 160:1120]).resize((640, 480), PilImage.BILINEAR))
    native = env.camera_intrinsics("g1_head", width=1280, height=720)
    k = (native["fx"] * 2 / 3, native["fy"] * 2 / 3, (native["cx"] - 160 + 0.5) * 2 / 3 - 0.5, (native["cy"] + 0.5) * 2 / 3 - 0.5)
    head = mujoco.mj_name2id(env.model, mujoco.mjtObj.mjOBJ_CAMERA, "g1_head")
    pose = np.eye(4)
    pose[:3, :3] = env.data.cam_xmat[head].reshape(3, 3) @ np.diag([1.0, -1.0, -1.0])
    pose[:3, 3] = env.data.cam_xpos[head]
    buffer = io.BytesIO()
    PilImage.fromarray(rgb).save(buffer, format="JPEG", quality=95)

    return buffer.getvalue(), pose, k


def score(
    box,
    query_button: str,
    buttons: dict,
    pose: np.ndarray,
    k: tuple,
) -> tuple[str, dict]:
    """Class of one box against the projected caps, and the projected caps (u, v, r) [px]."""
    projected = {}
    for button, (centre, radius) in buttons.items():
        x, y, z = np.linalg.inv(pose)[:3] @ np.append(centre, 1.0)
        projected[button] = (k[0] * x / z + k[2], k[1] * y / z + k[3], k[0] * radius / z)
    if box is None:
        return "none", projected
    u, v = box.center.position.x, box.center.position.y
    on = [b for b, (cu, cv, r) in projected.items() if math.hypot(u - cu, v - cv) <= r]
    cu, cv, _ = projected[query_button]
    holds = abs(cu - u) <= box.size_x / 2 and abs(cv - v) <= box.size_y / 2

    return ("correct" if query_button in on else "wrong" if on else "plate" if holds else "other"), projected


def main() -> None:
    places_file, output = sys.argv[1:3]
    samples = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    frames = sys.argv[4] if len(sys.argv) > 4 else ""
    texts = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}
    with open(places_file) as file:
        places = yaml.safe_load(file)

    rclpy.init()
    node = rclpy.create_node("grounding_test")
    image_pub = node.create_publisher(CompressedImage, "/camera/camera/color/image_raw/compressed", 1)
    query_pub = node.create_publisher(String, "/keyboard_input", 1)
    answers = []
    node.create_subscription(Detection2DArray, "/init_bbox", lambda m: answers.append(("box", m)), 5)
    node.create_subscription(Log, "/rosout", lambda m: answers.append(("log", m.msg)) if m.name == "vlm_seed_bbox" else None, 50)

    env = ElevatorEnvironment()
    env.request_floor(0)  # the car to floor 1 (z 0), as the robot stands in it
    for _ in range(60):
        env.step(500)
        if env.state == "open":
            break
    address = env.model.jnt_qposadr[env.model.body_jntadr[env._pelvis_id]]
    rng = np.random.default_rng(0)
    results = []
    for place, queries in PANELS.items():
        queries = [(q, b) for q, b in queries if q in texts] if texts else queries
        goal = places["car"][place] if place == "panel" else places["floors"]["1"][place]
        for sample in range(samples):
            along, across, turn = rng.uniform(-0.1, 0.1), rng.uniform(-0.1, 0.1), math.radians(rng.uniform(-10.0, 10.0))
            if not queries:  # the same poses as a full run
                continue
            c, s = math.cos(goal["yaw"]), math.sin(goal["yaw"])
            yaw = goal["yaw"] + turn
            env.data.qpos[address:address + 2] = goal["x"] + c * along - s * across, goal["y"] + s * along + c * across
            env.data.qpos[address + 3:address + 7] = math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)
            mujoco.mj_forward(env.model, env.data)
            jpeg, pose, k = frame(env)
            buttons = caps(env)
            for query, button in queries:
                message = CompressedImage(format="jpeg", data=jpeg)
                message.header.stamp, message.header.frame_id = node.get_clock().now().to_msg(), "camera_color_optical_frame"
                # seed_bbox grounds the newest frame it has: stream this one for 1 s first, as a camera does (a single
                # publish before the query was not always there yet, and the previous frame was grounded)
                for _ in range(10):
                    image_pub.publish(message)
                    rclpy.spin_once(node, timeout_sec=0.1)
                answers.clear()
                start = time.monotonic()
                query_pub.publish(String(data=texts.get(query, query)))
                box, note = None, "timeout"
                while time.monotonic() - start < 20.0:
                    image_pub.publish(message)
                    rclpy.spin_once(node, timeout_sec=0.1)
                    boxes = [m for kind, m in answers if kind == "box"]
                    logs = [m for kind, m in answers if kind == "log" and f"'{texts.get(query, query)}'" in m]
                    if boxes:
                        same = boxes[0].header.stamp == message.header.stamp  # the header of the grounded frame
                        box, note = (boxes[0].detections[0].bbox if boxes[0].detections else None), "box" if same else "box of another frame"
                        break
                    if any("no accepted box" in m or "failed" in m or "ignoring" in m for m in logs):
                        note = logs[-1]
                        break
                kind, projected = score(box, button, buttons, pose, k)
                if frames:  # the frame with the projected caps (green) and the box (yellow)
                    picture = PilImage.open(io.BytesIO(jpeg)).convert("RGB")
                    draw = ImageDraw.Draw(picture)
                    for name, (u, v, r) in [(name, p) for name, p in projected.items() if p[2] > 0]:  # in front of the camera
                        draw.ellipse((u - r, v - r, u + r, v + r), outline=(0, 255, 0), width=2)
                        draw.text((u + r + 2, v - 6), name, fill=(0, 255, 0))
                    if box is not None:
                        u, v, w, h = box.center.position.x, box.center.position.y, box.size_x, box.size_y
                        draw.rectangle((u - w / 2, v - h / 2, u + w / 2, v + h / 2), outline=(255, 235, 60), width=2)
                    draw.text((8, 8), f"query {query}: {kind}", fill=(255, 235, 60))
                    picture.save(f"{frames}/{place}_{sample:02d}_{query}_{kind}.png")
                result = {"place": place, "query": query, "sample": sample, "along": round(along, 3), "across": round(across, 3),
                          "turn_deg": round(math.degrees(turn), 1), "class": kind, "seconds": round(time.monotonic() - start, 2),
                          "note": note, "caps_px": {b: [round(v, 1) for v in p] for b, p in projected.items()},
                          "box": None if box is None else [round(box.center.position.x, 1), round(box.center.position.y, 1),
                                                           round(box.size_x, 1), round(box.size_y, 1)]}
                results.append(result)
                print(json.dumps({key: result[key] for key in ("place", "query", "sample", "class", "seconds", "box")}), flush=True)

    with open(output, "w") as file:
        json.dump(results, file, indent=1)
    for query in ("up", "down", "floor_1", "floor_2"):
        kinds = [r["class"] for r in results if r["query"] == query]
        print(query, {kind: kinds.count(kind) for kind in ("correct", "plate", "wrong", "other", "none")}, f"of {len(kinds)}",
              "boxes of another frame:", sum(r["note"] == "box of another frame" for r in results if r["query"] == query))
    env.close()
    node.destroy_node()
    rclpy.try_shutdown()


if __name__ == "__main__":
    main()
