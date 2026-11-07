"""Where to stand to press a button: sweep base poses in front of each sim button, plan the press with G1Pilot.

python3 press_reach_sweep.py <output_dir> [--quick] [--buttons hall_up,hall_down] [--grid d0:d1:step,l0:l1:step,h1:h2:...]

For each base pose (distance from the panel along its normal, lateral offset of the button to the robot's right, heading
error) it reports: the button in the head camera view, the planner result (IK, path, collision checks of the sim
adapter), the pelvis-frame target, and the residual: the TCP at the end of the plan (MuJoCo FK) to the goal (the
adapter accepts up to 25 mm, and a press then misses). Writes press_reach.csv and one heat map per button to <output_dir>/images.
"""
import csv
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from itertools import product

import mujoco
import numpy as np

BUTTONS = ("hall_up", "hall_down", "floor_1", "floor_2")
DISTANCES = np.round(np.arange(0.30, 0.66, 0.05), 2)
LATERALS = np.round(np.arange(-0.10, 0.31, 0.05), 2)
HEADINGS = (-15.0, 0.0, 15.0)
IMAGE = (640, 360)
MARGIN = 20


def span(
    text: str,
) -> np.ndarray:
    """Values of 'start:stop:step' (stop excluded), rounded to mm."""
    return np.round(np.arange(*[float(v) for v in text.split(":")]) + 1e-9, 3)


def place_robot(
    env,
    x: float,
    y: float,
    yaw: float,
) -> None:
    """Put the kinematic base at (x, y, yaw) on its floor and re-anchor its weld."""
    import mujoco

    address = env.model.jnt_qposadr[env.model.joint("floating_base_joint").id]
    env.data.qpos[address:address + 2] = (x, y)
    env.data.qpos[address + 3:address + 7] = (math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2))
    env.data.qvel[:] = 0.0
    mujoco.mj_forward(env.model, env.data)
    env._robot_motion._anchor_current_pose()
    env.step(100)


def stream_pixel(
    env,
    point: np.ndarray,
) -> tuple[float, float]:
    """The point in the streamed 640x480 view (the centre 4:3 crop of the 16:9 render) [px]; nan behind the camera."""
    optical = np.linalg.inv(env.camera_world_from_optical("g1_head"))
    local = optical[:3, :3] @ point + optical[:3, 3]
    if local[2] <= 0.05:
        return math.nan, math.nan
    k = env.camera_intrinsics("g1_head", width=IMAGE[0], height=IMAGE[1])
    u, v = k["fx"] * local[0] / local[2] + k["cx"], k["fy"] * local[1] / local[2] + k["cy"]
    crop = (IMAGE[0] - IMAGE[1] * 4 / 3) / 2
    scale = 480 / IMAGE[1]

    return (u - crop) * scale, v * scale


def in_view(
    env,
    point: np.ndarray,
) -> bool:
    """The point projects inside the streamed 640x480 view with a margin."""
    u, v = stream_pixel(env, point)

    return MARGIN <= u <= 640 - MARGIN and MARGIN <= v <= 480 - MARGIN


def evaluate(
    case: tuple,
) -> dict:
    """One base pose: view and plan check (no motion executed)."""
    os.environ.setdefault("MUJOCO_GL", "egl")
    from elevator_sim import ElevatorEnvironment
    from elevator_sim.agent.errors import SkillPreconditionError
    from elevator_sim.agent.manipulation import PressController

    button, distance, lateral, heading = case
    global ENV
    if "ENV" not in globals():
        ENV = ElevatorEnvironment(robot_motion="kinematic")
        if button.startswith("floor_"):
            ENV.reset()
    env = ENV
    env.reset()
    if button.startswith("floor_"):
        place_robot(env, 0.0, 1.5, -math.pi / 2)  # cabin buttons are in the car on floor 0
    info = env.button_info(button if button.startswith("floor_") else button)
    position, normal = np.asarray(info["position"]), np.asarray(info["normal"])
    facing = math.atan2(-normal[1], -normal[0])
    right = np.array([math.sin(facing), -math.cos(facing), 0.0])
    base = position + distance * normal - lateral * right
    yaw = facing + math.radians(heading)
    place_robot(env, base[0], base[1], yaw)

    rotation = env.data.xmat[env._pelvis_id].reshape(3, 3)
    pelvis = env.data.xpos[env._pelvis_id]
    point = rotation.T @ (position - pelvis)
    tracker = {"status": "tracking", "position_base": point, "surface_normal_base": rotation.T @ normal,
               "observed_at": float(env.data.time)}
    row = {"button": button, "distance": distance, "lateral": lateral, "heading": heading,
           "target_x": round(point[0], 3), "target_y": round(point[1], 3), "target_z": round(point[2], 3),
           "in_view": in_view(env, position), "pixel": [round(c) for c in stream_pixel(env, position)]}
    try:
        controller = PressController(env, point, tracker)
        scratch = mujoco.MjData(env.model)
        mujoco.mj_copyData(scratch, env.model, env.data)
        scratch.qpos[controller.qpos] = controller.trajectory[-1]
        mujoco.mj_kinematics(env.model, scratch)
        residual = np.linalg.norm(scratch.site_xpos[controller.site] - controller.goal_world)
        row.update(plan="ok", duration=round(controller.duration, 2), residual_mm=round(1000 * residual, 1))
        controller.close()
    except SkillPreconditionError as error:
        row.update(plan=error.code, duration="", residual_mm="")

    return row


def main() -> None:
    global BUTTONS, DISTANCES, LATERALS, HEADINGS
    output = sys.argv[1]
    quick = "--quick" in sys.argv
    if "--buttons" in sys.argv:
        BUTTONS = tuple(sys.argv[sys.argv.index("--buttons") + 1].split(","))
    if "--grid" in sys.argv:
        distances, laterals, headings = sys.argv[sys.argv.index("--grid") + 1].split(",")
        DISTANCES, LATERALS, HEADINGS = span(distances), span(laterals), tuple(float(v) for v in headings.split(":"))
    os.makedirs(os.path.join(output, "images"), exist_ok=True)
    cases = list(product(BUTTONS, DISTANCES, LATERALS, (0.0,) if quick else HEADINGS))

    with ProcessPoolExecutor(max_workers=20) as pool:
        rows = list(pool.map(evaluate, cases, chunksize=4))

    with open(os.path.join(output, "press_reach.csv"), "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    for heading in sorted({row["heading"] for row in rows}):
        fig, axes = plt.subplots(1, len(BUTTONS), figsize=(5 * len(BUTTONS), 4.6))
        for ax, button in zip(axes, BUTTONS):
            grid = np.full((len(DISTANCES), len(LATERALS)), np.nan)
            for row in rows:
                if row["button"] == button and row["heading"] == heading:
                    i, j = list(DISTANCES).index(row["distance"]), list(LATERALS).index(row["lateral"])
                    grid[i, j] = 2 * (row["plan"] == "ok") + row["in_view"]
            ax.imshow(grid, origin="lower", cmap="RdYlGn", vmin=0, vmax=3, aspect="auto",
                      extent=(LATERALS[0] - 0.025, LATERALS[-1] + 0.025, DISTANCES[0] - 0.025, DISTANCES[-1] + 0.025))
            ax.set_title(f"{button}, heading error {heading:+.0f} deg")
            ax.set_xlabel("button to the robot's right [m]")
            ax.set_ylabel("pelvis to panel along the normal [m]")
        fig.suptitle("green: plan ok + in view, yellow-green: plan ok only, orange: in view only, red: neither")
        fig.tight_layout()
        fig.savefig(os.path.join(output, "images", f"press_reach_heading_{heading:+.0f}.png"), dpi=80)

    for button in BUTTONS:
        good = [row for row in rows if row["button"] == button and row["plan"] == "ok" and row["in_view"]]
        print(f"{button}: {len(good)} of {sum(row['button'] == button for row in rows)} poses plan and see the button")


if __name__ == "__main__":
    main()
