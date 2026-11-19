#!/usr/bin/env python3
"""Make a session's Claude Code settings from the templates in git (link_agent_dir.sh runs it).

    agent_settings.py OUT TEMPLATE [TEMPLATE ...] [--hide DIR ...]

The deny lists of the templates are joined in order. Each DIR to hide becomes a Read deny with its absolute path,
which a template cannot hold: the workspace is in a different place on each machine. Edit the templates, not OUT."""

import sys
import json
import argparse

from pathlib import Path


def settings(templates: list[Path], hide: list[Path]) -> dict:
    deny = []

    for template in templates:
        deny += [rule for rule in json.loads(template.read_text())['permissions']['deny'] if rule not in deny]

    deny += [f'Read(/{folder.resolve()}/**)' for folder in hide]       # //abs: an absolute path in a rule
    made  = ', '.join(str(template) for template in templates)

    return {'$comment': f'Made by scripts/link_agent_dir.sh from {made}: edit those, then run it again.',
            'permissions': {'deny': deny}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('out', type=Path)
    parser.add_argument('templates', type=Path, nargs='+')
    parser.add_argument('--hide', type=Path, nargs='*', default=[])
    arguments = parser.parse_args()

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.unlink(missing_ok=True)                               # an old link into git: never write through it
    arguments.out.write_text(json.dumps(settings(arguments.templates, arguments.hide), indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
