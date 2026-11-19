"""validate_tree: check a tree file against the contracts and print its issues."""

import sys
import argparse

from pathlib import Path

from .registry import Registry, contract_directories
from .validator import TreeValidator, ValidatorConfig
from .tree_files import IncludeError, load_tree


def main():
    parser = argparse.ArgumentParser(description='Check a BehaviorTree.CPP v4 tree against the skill contracts')
    parser.add_argument('tree', type=Path)
    parser.add_argument('--contracts', type=Path, help='a folder of contracts; default: the skill packages')
    parser.add_argument('--skill-packages', nargs='+', help='Python packages with one folder per skill; default: g1_skills')
    parser.add_argument('--blind', action='store_true', help='the tree must run with no cloud link')
    arguments = parser.parse_args()

    registry  = Registry.from_directories(contract_directories(arguments.skill_packages, arguments.contracts or ''))
    validator = TreeValidator(registry, ValidatorConfig(blind=arguments.blind))

    try:
        issues = validator.validate(load_tree(arguments.tree))
    except IncludeError as error:
        print(f'error    {error}')
        sys.exit(1)

    for issue in issues:
        print(f'{issue.level:8} {issue.path}: {issue.message}')

    num_errors = sum(issue.level == 'error' for issue in issues)
    print(f'{len(registry)} skills, {num_errors} errors, {len(issues) - num_errors} warnings')

    sys.exit(1 if num_errors else 0)


if __name__ == '__main__':
    main()
