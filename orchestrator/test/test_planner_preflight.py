"""The planner waits for the preflight run's end, and stops waiting when the executor refuses the preflight tree."""

import json
import threading

from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')

from orchestrator.planner_node import PlannerNode


def _waiting():
    return SimpleNamespace(_preflight_done=threading.Event(), _preflight_status='', _preflight_started=False)


def _event(planner, **event):
    PlannerNode._on_trace(planner, SimpleNamespace(data=json.dumps({'source': 'executor', **event})))


def test_a_refused_preflight_ends_the_wait():
    planner = _waiting()

    _event(planner, kind='rejected', tree_id='freight_elevator_go_to_place', text='tree refused: the recorder is not ready')

    assert planner._preflight_done.is_set() and planner._preflight_status == 'rejected'


def test_a_finished_preflight_ends_the_wait_with_its_status():
    planner = _waiting()

    _event(planner, kind='started', tree_id='preflight')
    _event(planner, kind='finished', tree_id='preflight', status='success')

    assert planner._preflight_done.is_set() and planner._preflight_status == 'success'


def test_a_refusal_after_the_preflight_started_is_another_tree():
    planner = _waiting()

    _event(planner, kind='started', tree_id='preflight')
    _event(planner, kind='rejected', tree_id='preflight', text='tree rejected: 2 errors')

    assert not planner._preflight_done.is_set()
