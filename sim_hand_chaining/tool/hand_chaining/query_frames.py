"""For each press query in a run bag: the frame at the query (what seed_bbox sent to Cosmos), with the Cosmos box (yellow)
and the EdgeTAM mask (green, the 4th segmented cloud after the query) drawn; prints the times and boxes.

python3 query_frames.py <bag.mcap> <output dir>
"""
import io, sys, os
import numpy as np
from PIL import Image, ImageDraw
from mcap_ros2.reader import read_ros2_messages
bag, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
topics = ["/keyboard_input", "/init_bbox", "/camera/camera/color/image_raw/compressed", "/track_3d/selected_target_pointcloud", "/camera/camera/color/camera_info", "/task_executor/status"]
data = {t: [] for t in topics}
for m in read_ros2_messages(bag, topics=topics):
    data[m.channel.topic].append((m.log_time_ns * 1e-9, m.ros_msg))
t0 = next(t for t, s in data["/task_executor/status"] if '"running"' in s.data)
k = data["/camera/camera/color/camera_info"][0][1].k
for i, (tq, q) in enumerate(data["/keyboard_input"]):
    frame = max((f for f in data["/camera/camera/color/image_raw/compressed"] if f[0] <= tq + 0.05), key=lambda f: f[0])
    image = Image.open(io.BytesIO(bytes(frame[1].data))).convert("RGB")
    draw = ImageDraw.Draw(image)
    seeds = [(t, s) for t, s in data["/init_bbox"] if tq < t < tq + 10]
    for t, s in seeds[:1]:
        for d in s.detections:
            b = d.bbox
            draw.rectangle((b.center.position.x - b.size_x / 2, b.center.position.y - b.size_y / 2, b.center.position.x + b.size_x / 2, b.center.position.y + b.size_y / 2), outline=(255, 235, 60), width=2)
            print(f"  seed box at +{t - tq:.1f} s: centre ({b.center.position.x:.0f}, {b.center.position.y:.0f}) size {b.size_x:.0f} x {b.size_y:.0f} px")
    clouds = [(t, c) for t, c in data["/track_3d/selected_target_pointcloud"] if tq < t < tq + 15]
    if clouds:
        c = clouds[min(3, len(clouds) - 1)][1]
        off = {f.name: f.offset for f in c.fields}
        rows = np.frombuffer(bytes(c.data), np.uint8).reshape(-1, c.point_step)
        xyz = np.stack([rows[:, off[n]:off[n] + 4].copy().view(np.float32)[:, 0] for n in "xyz"], 1)
        u, v = (k[0] * xyz[:, 0] / xyz[:, 2] + k[2]).astype(int), (k[4] * xyz[:, 1] / xyz[:, 2] + k[5]).astype(int)
        px = np.asarray(image).copy(); ok = (u >= 0) & (u < 640) & (v >= 0) & (v < 480)
        px[v[ok], u[ok]] = 0.45 * px[v[ok], u[ok]] + 0.55 * np.array([0, 255, 150])
        image = Image.fromarray(px)
        print(f"  mask {len(xyz)} points, depth {np.median(xyz[:, 2]):.3f} m")
    name = f"{out}/query{i + 1}_{q.data}_t{tq - t0:.0f}.png"
    image.save(name)
    print(f"query {i + 1} '{q.data}' at t = {tq - t0:.1f} s, frame {tq - frame[0]:.2f} s before, {len(seeds)} seed(s) -> {name}")
