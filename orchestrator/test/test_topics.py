"""Topic names and QoS profiles live in topics.py: the recorder and the tools do not load the executor's modules."""

import subprocess
import sys

import pytest

pytest.importorskip('rclpy')

from rclpy.qos import DurabilityPolicy, ReliabilityPolicy

from orchestrator.common import topics


def test_the_recorder_loads_no_executor_module_for_its_topics():
    code   = 'import sys, orchestrator.recording.node; print(" ".join(sorted(m for m in sys.modules if m.startswith("orchestrator."))))'
    loaded = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True).stdout.split()

    heavy = [name for name in loaded if name.startswith(('orchestrator.skills.', 'orchestrator.agent', 'orchestrator.tree'))]

    assert heavy == ['orchestrator.skills.loader']                 # stdlib only: where the skill packages are


def test_the_latched_and_command_profiles():
    assert topics.LATCHED_QOS.durability == DurabilityPolicy.TRANSIENT_LOCAL and topics.LATCHED_QOS.depth == 1
    assert topics.COMMAND_QOS.durability == DurabilityPolicy.VOLATILE and topics.COMMAND_QOS.depth == 5
    assert topics.LATCHED_QOS.reliability == topics.COMMAND_QOS.reliability == ReliabilityPolicy.RELIABLE
