"""A stand-in for the robot: the same topics, services, action and VLM server, with scripted results

Use it to run the executor on a laptop, with no hardware.
"""

import json
import math
import time
import threading

import rclpy
import numpy as np

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rclpy.node import Node
from rclpy.action import ActionServer, CancelResponse
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.callback_groups import ReentrantCallbackGroup
from std_msgs.msg import Bool, Empty, String
from std_srvs.srv import Trigger
from sensor_msgs.msg import Image

from .qos import COMMAND_QOS, LATCHED_QOS


class FakeRobot(Node):
    def __init__(self):
        super().__init__('fake_robot')

        self.nav_time_s    = self.declare_parameter('nav_time_s', 2.0).value
        self.nav_blocked   = self.declare_parameter('nav_blocked', False).value     # true: every route ends blocked, until cancelled
        self.press_time_s  = self.declare_parameter('press_time_s', 2.0).value
        self.press_success = self.declare_parameter('press_success', True).value
        self.press_fails   = self.declare_parameter('press_fail_count', 0).value     # the first N presses miss, as out of reach
        self.vlm_port      = self.declare_parameter('vlm_port', 8090).value
        self.vlm_answers   = dict(                                                           # 'door=closed,lit=not_lit'
            pair.split('=', 1) for pair in self.declare_parameter('vlm_answers', '').value.split(',') if '=' in pair
        )

        self.group         = ReentrantCallbackGroup()
        self.nav_cancelled = False
        self.nav_payload   = None             # the last status sent; route_planner sends only changes
        self.is_tracking   = False

        self._status    = self.create_publisher(String, '/route_planner/status', LATCHED_QOS)
        self._slam      = self.create_publisher(String, '/slam_elevatorlio/status', LATCHED_QOS)
        self._tracking  = self.create_publisher(Bool, '/track_3d/is_tracking', COMMAND_QOS)
        self._elevator  = self.create_publisher(Bool, '/LIO/in_elevator', COMMAND_QOS)
        self._camera    = self.create_publisher(Image, '/camera/camera/color/image_raw', 1)     # raw, as on the Orin
        self._frame     = np.full((480, 640, 3), (90, 90, 95), dtype=np.uint8)                   # a plain grey frame

        self.create_subscription(Bool, '/cancel_goal', self._on_cancel, COMMAND_QOS)
        self.create_subscription(String, '/keyboard_input', self._on_query, COMMAND_QOS)
        self.create_subscription(Empty, '/track_3d/reset', self._on_reset, COMMAND_QOS)
        self.create_subscription(String, '/speak', self._on_tts, COMMAND_QOS)

        for name in ['ready_locomotion', 'release_arms']:
            self.create_service(Trigger, f'/unitree_interface/{name}', self._on_trigger)

        self._make_nav_action()
        self._make_arm_action()
        self._start_vlm_server()

        self._publish_nav('idle')
        self._slam.publish(String(data=json.dumps({'mode': 'localization', 'localized': True, 'healthy': True})))
        self.create_timer(0.2, self._on_timer)

        self.get_logger().info(f'fake robot up; fake VLM on port {self.vlm_port}')

    # ========== Navigation ==========

    def _make_nav_action(self):
        try:
            from navigation_interfaces.action import NavigateToGoal
        except ImportError:
            self.get_logger().warn('navigation_interfaces not built: no fake navigate_to')
            return

        self._nav_type   = NavigateToGoal
        self._nav_server = ActionServer(
            self,
            NavigateToGoal,
            '/route_planner/navigate_to_goal',
            self._navigate,
            cancel_callback=lambda goal: CancelResponse.ACCEPT,
            callback_group=self.group
        )

    def _navigate(self, goal_handle):
        """Like route_planner: routing, then arrived on the status and the feedback once the goal is near, then idle
        when it ends. With nav_blocked, the route turns blocked halfway and stays so until the goal is cancelled"""
        position = goal_handle.request.goal.pose.position
        start    = time.monotonic()
        deadline = math.inf if self.nav_blocked else start + self.nav_time_s
        result   = self._nav_type.Result()

        def set_state(state: str):
            if self._publish_nav(state):
                goal_handle.publish_feedback(self._nav_type.Feedback(state=state, dist_to_goal=1.0))

        self.get_logger().info(f'route goal ({position.x:.1f}, {position.y:.1f})' + (' (will block)' if self.nav_blocked else ''))
        self.nav_cancelled = False
        self.nav_payload   = None
        set_state('routing')

        while time.monotonic() < deadline:
            if goal_handle.is_cancel_requested or self.nav_cancelled:
                self._publish_nav('idle')
                result.reason = 'cancelled'

                if goal_handle.is_cancel_requested:
                    goal_handle.canceled()
                else:
                    goal_handle.abort()

                return result

            if time.monotonic() - start > self.nav_time_s / 2:
                set_state('blocked' if self.nav_blocked else 'arrived')

            time.sleep(0.05)

        self._publish_nav('idle')
        result.success, result.reason = True, 'arrived'
        goal_handle.succeed()

        return result

    def _on_cancel(self, message: Bool):
        self.nav_cancelled = True
        self._publish_nav('idle')

    def _publish_nav(self, state: str):
        """Publish the status when it changes, as route_planner does; True if it was published"""
        payload = json.dumps({'state': state, 'map_id': 'fake', 'dist_to_goal': None})

        if payload == self.nav_payload:
            return False

        self.nav_payload = payload
        self._status.publish(String(data=payload))

        return True

    def _on_timer(self):
        self._tracking.publish(Bool(data=self.is_tracking))
        self._elevator.publish(Bool(data=False))

        image                     = Image()
        image.header.stamp        = self.get_clock().now().to_msg()
        image.header.frame_id     = 'camera_color_optical_frame'
        image.height, image.width = self._frame.shape[:2]
        image.encoding            = 'rgb8'
        image.step                = image.width * 3
        image.data                = self._frame.tobytes()
        self._camera.publish(image)

    # ========== Tracking, voice, services ==========

    def _on_query(self, message: String):
        self.get_logger().info(f'track query: {message.data}')
        threading.Timer(1.0, lambda: setattr(self, 'is_tracking', True)).start()

    def _on_reset(self, message: Empty):
        self.is_tracking = False

    def _on_tts(self, message: String):
        self.get_logger().info(f'SAY: {message.data}')

    def _on_trigger(self, request, response):
        response.success = True
        response.message = 'fake'

        return response

    # ========== Arm ==========

    def _make_arm_action(self):
        try:
            from humanoid_manipulation_interfaces.action import VisualServoingArm
        except ImportError:
            self.get_logger().warn('humanoid_manipulation_interfaces not built: no fake press_button')
            return

        self._action_type = VisualServoingArm
        self._arm_server  = ActionServer(
            self,
            VisualServoingArm,
            'humanoid_arm_visual_servoing',
            self._press,
            cancel_callback=lambda goal: CancelResponse.ACCEPT,
            callback_group=self.group
        )

    def _press(self, goal_handle):
        """Like the servo node: feedback at 2 Hz for press_time_s, then the result. A miss is an arm out of reach"""
        out_of_reach = self.press_fails > 0
        self.press_fails -= 1 if out_of_reach else 0
        success      = self.press_success and not out_of_reach
        start        = time.monotonic()
        feedback     = self._action_type.Feedback()
        goal         = (0.62, -0.18, 1.05)

        feedback.current_goal.header.frame_id = 'foot_world'
        feedback.current_goal.pose.position.x, feedback.current_goal.pose.position.y, feedback.current_goal.pose.position.z = goal
        feedback.current_goal.pose.orientation.w = 1.0              # +X into the panel
        self.get_logger().info(f'press: {goal_handle.request.target_description}' + (' (out of reach)' if out_of_reach else ''))
        self.is_tracking = True                                    # the servo node seeds track_anything first

        last_feedback = 0.0

        while (elapsed := time.monotonic() - start) < self.press_time_s:
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                return self._action_type.Result(success=False, message='Cancelled')

            if elapsed - last_feedback >= 0.5:
                last_feedback              = elapsed
                feedback.elapsed_time      = elapsed
                feedback.distance_to_goal  = max(0.0, 0.10 * (1.0 - elapsed / self.press_time_s))
                goal_handle.publish_feedback(feedback)

            time.sleep(0.05)

        self.is_tracking = False                                   # and resets it when the goal ends
        result = self._action_type.Result(success=success, message='Controller reached target')
        result.final_distance = 0.13 if out_of_reach else 0.004

        if success:
            goal_handle.succeed()
        else:
            result.message = 'Planning failed: empty trajectory' if out_of_reach else 'fake press missed'
            goal_handle.abort()

        return result

    # ========== VLM ==========

    def _start_vlm_server(self):
        answers = self.vlm_answers
        logger  = self.get_logger()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'data': [{'id': 'fake'}]}).encode())

            def do_POST(self):
                body     = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                question = body['messages'][0]['content'][1]['text']
                choices  = body['response_format']['json_schema']['schema']['properties']['answer']['enum']
                answer   = next((value for word, value in answers.items() if word in question.lower()), choices[0])
                reply    = {'choices': [{'message': {'content': json.dumps({'answer': answer})}}]}

                logger.info(f'VLM: {question.splitlines()[0]} -> {answer}')

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())

            def log_message(self, *args):
                pass

        self._http = ThreadingHTTPServer(('127.0.0.1', self.vlm_port), Handler)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()


def main():
    rclpy.init()
    node     = FakeRobot()
    executor = MultiThreadedExecutor()
    executor.add_node(node)

    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node._http.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
