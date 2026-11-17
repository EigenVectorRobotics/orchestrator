"""Render one top-view PNG per floor of the prior map, and floors.json with their bounds and the places, for the map pane."""

import argparse
import json
from pathlib import Path

import numpy as np
import yaml
from PIL import Image


OUT        = Path(__file__).resolve().parents[1] / 'src' / 'run_viewer' / 'static' / 'maps'
FLOOR_BAND = (-0.45, 0.30)              # height above the floor ground of floor points; the floor is not flat
WALL_BAND  = (1.30, 2.20)               # height above the floor ground of wall points: tall structure only
LOW_BAND   = (0.25, 1.30)               # furniture and other low objects, drawn lighter than walls
MIN_HITS   = 3                          # points in a cell to call it a wall
MARGIN_M   = 4.0
KEEP_PCT   = (0.5, 99.5)                # floor points that set the bounds; the rest are stray points far out
CLOSE_STEPS = 4                         # pixels to close the holes of the floor area, which the sparse scan leaves
WALL_REACH  = 6                         # pixels from the floor area a wall may be; farther is clutter seen through glass
FLOOR_RGBA = (255, 255, 255, 255)       # a floor plan: white floor, dark walls, the outside left to the page
WALL_RGBA  = (61, 69, 64, 255)
LOW_RGBA   = (190, 197, 191, 255)


def parse_args() -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pcd', required=True, help='prior map, binary x y z intensity, e.g. two_floor_clean_cabin2.pcd')
    parser.add_argument('--places', required=True, help='the places file of that map, e.g. two_floor_clean_cabin2.places.yaml')
    parser.add_argument('--floors', default='1:-7.1,2:-0.93', help='floor:ground z, as /slam_elevatorlio/floor numbers them')
    parser.add_argument('--res', type=float, default=0.05, help='metres per pixel')
    parser.add_argument('--out', default=str(OUT))
    return parser.parse_args()


def read_pcd(
    path: str,
) -> np.ndarray:
    """Binary PCD with float32 fields as (N, fields); x, y, z come first."""
    with open(path, 'rb') as file:
        header = b''
        while not header.endswith(b'DATA binary\n'):
            header += file.readline()
        count = int(header.split(b'POINTS ')[1].split()[0])
        fields = len(header.split(b'FIELDS ')[1].split(b'\n')[0].split())
        return np.frombuffer(file.read(4 * fields * count), dtype=np.float32).reshape(count, fields)


def cell_counts(
    xy: np.ndarray,
    x0: float,
    y1: float,
    res: float,
    shape: tuple[int, int],
) -> np.ndarray:
    """Points per pixel; row 0 is the top (largest y)."""
    cols = ((xy[:, 0] - x0) / res).astype(int)
    rows = ((y1 - xy[:, 1]) / res).astype(int)
    inside = (cols >= 0) & (cols < shape[1]) & (rows >= 0) & (rows < shape[0])
    counts = np.zeros(shape, dtype=np.int32)
    np.add.at(counts, (rows[inside], cols[inside]), 1)
    return counts


def grow(
    mask: np.ndarray,
    steps: int,
    value: bool,
) -> np.ndarray:
    """Dilate (value True) or erode (value False) a mask by one pixel per step, 8-connected."""
    out = mask.copy()
    for _ in range(steps):
        padded = np.pad(out, 1, constant_values=not value)
        near = [padded[1 + dy:padded.shape[0] - 1 + dy, 1 + dx:padded.shape[1] - 1 + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
        out = np.logical_or.reduce(near) if value else np.logical_and.reduce(near)
    return out


def neighbours(
    mask: np.ndarray,
) -> np.ndarray:
    """Number of set pixels among the 8 neighbours of each pixel."""
    padded = np.pad(mask.astype(np.int8), 1)
    return sum(padded[1 + dy:padded.shape[0] - 1 + dy, 1 + dx:padded.shape[1] - 1 + dx]
               for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx)


def render_floor(
    points: np.ndarray,
    ground: float,
    res: float,
) -> tuple[Image.Image, dict]:
    """Floor area light, walls dark, transparent elsewhere; and the bounds in map metres."""
    height = points[:, 2] - ground
    floor = points[(height > FLOOR_BAND[0]) & (height < FLOOR_BAND[1]), :2]
    walls = points[(height > WALL_BAND[0]) & (height < WALL_BAND[1]), :2]
    lows = points[(height > LOW_BAND[0]) & (height < LOW_BAND[1]), :2]
    x0, y0 = np.percentile(floor, KEEP_PCT[0], axis=0) - MARGIN_M
    x1, y1 = np.percentile(floor, KEEP_PCT[1], axis=0) + MARGIN_M
    shape = (int(np.ceil((y1 - y0) / res)), int(np.ceil((x1 - x0) / res)))
    rgba = np.zeros(shape + (4,), dtype=np.uint8)
    area = grow(grow(cell_counts(floor, x0, y1, res, shape) > 0, CLOSE_STEPS, True), CLOSE_STEPS, False)
    near = grow(area, WALL_REACH, True)
    wall = (cell_counts(walls, x0, y1, res, shape) >= MIN_HITS) & near
    wall &= neighbours(wall) >= 2                                     # drop lone pixels
    low = (cell_counts(lows, x0, y1, res, shape) >= MIN_HITS) & near
    low &= neighbours(low) >= 2
    rgba[area] = FLOOR_RGBA
    rgba[low] = LOW_RGBA
    rgba[wall] = WALL_RGBA
    bounds = {'x0': round(float(x0), 3), 'y0': round(float(y0), 3), 'res': res, 'w': shape[1], 'h': shape[0], 'ground_z': ground}
    return Image.fromarray(rgba, 'RGBA'), bounds


def main() -> None:
    """Write floor_<n>.png and floors.json."""
    args = parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    points = read_pcd(args.pcd)
    grounds = {name: float(z) for name, z in (item.split(':') for item in args.floors.split(','))}
    floors = {}
    for name, ground in grounds.items():
        image, bounds = render_floor(points, ground, args.res)
        image.save(out / f'floor_{name}.png', optimize=True)
        floors[name] = {'file': f'floor_{name}.png', **bounds}
        print(f'floor {name}: {bounds["w"]}x{bounds["h"]} px')
    places = []
    for place, pose in (yaml.safe_load(Path(args.places).read_text()).get('places') or {}).items():
        floor = min(grounds, key=lambda name: abs(pose['z'] - grounds[name] - 0.5))
        places.append({'name': place, 'x': pose['x'], 'y': pose['y'], 'yaw_deg': pose.get('yaw_deg', 0.0), 'floor': floor})
    (out / 'floors.json').write_text(json.dumps({'map': Path(args.pcd).name, 'floors': floors, 'places': places}, indent=1))
    print(f'{len(places)} places -> {out / "floors.json"}')


if __name__ == '__main__':
    main()
