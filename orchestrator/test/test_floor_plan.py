import io

import numpy as np
import pytest

from PIL import Image

from orchestrator.floor_plan import BLOCKED, OPEN, FloorPlans, render
from orchestrator.map_view import MapView
from orchestrator.mission_control import Commands, MissionApi, MissionState


def _terrain(path, floors=((0.0, 'floor 1'), (6.0, 'floor 2'))):
    """A 4 m x 2 m room on each floor, a cell every 0.2 m; on floor 1 one blocked cell, one costly cell with a table
    top above it, and a stray cell 50 m away"""
    nodes, cost, blocked = [], [], []

    for z, _ in floors:
        for x in np.arange(0.0, 4.0, 0.2):
            for y in np.arange(0.0, 2.0, 0.2):
                alone = z == 0.0 and abs(x - 1.0) < 0.01 and abs(y - 1.0) < 0.01     # a blocked cell, no free one
                nodes.append((x, y, z)); cost.append(1.0); blocked.append(alone)

    nodes += [(3.0, 1.0, 0.1), (2.0, 1.0, 0.7), (60.0, 60.0, 0.0), (5.0, 1.0, 2.0)]   # blocked, a table top, stray, a roof
    cost += [1.0, 15.0, 1.0, 1.0]
    blocked += [True, False, False, False]
    np.savez(path / 'map.terrain.npz', nodes=np.array(nodes), cost=np.array(cost), blocked=np.array(blocked),
             params=np.array('{"cell": 0.2}'))
    (path / 'map.pcd').write_bytes(b'')
    places = path / 'map.places.yaml'
    places.write_text(f'map: {path / "map.pcd"}\nfloors:\n' + ''.join(
        f'  {name}:\n    z_min: {z - 3}\n    z_max: {z + 3}\n' for z, name in floors))

    return places


def _pixel(png, meta, x, y):
    image = Image.open(io.BytesIO(png))
    return image.getpixel((round((x - meta['x0']) / meta['cell']), meta['h'] - 1 - round((y - meta['y0']) / meta['cell'])))


def test_a_floor_plan_shows_open_costly_and_blocked_cells_of_its_floor_only(tmp_path):
    data      = np.load(_terrain(tmp_path).parent / 'map.terrain.npz')
    png, meta = render(data['nodes'], data['cost'], data['blocked'], (-3.0, 3.0), 0.2)

    assert (meta['w'], meta['h']) == (20 + 4, 10 + 4)            # the room and MARGIN: not the stray cell, not the roof
    assert _pixel(png, meta, 0.4, 0.4)[:3] == tuple(OPEN)
    assert _pixel(png, meta, 1.0, 1.0) == (*BLOCKED, 255)
    assert _pixel(png, meta, 3.0, 1.0)[:3] == tuple(OPEN)                     # a free cell there too: the robot can walk
    assert _pixel(png, meta, 2.0, 1.0)[:3] == tuple(OPEN)                     # the cheapest free cell of the pixel wins
    assert _pixel(png, meta, 3.8, 1.8)[:3] == tuple(OPEN)                     # rint: every cell lands on its pixel
    assert _pixel(png, meta, -0.2, -0.2)[3] == 0                              # the margin, no cell: transparent


def test_the_plans_follow_map_info_s_file_and_say_why_there_is_none(tmp_path):
    plans = FloorPlans()

    assert plans.image('floor 1') is None and 'has not said' in plans.problem

    plans.use(str(_terrain(tmp_path)))
    first = plans.image('floor 2')

    assert plans.floors()['floors'] == {'floor 1': [-3.0, 3.0], 'floor 2': [3.0, 9.0]} and plans.floors()['problem'] == ''
    assert plans.image('floor 2') is first and plans.image('floor 9') is None      # made once; no such floor

    plans.use(str(tmp_path / 'map.places.yaml'))                                    # every 2 s, the same map: kept

    assert plans.image('floor 2') is first

    plans.use(str(tmp_path / 'nothing.yaml'))

    assert plans.image('floor 2') is None and 'no floor plan' in plans.floors()['problem']


def test_the_map_shows_the_robot_s_floor_plan_or_the_one_you_ask_for(tmp_path):
    view   = MapView()
    floors = {'floors': {'floor 1': [-3.0, 3.0], 'floor 2': [3.0, 9.0]}, 'problem': ''}

    assert view.state(floors)['plan']['floor'] == 'floor 1'                    # nothing known: the first
    view.put_pose({'x': 0.0, 'y': 0.0, 'z': 6.5, 'yaw': 0.0})
    assert view.state(floors)['plan']['floor'] == 'floor 2'                    # the robot's z
    view.put_floor(1)
    assert view.state(floors)['plan']['floor'] == 'floor 1'                    # SLAM's floor first

    plans = FloorPlans()
    plans.use(str(_terrain(tmp_path)))
    api   = MissionApi(MissionState(), Commands(None, None, None, None, None), floor_plans=plans)

    code, (png, meta) = api.floor_png('floor 1')
    assert code == 200 and meta['cell'] == 0.2 and png.startswith(b'\x89PNG')
    assert api.floor_png('floor 9')[0] == 404
    assert api.get('/api/state', {})[1]['map']['plan']['floors'] == {'floor 1': [-3.0, 3.0], 'floor 2': [3.0, 9.0]}


def test_a_terrain_file_that_cannot_be_read_is_an_error_not_a_crash(tmp_path):
    plans = FloorPlans()
    plans.use(str(_terrain(tmp_path)))
    cache = tmp_path / 'map.terrain.npz'
    cache.write_bytes(cache.read_bytes()[:len(cache.read_bytes()) // 2])     # half written: route_planner writes it

    assert MissionApi(MissionState(), Commands(None, None, None, None, None), floor_plans=plans).floor_png('floor 1')[0] == 503


def test_a_long_narrow_corridor_is_kept_and_a_stray_cell_is_not():
    room     = [(x, y, 0.0) for x in np.arange(0.0, 20.0, 0.2) for y in np.arange(0.0, 20.0, 0.2)]          # 10 000 cells
    corridor = [(x, y, 0.0) for x in np.arange(20.0, 50.0, 0.2) for y in np.arange(10.0, 11.0, 0.2)]        # 1 m x 30 m
    nodes    = np.array(room + corridor + [(90.0, 90.0, 0.0)])
    png, meta = render(nodes, np.ones(len(nodes)), np.zeros(len(nodes), bool), (-3.0, 3.0), 0.2)

    assert _pixel(png, meta, 49.8, 10.4)[3] == 255                            # the end of the corridor is there
    assert meta['x0'] + meta['w'] * meta['cell'] < 60                         # the stray cell at 90 m is not


def test_a_places_file_of_a_bad_shape_gives_a_problem_not_a_crash_and_a_change_to_its_floors_is_seen(tmp_path):
    places = _terrain(tmp_path)
    plans  = FloorPlans()
    plans.use(str(places))
    first  = plans.floors()['version']

    places.write_text(places.read_text().replace('z_max: 9.0', 'z_max: 8.0'))                # floor 2 moved
    plans.use(str(places))

    assert plans.floors()['floors']['floor 2'] == [3.0, 8.0] and plans.floors()['version'] != first

    (tmp_path / 'list.yaml').write_text('map: map.pcd\nfloors:\n  - {z_min: 0, z_max: 3}\n')
    plans.use(str(tmp_path / 'list.yaml'))

    assert 'not mappings' in plans.floors()['problem'] and plans.floors()['version'] == ''

    (tmp_path / 'relative.yaml').write_text(places.read_text().replace(f'map: {tmp_path / "map.pcd"}', 'map: map.pcd'))
    plans.use(str(tmp_path / 'relative.yaml'))                                # a relative map: next to the places file

    assert plans.floors()['problem'] == '' and plans.image('floor 1') is not None


def test_a_floor_with_no_terrain_cell_has_no_plan_and_the_others_still_do(tmp_path):
    places = _terrain(tmp_path)
    places.write_text(places.read_text() + '  floor 3:\n    z_min: 20\n    z_max: 30\n')
    plans = FloorPlans()
    plans.use(str(places))
    api   = MissionApi(MissionState(), Commands(None, None, None, None, None), floor_plans=plans)

    assert api.floor_png('floor 3')[0] == 503 and 'no open terrain cell' in api.floor_png('floor 3')[1]['message']
    assert api.floor_png('floor 1')[0] == 200
