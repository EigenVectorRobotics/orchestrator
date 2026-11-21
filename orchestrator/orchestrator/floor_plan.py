"""The floor plan of the Map panel: one image for each floor, from the route planner's own terrain graph.

The route planner keeps its terrain graph in <map>.terrain.npz beside the prior map (route_planner/terrain_graph.py):
a cell (x, y, surface z) every 0.2 m where the robot can stand, with a cost (1 = open floor, more near a wall or in
a keep-out zone) and blocked. That is how the planner sees the floor, so the image also shows why a route is blocked.
map_info gives the file of the places (Places.annotations_path); it names the map and the z range of each floor.

Two files of other packages are read here, and only here: the terrain cache (_make) and the places file
(read_floors). If their format changes, the panel says why it has no plan; a grid that route_planner publishes would
take this coupling away.

No ROS here: the node gives the places file; the page draws the robot, the places, the route and the goal on top.
"""

import threading

from pathlib import Path

import numpy as np
import yaml

from .images import _png


OPEN     = (236, 240, 230)              # cost 1: open floor
COSTLY   = (150, 132, 176)              # the most costly free cell (near a wall, in a keep-out zone)
BLOCKED  = (196, 84, 72)
COLORS   = {'open': OPEN, 'costly': COSTLY, 'blocked': BLOCKED}     # the page's legend: the same colours
MAX_COST_SHADE = 20.0                   # a cost from 1 to this goes from OPEN to COSTLY
MARGIN   = 2                            # cells around the floor's cells
BAND     = 0.8                          # m: the cells this near the floor's main surface; a floor's z range also holds
                                        # roofs, ramps and points seen through glass
BLOCK_M  = 2.0                          # m: the bounds are the blocks of this size with MIN_OPEN open cells or more, so
MIN_OPEN = 10                           # a few stray cells far out are dropped, and a 1 m corridor is kept
CROP_M   = 3.0                          # m of margin around those bounds


class FloorPlanError(Exception):
    pass


def read_floors(places_file: Path) -> tuple[Path, dict[str, tuple[float, float]]]:
    """The map and the z range of each floor, from map_info's places file (map: and floors: z_min, z_max). A map
    path with ~ is expanded, and a relative one is next to the places file"""
    data   = yaml.safe_load(places_file.read_text()) or {}
    floors = data.get('floors') or {} if isinstance(data, dict) else None

    if not isinstance(floors, dict) or not all(isinstance(floor, dict) for floor in floors.values()):
        raise FloorPlanError(f'{places_file}: the file and its floors are not mappings')

    if not data.get('map'):
        raise FloorPlanError(f'{places_file} names no map')

    return (places_file.parent / Path(str(data['map'])).expanduser(),
            {str(name): (float(floor['z_min']), float(floor['z_max']))
             for name, floor in floors.items() if 'z_min' in floor and 'z_max' in floor})


def surface(nodes: np.ndarray, cost: np.ndarray, blocked: np.ndarray, z_range: tuple[float, float]) -> float:
    """The z of the floor's main surface: the height that most open cells (cost 1) in z_range have"""
    z    = nodes[:, 2]
    open = z[(z >= z_range[0]) & (z < z_range[1]) & ~blocked & (cost <= 1.0)]

    if not open.size:
        raise FloorPlanError(f'no open terrain cell has z in {z_range}')

    counts, edges = np.histogram(open, bins=np.arange(z_range[0], z_range[1] + 0.1, 0.1))

    return float(edges[counts.argmax()] + 0.05)


def render(nodes: np.ndarray, cost: np.ndarray, blocked: np.ndarray, z_range: tuple[float, float], cell: float) -> tuple[bytes, dict]:
    """The cells near the main surface of the floor in z_range as a PNG, one pixel per cell, +y up; and where it lies
    in the map frame. A pixel with more than one cell (a table top over the floor) shows the cheapest free one; a
    blocked cell shows only where no free cell is, because the image says where the robot can walk"""
    level = surface(nodes, cost, blocked, z_range)
    keep  = np.abs(nodes[:, 2] - level) <= BAND
    open  = keep & ~blocked & (cost <= 1.0)
    blocks, counts = np.unique(np.floor(nodes[open, :2] / BLOCK_M).astype(int), axis=0, return_counts=True)
    dense = blocks[counts >= MIN_OPEN] if (counts >= MIN_OPEN).any() else blocks
    low   = dense.min(axis=0) * BLOCK_M - CROP_M
    high  = (dense.max(axis=0) + 1) * BLOCK_M + CROP_M
    keep &= np.all((nodes[:, :2] >= low) & (nodes[:, :2] <= high), axis=1)

    x, y, c, b = nodes[keep, 0], nodes[keep, 1], cost[keep], blocked[keep]
    x0, y0     = x.min() - MARGIN * cell, y.min() - MARGIN * cell
    i          = np.rint((x - x0) / cell).astype(int)          # rint: cells sit on the grid; int() would leave stripes
    j          = np.rint((y - y0) / cell).astype(int)
    w, h       = i.max() + MARGIN + 1, j.max() + MARGIN + 1
    row        = h - 1 - j                                     # +y up

    best = np.full((h, w), np.inf)                             # the cheapest free cost at each pixel
    free = ~b & np.isfinite(c)
    np.minimum.at(best, (row[free], i[free]), c[free])

    image               = np.zeros((h, w, 4), np.uint8)        # no cell: transparent, the page's background shows
    image[row[b], i[b]] = (*BLOCKED, 255)
    shade               = np.clip((best - 1.0) / (MAX_COST_SHADE - 1.0), 0, 1)[..., None]
    has_free            = np.isfinite(best)
    image[has_free, :3] = (np.array(OPEN) + (np.array(COSTLY) - np.array(OPEN)) * shade)[has_free].astype(np.uint8)
    image[has_free, 3]  = 255

    return _png(image), {'x0': round(float(x0), 3), 'y0': round(float(y0), 3), 'cell': cell, 'w': int(w), 'h': int(h),
                         'surface_z': round(level, 2)}


class FloorPlans:
    """The floor images of the map that map_info has loaded: all floors made at the first request for a map, and
    only the images kept"""

    def __init__(self):
        self._lock   = threading.Lock()     # the state; held only briefly
        self._making = threading.Lock()     # one terrain read at a time: two first requests must not both read it
        self._reset(None, {}, 'map_info has not said which map it loaded')

    def _reset(self, source, floors: dict, problem: str):
        """The caller holds _lock, or no other thread can see the object yet"""
        self._source, self._floors, self._made, self.problem = source, floors, None, problem

    def use(self, places_file: str):
        """map_info's places file, at each Places message: a new map, or a new terrain cache, drops the images"""
        try:
            path        = Path(places_file)
            pcd, floors = read_floors(path)
            terrain     = pcd.with_suffix('.terrain.npz')
            source      = (path, terrain, terrain.stat().st_mtime, tuple(sorted(floors.items())))   # a floor change too
        except (OSError, ValueError, yaml.YAMLError, FloorPlanError, KeyError, TypeError) as error:
            with self._lock:
                self._reset(None, {}, f'no floor plan: {error}')
            return

        with self._lock:
            if source != self._source:
                self._reset(source, floors, '' if floors else f'{path} has no floors with z_min and z_max')

    def floors(self) -> dict:
        """Each floor's z range, the plan's colours, why there is no plan ('' when there is one), and a version that
        changes with the map, its terrain cache or its floors: the page loads the images again when it changes"""
        with self._lock:
            return {'floors': {name: list(z) for name, z in self._floors.items()}, 'colors': COLORS,
                    'problem': self.problem, 'version': f'{hash(self._source) & 0xffffffff:08x}' if self._source else ''}

    def image(self, floor: str) -> tuple[bytes, dict] | None:
        """The floor's PNG and where it lies; None when there is no such floor. Raises FloorPlanError (or OSError) when
        it cannot be made. The first call for a map reads the terrain (about 20 MB, a second) and makes every floor:
        it runs on the HTTP thread that asked, never on the spin thread"""
        with self._making:
            with self._lock:
                source, floors, made = self._source, dict(self._floors), self._made

            if source is None or floor not in floors:
                return None

            if made is None:
                made = self._make(source, floors)

                with self._lock:
                    if self._source == source:
                        self._made = made

        if isinstance(made[floor], Exception):
            raise made[floor]

        return made[floor]

    @staticmethod
    def _make(source, floors: dict) -> dict:
        """Every floor's image, or the error of a floor that has none; the terrain is not kept"""
        try:                                             # a cache half written, or of another format: say so, never crash
            with np.load(source[1], allow_pickle=False) as data:
                params = yaml.safe_load(str(data['params'])) if 'params' in data.files else {}
                nodes, cost, blocked = data['nodes'], data['cost'], data['blocked']

            cell = float(params.get('cell', 0.2)) if isinstance(params, dict) else 0.2
        except Exception as error:
            raise FloorPlanError(f'the terrain cache {source[1]} cannot be read: {error!r}') from error

        made = {}

        for name, z_range in floors.items():
            try:
                made[name] = render(nodes, cost, blocked, z_range, cell)
            except FloorPlanError as error:
                made[name] = error

        return made
