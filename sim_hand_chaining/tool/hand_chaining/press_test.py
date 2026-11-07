"""Success rate of the sim press (G1Pilot plan, MuJoCo arm, closed Dex1-1 gripper) with the button pose from the sim.

The robot is put where the navigation stack ends at a press pose: the places pose plus its mean arrival error, +-4 cm
along and across and +-4 deg (the straight last leg: nav_approach_test.py, analysis/fvd_sim_06/
nav_approach_test_last_leg_crab.json, and runs 42-44), stands 1.5 s,
then presses the button with PressController and
withdraws along its path (as the arm does when it is released at the press pose). Button calls are off, so the car
stays. Per press: the button in its press region of the places file (as task_executor.py reach checks it), planned, the
target button pressed (cap > 2 mm in), any other button pressed, result code.

python3 press_test.py <places.yaml> <results.json> [presses per button] [buttons, e.g. hall_up,hall_down]   (vector-pc, vector_pc.env, no ROS)
"""
import json
import math
import sys

import mujoco
import numpy as np
import yaml

from elevator_sim import ElevatorEnvironment
from elevator_sim.agent.errors import SkillPreconditionError
from elevator_sim.agent.manipulation import G1Planner, PressController

TESTS = (("call_panel", "hall_up"), ("call_panel", "hall_down"), ("panel", "floor_2"), ("panel", "floor_1"))
PRESSED = 0.002  # m of cap travel at which the sim registers a press
# Mean arrival error of the navigation stack's last leg at each press pose: past it [m], to its left [m], heading [deg]
NAV_BIAS = {"call_panel": (0.025, 0.065, -1.8), "panel": (0.02, 0.05, -2.0)}
SPREAD = (0.04, 0.04, 4.0)  # +- along [m], across [m], heading [deg]


def put_robot(
    env: ElevatorEnvironment,
    x: float,
    y: float,
    yaw: float,
) -> None:
    """Put the standing robot's pelvis at x, y, yaw with no motion."""
    address = env.model.jnt_qposadr[env.model.body_jntadr[env._pelvis_id]]
    env.data.qpos[address:address + 2] = x, y
    env.data.qpos[address + 3:address + 7] = math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)
    env.data.qvel[:] = 0.0
    mujoco.mj_forward(env.model, env.data)


def main() -> None:
    places_file, output = sys.argv[1:3]
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    tests = [test for test in TESTS if len(sys.argv) < 5 or test[1] in sys.argv[4].split(",")]
    with open(places_file) as file:
        places = yaml.safe_load(file)

    env = ElevatorEnvironment(robot_motion="unitree_rl")
    env.request_floor(0)  # the car to floor 1 with its door open, then no more calls
    for _ in range(60):
        env.step(500)
        if env.state == "open":
            break
    env.enable_button_calls = False
    caps = {button: env.model.jnt_qposadr[mujoco.mj_name2id(env.model, mujoco.mjtObj.mjOBJ_JOINT, env.button_info(button)["joint"])]
            for button in ("hall_up", "hall_down", "floor_1", "floor_2")}
    rng = np.random.default_rng(0)
    results = []
    for place, button in tests:
        goal = places["car"][place] if place == "panel" else places["floors"]["1"][place]
        for sample in range(count):
            bias = NAV_BIAS[place]
            along, across = bias[0] + rng.uniform(-SPREAD[0], SPREAD[0]), bias[1] + rng.uniform(-SPREAD[1], SPREAD[1])
            turn = math.radians(bias[2] + rng.uniform(-SPREAD[2], SPREAD[2]))
            c, s = math.cos(goal["yaw"]), math.sin(goal["yaw"])
            put_robot(env, goal["x"] + c * along - s * across, goal["y"] + s * along + c * across, goal["yaw"] + turn)
            env.stop_robot()
            env.step(int(1.5 / env.model.opt.timestep))
            info = env.button_info(button)
            rotation = env.data.xmat[env._pelvis_id].reshape(3, 3)
            point = rotation.T @ (np.asarray(info["position"]) - env.data.xpos[env._pelvis_id])
            tracker = {"status": "tracking", "position_base": point, "surface_normal_base": rotation.T @ np.asarray(info["normal"]),
                       "observed_at": float(env.data.time)}
            annotated = places["buttons"][button]
            region = annotated.get("region", places["press_region"])
            x, y, yaw = env.data.xpos[env._pelvis_id][0], env.data.xpos[env._pelvis_id][1], math.atan2(rotation[1, 0], rotation[0, 0])
            dx, dy, facing = annotated["x"] - x, annotated["y"] - y, annotated["normal_yaw"] + math.pi
            distance = -(math.cos(annotated["normal_yaw"]) * dx + math.sin(annotated["normal_yaw"]) * dy)
            lateral = math.sin(facing) * dx - math.cos(facing) * dy
            panel_turn = math.degrees(math.atan2(math.sin(facing - yaw), math.cos(facing - yaw)))
            inside = bool(region["distance"][0] <= distance <= region["distance"][1] and region["lateral"][0] <= lateral <= region["lateral"][1]
                          and region["turn_deg"][0] <= panel_turn <= region["turn_deg"][1])
            result = {"place": place, "button": button, "sample": sample, "along": round(along, 3), "across": round(across, 3),
                      "turn_deg": round(math.degrees(turn), 1), "panel": [round(distance, 3), round(lateral, 3), round(panel_turn, 1)],
                      "in_region": inside}
            try:
                press = PressController(env, point, tracker, planner=G1Planner())
            except SkillPreconditionError as error:
                result.update(planned=False, code=getattr(error, "code", "refused"))
                results.append(result)
                print(json.dumps(result), flush=True)
                continue
            travel = {name: 0.0 for name in caps}
            while not press.done:
                env.step(10)
                for name, address in caps.items():
                    travel[name] = max(travel[name], float(env.data.qpos[address]))
            success, code, details = press.result()
            press.close()
            others = [name for name in caps if name != button and travel[name] > PRESSED]
            result.update(planned=True, pressed=travel[button] > PRESSED, others=others, code=code,
                          tip_m=round(float(details["final_distance"]), 4), travel_mm=round(1000 * travel[button], 1))
            results.append(result)
            print(json.dumps(result), flush=True)

    with open(output, "w") as file:
        json.dump(results, file, indent=1)
    for place, button in tests:
        rows = [r for r in results if r["button"] == button]
        inside = [r for r in rows if r["in_region"]]
        print(button, f"planned {sum(r['planned'] for r in rows)}/{len(rows)}, pressed {sum(r.get('pressed', False) for r in rows)},",
              f"another button pressed {sum(bool(r.get('others')) for r in rows)};",
              f"in the region {len(inside)}, of them pressed {sum(r.get('pressed', False) for r in inside)}")
    env.close()


if __name__ == "__main__":
    main()
