import sys
import argparse

from pathlib import Path

from .registry import Registry
from .validator import TreeValidator, ValidatorConfig
from .tree_files import IncludeError, load_tree


SOURCE_CONTRACTS = Path(__file__).resolve().parent.parent / 'contracts'      # only there in the source tree


def default_contracts() -> Path:
    """The source contracts if run from the source tree, else the installed ones"""
    if SOURCE_CONTRACTS.is_dir():
        return SOURCE_CONTRACTS

    from ament_index_python.packages import get_package_share_directory

    return Path(get_package_share_directory('skill_registry')) / 'contracts'


def main():
    parser = argparse.ArgumentParser(description='Check a BehaviorTree.CPP v4 tree against the skill contracts')
    parser.add_argument('tree', type=Path)
    parser.add_argument('--contracts', type=Path, help='default: the source contracts, else the installed ones')
    parser.add_argument('--blind', action='store_true', help='the tree must run with no cloud link')
    arguments = parser.parse_args()

    registry  = Registry.from_directory(arguments.contracts or default_contracts())
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
