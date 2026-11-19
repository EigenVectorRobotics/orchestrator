"""Print the map pane's floor maps of a places file as one JSON line: a top-view PNG per floor of the map it names, the bounds and the places.

The Run Viewer runs it on the robot host as python3 -c, once per places file (sources.py, Sessions.maps); the host needs numpy and PyYAML, not Pillow.
"""

import argparse
import base64
import json
import struct
import zlib
from pathlib import Path

import numpy as np
import yaml


GROUND_BIN_M = 0.02                     # the ground of a floor is the most filled z bin of this size in the floor's z range
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
    parser.add_argument('places', help='a places file: its map key names the prior map (binary x y z intensity .pcd), its floors table the z range of each floor')
    parser.add_argument('--res', type=float, default=0.05, help='metres per pixel')
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


def ground_z(
    z: np.ndarray,
    z_min: float,
    z_max: float,
) -> float:
    """The ground of a floor: the centre of the most filled z bin between z_min and z_max."""
    counts, edges = np.histogram(z, bins=np.arange(z_min, z_max + GROUND_BIN_M, GROUND_BIN_M))
    return round(float(edges[np.argmax(counts)] + GROUND_BIN_M / 2), 2)


def png_chunk(
    kind: bytes,
    data: bytes,
) -> bytes:
    """One PNG chunk: length, type, data and CRC."""
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png(
    rgba: np.ndarray,
) -> bytes:
    """RGBA pixels (rows from the top) as PNG bytes, with the standard library."""
    header = struct.pack('>IIBBBBB', rgba.shape[1], rgba.shape[0], 8, 6, 0, 0, 0)
    pixels = zlib.compress(b''.join(b'\x00' + row.tobytes() for row in rgba), 9)
    return b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', header) + png_chunk(b'IDAT', pixels) + png_chunk(b'IEND', b'')


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
) -> tuple[np.ndarray, dict]:
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
    return rgba, bounds


def main() -> None:
    """Print {map, floors: {n: bounds and png}, places} as one JSON line."""
    args = parse_args()
    places_file = yaml.safe_load(Path(args.places).read_text())
    points = read_pcd(places_file['map'])
    grounds = {key.split()[-1]: ground_z(points[:, 2], band['z_min'], band['z_max']) for key, band in places_file['floors'].items()}   # "floor 1" is floor 1 of /slam_elevatorlio/floor
    floors = {}
    for name, ground in grounds.items():
        rgba, bounds = render_floor(points, ground, args.res)
        floors[name] = {**bounds, 'png': base64.b64encode(png(rgba)).decode()}
    places = []
    for place, pose in (places_file.get('places') or {}).items():
        floor = min(grounds, key=lambda name: abs(pose['z'] - grounds[name] - 0.5))
        places.append({'name': place, 'x': pose['x'], 'y': pose['y'], 'yaw_deg': pose.get('yaw_deg', 0.0), 'floor': floor})
    print(json.dumps({'map': Path(places_file['map']).name, 'floors': floors, 'places': places}))


if __name__ == '__main__':
    main()
