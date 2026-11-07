import numpy as np


def count_points_in_rectangle(
    points: np.ndarray,
    corners: np.ndarray,
    min_intensity: float = 5.0,
) -> int:
    """Count points (x, y, z, intensity) with intensity >= min_intensity whose (x, y) lies in the rectangle given by its 4 corners in order around it."""
    xy = points[points[:, 3] >= min_intensity, :2]
    origin = corners[0]
    side_a = corners[1] - origin
    side_b = corners[3] - origin
    a = (xy - origin) @ side_a
    b = (xy - origin) @ side_b

    return int(((a >= 0) & (a <= side_a @ side_a) & (b >= 0) & (b <= side_b @ side_b)).sum())
