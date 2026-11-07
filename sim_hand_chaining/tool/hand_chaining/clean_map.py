"""Remove the places file's map_masks (dynamic objects such as elevator doors) from a prior map PCD.

python3 clean_map.py <map.pcd> <places.yaml> <out.pcd>
"""
import sys

import numpy as np
import yaml

from slam_elevatorlio.map_io import read_pcd_xyz, write_pcd


def remove_boxes(
    points: np.ndarray,
    boxes: list,
) -> np.ndarray:
    """Points outside every [x_min, y_min, z_min, x_max, y_max, z_max] box."""
    keep = np.ones(len(points), dtype=bool)
    for box in boxes:
        low, high = np.array(box[:3]), np.array(box[3:])
        keep &= ~np.all((points[:, :3] >= low) & (points[:, :3] <= high), axis=1)

    return points[keep]


def main() -> None:
    """Read, mask, write."""
    source, places, output = sys.argv[1:4]
    points = read_pcd_xyz(source)
    with open(places) as file:
        boxes = yaml.safe_load(file)["map_masks"]
    kept = remove_boxes(points, boxes)
    write_pcd(output, np.hstack([kept, np.zeros((len(kept), 1))]).astype(np.float32))
    print(f"{len(points) - len(kept)} of {len(points)} points removed, wrote {output}")


if __name__ == "__main__":
    main()
