"""The QoS profiles of the orchestrator's topics. The fake robot and the elevator_sim bridge use the same profiles;
keep them in step"""

from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy


COMMAND_QOS = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)
LATCHED_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
SENSOR_QOS  = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
TRACE_QOS   = QoSProfile(depth=100, reliability=ReliabilityPolicy.RELIABLE)
