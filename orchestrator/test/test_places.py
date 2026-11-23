import math

import pytest

from orchestrator.common.config import PlacesError, parse_places


TEAM_FILE = {                                   # as ~/maps/two_floor_clean.places.yaml on the NUC
    'map':     '/home/vector/maps/two_floor_clean.pcd',
    'frame':   'map',
    'pose_of': 'base_link',
    'places':  {
        'freight elevator call panel':          {'x': 1.771, 'y': -1.356, 'z': -0.458, 'yaw_deg': -15.6, 'inliers': 0.992, 'note': 'by hand'},
        'inside freight elevator button panel': {'x': 3.428, 'y': -0.263, 'z': -0.435, 'yaw_deg': 152.8},
    },
}


def test_the_team_format_becomes_snake_case_keys_with_yaw_in_radians():
    places = parse_places(TEAM_FILE)

    assert set(places) == {'freight_elevator_call_panel', 'inside_freight_elevator_button_panel'}
    assert places['freight_elevator_call_panel'] == pytest.approx({'x': 1.771, 'y': -1.356, 'z': -0.458, 'yaw': math.radians(-15.6), 'frame': 'map'})


def test_this_packages_format_is_unchanged():
    places = parse_places({'call_panel': {'x': 1.0, 'y': 2.0, 'yaw': 0.5}})

    assert places == {'call_panel': {'x': 1.0, 'y': 2.0, 'yaw': 0.5, 'frame': 'map'}}


def test_bad_places_are_refused():
    with pytest.raises(PlacesError, match='needs x and y'):
        parse_places({'places': {'panel': {'x': 1.0}}})

    with pytest.raises(PlacesError, match='two places'):
        parse_places({'places': {'Call Panel': {'x': 0, 'y': 0}, 'call panel': {'x': 1, 'y': 1}}})


def test_the_robot_trees_validate_against_the_team_places():
    from pathlib import Path

    import yaml

    from skill_registry import Registry, TreeValidator, ValidatorConfig, load_tree

    root      = Path(__file__).resolve().parents[2]
    registry  = Registry.from_directory(root / 'g1_skills' / 'g1_skills')
    places    = yaml.safe_load((root / 'orchestrator' / 'config' / 'places' / 'fvd_map_two_elevators.places.yaml').read_text())
    validator = TreeValidator(registry, ValidatorConfig(known_keys=set(parse_places(places))))
    trees     = [root / 'orchestrator' / 'test_trees' / 'freight_elevator_floor1_press_agentic.xml', *(root / 'skill_registry' / 'trees').glob('*.xml')]

    for tree in trees:
        assert [issue.message for issue in validator.validate(load_tree(tree)) if issue.level == 'error'] == [], tree.name


def test_place_floors_come_from_the_floors_table_by_z():
    from orchestrator.common.config import place_floors

    raw = {**TEAM_FILE, 'floors': {'floor 1': {'z_min': -10.0, 'z_max': -3.5}, 'floor 2': {'z_min': -3.5, 'z_max': 2.5}}}
    raw['places'] = {**raw['places'], 'target floor 1': {'x': -0.7, 'y': -0.1, 'z': -6.53, 'yaw_deg': 169.8}, 'no z': {'x': 0.0, 'y': 0.0}}

    assert place_floors(raw) == {'freight_elevator_call_panel': '2', 'inside_freight_elevator_button_panel': '2', 'target_floor_1': '1'}
    assert place_floors(TEAM_FILE) == {}
