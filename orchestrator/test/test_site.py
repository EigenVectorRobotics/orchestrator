"""The site file: what a robot run needs beyond the code defaults, read by the launch and by the agent bridge."""

import importlib.util
import sys

from pathlib import Path

import pytest

from orchestrator.common.site import SiteError, load_site, site_path


PACKAGE = Path(__file__).resolve().parents[1]
SITES   = PACKAGE / 'config' / 'sites'


def test_the_stanley_site_has_what_the_robot_run_needs():
    site = load_site('stanley', SITES)

    assert site['places_file'].endswith('two_floor_clean_cabin2.places.yaml')
    assert site['planner_backend'] == 'external'
    assert site['agent_bridge']['host'] and site['agent_bridge']['port'] == 8771
    assert '/imu/data' in site['raw_bag']['topics']


def test_no_site_is_empty_and_a_name_or_a_path_both_work(tmp_path):
    path = tmp_path / 'lab.yaml'
    path.write_text('planner_backend: claude\n')

    assert load_site('', SITES) == {}
    assert site_path('stanley', SITES) == SITES / 'stanley.yaml'
    assert load_site(str(path), SITES) == {'planner_backend': 'claude'}


def test_a_missing_site_or_an_unknown_key_is_an_error(tmp_path):
    with pytest.raises(SiteError, match='sites: stanley'):
        load_site('nowhere', SITES)

    path = tmp_path / 'typo.yaml'
    path.write_text('place_file: /x\n')

    with pytest.raises(SiteError, match="unknown keys \\['place_file'\\]"):
        load_site(str(path), SITES)


def _launch():
    pytest.importorskip('launch')
    spec   = importlib.util.spec_from_file_location('orchestrator_launch', PACKAGE / 'launch' / 'orchestrator.launch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def _resolve(given: dict, site: str = ''):
    return _launch().resolve(given, load_site(site, SITES), PACKAGE)


def test_the_launch_takes_the_site_and_a_given_argument_wins():
    found = _resolve({'planner_backend': 'claude'}, 'stanley')

    assert found['places_file'].endswith('two_floor_clean_cabin2.places.yaml')
    assert found['planner_backend'] == 'claude' and found['planner_model'] == 'claude-opus-5-5'
    assert (found['agent_push_host'], found['agent_push_port']) == ('100.107.208.25', 8771)
    assert found['raw_bag']['output'].rsplit('/', 1)[-1].startswith('raw_') and '/tf' in found['raw_bag']['topics']
    assert _resolve({'raw_bag': 'false'}, 'stanley')['raw_bag'] is None
    assert _resolve({'places_file': '/x.yaml'}, 'stanley')['places_file'] == '/x.yaml'


def test_without_a_site_the_code_defaults_and_no_bridge_or_raw_bag():
    found = _resolve({})

    assert found['places_file'] == str(PACKAGE / 'config' / 'places.yaml')
    assert found['planner_backend'] == 'claude' and found['agent_push_host'] == '' and found['raw_bag'] is None
    assert _resolve({'fake': 'true'}, 'stanley')['raw_bag'] is None          # a fake run records no raw topics


def test_a_fake_run_has_no_preflight_unless_asked_and_a_robot_run_has_one():
    assert _resolve({}, 'stanley')['preflight'] is True
    assert _resolve({'preflight': 'false'}, 'stanley')['preflight'] is False
    assert _resolve({'fake': 'true'})['preflight'] is False          # the fake robot cannot pass it: the stacks are not there
    assert _resolve({'fake': 'true', 'preflight': ''})['preflight'] is False   # the launch's default value
    assert _resolve({'preflight': ''}, 'stanley')['preflight'] is True
    assert _resolve({'fake': 'true', 'preflight': 'true'})['preflight'] is True


def test_the_bridge_takes_its_address_from_the_site(monkeypatch):
    pytest.importorskip('claude_agent_sdk')
    sys.path.insert(0, str(PACKAGE.parent / 'mission_control'))
    bridge = importlib.import_module('agent_bridge')

    assert load_site('stanley', bridge.SITES)['agent_bridge']['host']
