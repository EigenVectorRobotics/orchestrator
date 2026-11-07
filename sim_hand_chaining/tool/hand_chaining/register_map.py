"""Put a LIO map of the sim into the sim world frame: GICP of the map to points sampled on the scene's box geoms.

python3 register_map.py <map.pcd> <out.pcd>

<map>_frame.yaml (from mapping_tour.py) gives the first guess: the sensor pose when LIO started. The robot turns a
few degrees while it stands before that, so the guess alone leaves the map ~3 deg off. Writes <out>_registration.yaml.
"""
import math
import sys

import mujoco
import numpy as np
import small_gicp
import yaml

from elevator_sim import ElevatorEnvironment
from slam_elevatorlio.map_io import read_pcd_xyz, write_pcd


def scene_points(
    spacing: float = 0.05,
) -> np.ndarray:
    """Points on the faces of the scene's box geoms (not the robot), every spacing [m]."""
    env = ElevatorEnvironment(robot_motion="kinematic")
    model, data = env.model, env.data
    faces = []
    for geom in range(model.ngeom):
        body = int(model.geom_bodyid[geom])
        while body and body != env._pelvis_id:
            body = int(model.body_parentid[body])
        if body == env._pelvis_id or model.geom_type[geom] != mujoco.mjtGeom.mjGEOM_BOX or model.geom_group[geom] not in (0, 2, 3, 4):
            continue
        size, rotation, centre = model.geom_size[geom], data.geom_xmat[geom].reshape(3, 3), data.geom_xpos[geom]
        for axis in range(3):
            u, v = [index for index in range(3) if index != axis]
            grid_u, grid_v = np.meshgrid(np.linspace(-size[u], size[u], max(2, int(2 * size[u] / spacing))),
                                         np.linspace(-size[v], size[v], max(2, int(2 * size[v] / spacing))))
            for sign in (-1.0, 1.0):
                local = np.zeros((grid_u.size, 3))
                local[:, u], local[:, v], local[:, axis] = grid_u.ravel(), grid_v.ravel(), sign * size[axis]
                faces.append(local @ rotation.T + centre)
    env.close()

    return np.concatenate(faces)


def main() -> None:
    """Guess from the frame file, coarse to fine GICP, write the moved map."""
    source_path, output = sys.argv[1:3]
    with open(source_path.removesuffix(".pcd") + "_frame.yaml") as file:
        frame = yaml.safe_load(file)["world_from_map"]
    guess = np.eye(4)
    guess[:2, :2] = [[math.cos(frame["yaw"]), -math.sin(frame["yaw"])], [math.sin(frame["yaw"]), math.cos(frame["yaw"])]]
    guess[:3, 3] = frame["x"], frame["y"], frame["z"]

    points = read_pcd_xyz(source_path)
    target, tree = small_gicp.preprocess_points(scene_points(), downsampling_resolution=0.1, num_neighbors=10)
    source, _ = small_gicp.preprocess_points(points.astype(np.float64), downsampling_resolution=0.1, num_neighbors=10)
    transform = guess
    for distance in (2.0, 1.0, 0.5, 0.2):
        transform = small_gicp.align(target, source, tree, init_T_target_source=transform, registration_type="GICP",
                                     max_correspondence_distance=distance).T_target_source

    moved = points @ transform[:3, :3].T + transform[:3, 3]
    write_pcd(output, np.hstack([moved, np.zeros((len(moved), 1))]).astype(np.float32))
    _, squared = tree.batch_nearest_neighbor_search(moved.astype(np.float64))
    distances = np.sqrt(np.asarray(squared))
    with open(output.removesuffix(".pcd") + "_registration.yaml", "w") as file:
        yaml.safe_dump({"world_from_map": transform.tolist(), "source": source_path,
                        "yaw_correction_deg": float(math.degrees(math.atan2(transform[1, 0], transform[0, 0]) - frame["yaw"])),
                        "distance_to_scene_m": {"median": float(np.median(distances)),
                                                "p90": float(np.percentile(distances, 90))}}, file)
    print(f"wrote {output}: median distance to the scene {np.median(distances):.3f} m")


if __name__ == "__main__":
    main()
