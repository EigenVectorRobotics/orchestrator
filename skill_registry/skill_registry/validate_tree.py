import sys
import argparse

from pathlib import Path

from .registry import Registry, skills_dir
from .validator import TreeValidator, ValidatorConfig


def main():
    parser = argparse.ArgumentParser(description='Check a BehaviorTree.CPP v4 tree against the skill contracts')
    parser.add_argument('tree', type=Path)
    parser.add_argument('--skills', type=Path, help='the library, one folder per skill; default: <agent_dir>/skills')
    parser.add_argument('--blind', action='store_true', help='the tree must run with no cloud link')
    arguments = parser.parse_args()

    registry  = Registry.from_directory(arguments.skills or skills_dir())
    validator = TreeValidator(registry, ValidatorConfig(blind=arguments.blind))
    issues    = validator.validate(arguments.tree.read_text())

    for issue in issues:
        print(f'{issue.level:8} {issue.path}: {issue.message}')

    num_errors = sum(issue.level == 'error' for issue in issues)
    print(f'{len(registry)} skills, {num_errors} errors, {len(issues) - num_errors} warnings')

    sys.exit(1 if num_errors else 0)


if __name__ == '__main__':
    main()
