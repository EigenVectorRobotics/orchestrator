"""Pictures of the sim prior map: a top view of each floor with the annotated places, and a 3D view of each floor.

python3 map_views.py <map.pcd> <places.yaml> <output_dir>
"""
import math
import os
import sys

import matplotlib
import numpy as np
import yaml

from PIL import Image, ImageDraw, ImageFont

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def top_view(
    points: np.ndarray,
    places: dict,
    floor: str,
    path: str,
) -> None:
    """Floor points (light) and walls 0.3-2.0 m above the floor (dark, colour = height); places as arrows."""
    ground = places["floors"][floor]["ground_z"]
    height = points[:, 2] - ground
    figure, axis = plt.subplots(figsize=(11, 10))
    flat = np.abs(height) < 0.15
    axis.scatter(points[flat, 0], points[flat, 1], s=0.2, c="0.85", rasterized=True)
    walls = (height > 0.3) & (height < 2.0)
    shown = axis.scatter(points[walls, 0], points[walls, 1], s=0.4, c=height[walls], cmap="viridis", rasterized=True)
    figure.colorbar(shown, ax=axis, shrink=0.6, label="height above the floor [m]")

    poses = {name: pose for name, pose in places["floors"][floor].items() if isinstance(pose, dict) and "yaw" in pose}
    poses.update({f"car {name}": pose for name, pose in places["car"].items() if isinstance(pose, dict)})
    poses.update(places["floors"][floor]["targets"])
    for name, pose in poses.items():
        axis.arrow(pose["x"], pose["y"], 0.3 * math.cos(pose["yaw"]), 0.3 * math.sin(pose["yaw"]), head_width=0.12,
                   color="tab:red", zorder=5)
        axis.text(pose["x"] + 0.08, pose["y"] + 0.15, name, fontsize=8, color="tab:red", zorder=5)
    corners = np.array(places["car"]["corners"] + places["car"]["corners"][:1])
    axis.plot(corners[:, 0], corners[:, 1], "--", c="tab:orange", lw=1, label="car floor (places file)")
    for x0, y0, z0, x1, y1, z1 in places["map_masks"]:
        if z0 < ground + 2.0 and z1 > ground:
            axis.add_patch(plt.Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, ec="tab:purple", lw=1.5,
                                         label="removed door leaves"))
    axis.set_title(f"sim prior map, floor {floor} (floor at z = {ground} m): {flat.sum()} floor, {walls.sum()} wall points")
    axis.set_xlabel("x [m]")
    axis.set_ylabel("y [m]")
    axis.set_aspect("equal")
    axis.grid(True, alpha=0.3)
    axis.legend(loc="lower left", fontsize=8)
    figure.tight_layout()
    figure.savefig(path, dpi=110)
    plt.close(figure)


def perspective(
    points: np.ndarray,
    ground: float,
    title: str,
    eye: tuple = (-9.0, -14.0, 11.0),
    target: tuple = (0.0, -2.5, 0.0),
    size: tuple = (1000, 760),
) -> Image.Image:
    """One floor from above and in front, the two near walls cut away: points splatted far to near, colour = height."""
    eye, target = np.asarray(eye), np.asarray(target) + [0.0, 0.0, ground]
    eye = eye + [0.0, 0.0, ground]
    keep = (points[:, 0] > -5.7) & (points[:, 1] > -8.2)
    points = points[keep]
    forward = (target - eye) / np.linalg.norm(target - eye)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    local = (points - eye) @ np.stack([right, up, forward], 1)
    front = local[:, 2] > 0.5
    local, height = local[front], points[front, 2] - ground
    focal = 0.9 * size[0]
    u = (size[0] / 2 + focal * local[:, 0] / local[:, 2]).astype(int)
    v = (size[1] / 2 - focal * local[:, 1] / local[:, 2]).astype(int)
    inside = (u >= 0) & (u < size[0] - 1) & (v >= 0) & (v < size[1] - 1)
    order = np.argsort(-local[inside, 2])
    u, v, height = u[inside][order], v[inside][order], height[inside][order]
    colours = (plt.get_cmap("turbo")(np.clip(height / 2.6, 0, 1))[:, :3] * 255).astype(np.uint8)
    image = np.full((size[1], size[0], 3), 250, dtype=np.uint8)
    for du, dv in ((0, 0), (1, 0), (0, 1), (1, 1)):
        image[v + dv, u + du] = colours

    picture = Image.fromarray(image)
    ImageDraw.Draw(picture).text((16, 12), title, font=ImageFont.load_default(size=22), fill=(20, 20, 20))

    return picture


def main() -> None:
    from slam_elevatorlio.map_io import read_pcd_xyz
    map_path, places_path, output = sys.argv[1:4]
    os.makedirs(output, exist_ok=True)
    with open(places_path) as file:
        places = yaml.safe_load(file)
    points = read_pcd_xyz(map_path)
    views = []
    for floor, info in places["floors"].items():
        top_view(points, places, floor, os.path.join(output, f"map_floor{floor}.png"))
        band = (points[:, 2] > info["ground_z"] - 0.3) & (points[:, 2] < info["ground_z"] + 3.5)
        views.append(perspective(points[band], info["ground_z"], f"floor {floor}: colour = height above the floor, 0 (blue) to 2.6 m (red)"))
    combined = Image.new("RGB", (sum(view.width for view in views), views[0].height), "white")
    for index, view in enumerate(views):
        combined.paste(view, (index * views[0].width, 0))
    ImageDraw.Draw(combined).text((16, views[0].height - 34), f"sim prior map, {len(points)} points; the near (front, left) walls are cut away",
                                  font=ImageFont.load_default(size=20), fill=(60, 60, 60))
    combined.save(os.path.join(output, "map_3d.png"))
    print(f"{len(points)} points; pictures in {output}")


if __name__ == "__main__":
    main()
