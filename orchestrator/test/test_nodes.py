"""A node of ours must not set a name of rclpy's Node: `self._subscriptions = [...]` stopped the spin of the places"""

import ast

from pathlib import Path

import pytest

rclpy = pytest.importorskip('rclpy')

import orchestrator
import skill_registry

from rclpy.context import Context
from rclpy.node import Node


PACKAGES = [Path(orchestrator.__file__).parent, Path(skill_registry.__file__).parent]


def _node_names() -> set[str]:
    """The attributes a Node sets in __init__, and its methods and properties"""
    context = Context()
    rclpy.init(context=context)

    try:
        names = set(vars(Node('name_probe', context=context))) | set(dir(Node))
    finally:
        rclpy.shutdown(context=context)

    return names


def _self_assignments(path: Path):
    """(class, attribute, line) for each `self.x = ...` in a class that has Node as a base"""
    for cls in ast.walk(ast.parse(path.read_text())):
        if isinstance(cls, ast.ClassDef) and any(isinstance(base, ast.Name) and base.id == 'Node' for base in cls.bases):
            for target in ast.walk(cls):
                if (isinstance(target, ast.Attribute) and isinstance(target.ctx, ast.Store)
                        and isinstance(target.value, ast.Name) and target.value.id == 'self'):
                    yield cls.name, target.attr, target.lineno


def test_no_node_of_ours_sets_a_name_of_rclpy_node():
    reserved = _node_names()
    clashes  = [f'{path.name}:{line} {cls}.{name}' for package in PACKAGES for path in sorted(package.glob('*.py'))
                for cls, name, line in _self_assignments(path) if name in reserved]

    assert clashes == []
