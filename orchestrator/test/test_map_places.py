import math

import pytest

from types import SimpleNamespace

pytest.importorskip('navigation_interfaces')

from geometry_msgs.msg import PoseStamped
from navigation_interfaces.msg import Place, Places

from orchestrator.map_places import MapInfoPlaces


class FakeNode:
    def __init__(self):
        self.errors    = []
        self.on_places = None

    def create_subscription(self, _type, _topic, callback, _qos):
        self.on_places = callback

    def create_timer(self, _period, _callback):
        return None

    def get_logger(self):
        return SimpleNamespace(info=lambda _text: None, error=self.errors.append)


def _place(name, kind, x, y, z, yaw, floor='', note=''):
    pose                 = PoseStamped()
    pose.header.frame_id = 'map'
    pose.pose.position.x, pose.pose.position.y, pose.pose.position.z = x, y, z
    pose.pose.orientation.z, pose.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)

    return Place(name=name, kind=kind, pose=pose, floor=floor, note=note)


def test_places_and_regions_go_on_the_blackboard_by_key_with_kind_floor_and_note():
    node   = FakeNode()
    places = MapInfoPlaces(node, '/map_info')

    assert places() == {}                               # nothing until map_info publishes

    node.on_places(Places(places=[
        _place('freight elevator call panel', 'place', 1.771, -1.356, -0.458, math.radians(-15.6), 'floor 2', 'by hand'),
        _place('freight elevator cabin', 'region', 3.3, -0.2, -0.435, math.radians(152.8), 'floor 2'),
    ]))

    assert places()['freight_elevator_call_panel'] == pytest.approx({'x': 1.771, 'y': -1.356, 'z': -0.458, 'yaw': math.radians(-15.6),
                                                                     'frame': 'map', 'kind': 'place', 'floor': 'floor 2', 'note': 'by hand'})
    assert places()['freight_elevator_cabin']['kind'] == 'region' and 'note' not in places()['freight_elevator_cabin']


def test_each_message_replaces_the_places_but_two_names_with_one_key_keep_the_earlier_ones():
    node   = FakeNode()
    places = MapInfoPlaces(node, '/map_info')

    node.on_places(Places(places=[_place('call panel', 'place', 1.0, 2.0, 0.0, 0.0)]))
    node.on_places(Places(places=[_place('snack table', 'place', 74.0, -47.0, 0.0, 0.0)]))

    assert set(places()) == {'snack_table'}

    node.on_places(Places(places=[_place('Call Panel', 'place', 5.0, 5.0, 0.0, 0.0), _place('call panel', 'place', 1.0, 2.0, 0.0, 0.0)]))

    assert set(places()) == {'snack_table'} and 'call_panel' in node.errors[0]
