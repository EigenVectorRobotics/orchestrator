from orchestrator.map_view import MAX_ROUTE, MapView, floor_number


PLACES = {'call_panel':  {'x': 4.0, 'y': 0.0, 'yaw': 0.0, 'floor': 'floor 2', 'kind': 'button'},   # map_info's floor ids
          'lab_door':    {'x': 0.0, 'y': 3.0, 'yaw': 0.0, 'floor': 'floor 2'},
          'lobby':       {'x': 3.6, 'y': 0.1, 'yaw': 0.0, 'floor': 'floor 1'},
          'no_floor':    {'x': 1.0, 'y': 1.0, 'yaw': 0.0}}


def test_a_floor_id_gives_its_number():
    assert floor_number('floor 2') == 2 and floor_number('2') == 2 and floor_number('floor -1') == -1
    assert floor_number('') is None and floor_number(None) is None and floor_number('roof') is None


def test_the_map_shows_the_robot_s_floor_and_the_nearest_place_on_it():
    now  = [100.0]
    view = MapView(clock=lambda: now[0])

    assert view.state() == {}                                                   # nothing to draw yet

    view.put_places(PLACES)
    view.put_pose({'x': 3.5, 'y': 0.2, 'z': 0.0, 'yaw': 1.0})
    state = view.state()

    assert {place['name'] for place in state['places']} == set(PLACES)         # floor not known: every place
    assert state['robot']['nearest'] is None                                   # lobby is right below: name none

    view.put_floor(2)
    state = view.state()

    assert {place['name'] for place in state['places']} == {'call_panel', 'lab_door', 'no_floor'}
    assert state['robot'] | {'nearest': None} == {'x': 3.5, 'y': 0.2, 'yaw': 1.0, 'age_s': 0.0, 'old': False, 'nearest': None}
    assert state['robot']['nearest'] == {'name': 'call_panel', 'distance': 0.54}

    now[0] = 104.0

    assert view.state()['robot']['old'] and view.state()['robot']['age_s'] == 4.0


def test_a_long_route_is_thinned_keeps_its_ends_and_only_the_robot_s_floor():
    view = MapView()
    view.put_pose({'x': 0.0, 'y': 0.0, 'z': 0.0, 'yaw': 0.0})

    view.put_route([(float(index), 0.0, 0.0) for index in range(1001)])
    route = view.state()['route']

    assert len(route) <= MAX_ROUTE + 1 and route[0] == [0.0, 0.0] and route[-1] == [1000.0, 0.0]
    assert len(set(map(tuple, route))) == len(route)                            # the end is not doubled

    view.put_route([(1.0, 2.0, 0.2), (5.0, 5.0, 0.0), (5.0, 5.0, -6.0), (9.0, 9.0, -6.2)])   # down in the elevator

    assert view.state()['route'] == [[1.0, 2.0], [5.0, 5.0]]


def test_a_pose_is_kept_at_most_five_times_a_second():
    now  = [10.0]
    view = MapView(clock=lambda: now[0])

    assert view.wants_pose()

    view.put_pose({'x': 0.0, 'y': 0.0, 'yaw': 0.0})
    now[0] = 10.1

    assert not view.wants_pose()

    now[0] = 10.25

    assert view.wants_pose()
