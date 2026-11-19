"""Named places for the blackboard, live from map_info.

map_info (navigation_stack, package map_info) publishes every place and region of the prior map, latched on ~/places,
after each load, save, delete and reload. Each message replaces the places, so a place saved while the orchestrator
runs is on the blackboard of the next task. The entries have the format of places.py, with kind, floor and note.
"""

from rclpy.node import Node

from .places import PlacesError, parse_places
from .qos import LATCHED_QOS
from skill_registry.geometry import pose_dict


class MapInfoPlaces:
    """The places of map_info at `prefix` (e.g. /map_info); call it for the places now"""

    def __init__(self, node: Node, prefix: str, without: str = 'Tasks have no named places'):
        from navigation_interfaces.msg import Places

        self.places        = {}
        self.heard         = False
        self._topic        = f'{prefix}/places'
        self._without      = without                # what is missing without the places, for the warning
        self._logger       = node.get_logger()
        self._subscription = node.create_subscription(Places, self._topic, self._on_places, LATCHED_QOS)
        self._check        = node.create_timer(10.0, self._check_heard)

        self._logger.info(f'places from {self._topic}')

    def _check_heard(self):
        """Once, 10 s after startup: with no places, every named place in a task fails, so say why"""
        self._check.cancel()

        if not self.heard:
            self._logger.warn(f'no places from {self._topic} after 10 s: is map_info running? {self._without}')

    def __call__(self) -> dict:
        return self.places

    def _on_places(self, message):
        self.heard = True
        entries    = {}

        for place in message.places:
            entries[place.name] = pose_dict(place.pose) | {'kind': place.kind, 'floor': place.floor, 'note': place.note}

        try:
            self.places = parse_places({'places': entries})
        except PlacesError as error:                    # two names with one key: rename one in map_info
            return self._logger.error(f'map_info places not used, the earlier ones stay: {error}')

        self._logger.info(f'map_info: {len(self.places)} places from {message.annotations_path}')
