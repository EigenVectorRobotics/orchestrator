"""The executor runs a task tree only after a preflight passed, unless the sender skips it on purpose."""

import json

import pytest

pytest.importorskip('rclpy')

from orchestrator.execution.node import PREFLIGHT_TREE, preflight_gate, tree_message


def test_a_task_tree_waits_for_a_passed_preflight():
    allowed, note = preflight_gate('freight_elevator_go_to_place', passed=False, skip=False, required=True)

    assert not allowed and 'no preflight has passed' in note and '--skip-preflight' in note


def test_the_preflight_itself_and_a_task_after_it_run():
    assert preflight_gate(PREFLIGHT_TREE, passed=False, skip=False, required=True) == (True, '')
    assert preflight_gate('freight_elevator_go_to_place', passed=True, skip=False, required=True) == (True, '')


def test_a_skipped_preflight_runs_and_is_traced():
    allowed, note = preflight_gate('press_repeat', passed=False, skip=True, required=True)

    assert allowed and 'skip_preflight' in note


def test_no_gate_when_preflight_is_not_required():
    assert preflight_gate('press_repeat', passed=False, skip=False, required=False) == (True, '')


def test_a_tree_message_is_xml_or_json_with_the_skip():
    xml = '<root BTCPP_format="4"><BehaviorTree ID="t"><say text="hi"/></BehaviorTree></root>'

    assert tree_message(xml) == (xml, False)
    assert tree_message(json.dumps({'tree': xml, 'skip_preflight': True})) == (xml, True)
    assert tree_message(json.dumps({'tree': xml})) == (xml, False)
