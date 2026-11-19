#!/usr/bin/env python3
"""Make a session's Claude Code settings from the templates in git (link_agent_dir.sh runs it).

    agent_settings.py OUT TEMPLATE [TEMPLATE ...] [--hide DIR ...] [--no-edit DIR ...]

The deny lists of the templates are joined in order. Each DIR to hide becomes a Read deny (and each --no-edit DIR an
Edit deny) with its absolute path, which a template cannot hold: the workspace is in a different place on each
machine. Edit the templates, not OUT. A file at OUT that this script did not make stops it."""

import sys
import json
import argparse

from pathlib import Path


MADE = 'Made by agent_settings.py'      # in each file it makes: it replaces only those


def _paths(folders: list[Path]) -> list[str]:
    """Each folder as given and as resolved: a rule on one path may not match a link to it"""
    return list(dict.fromkeys(str(path) for folder in folders for path in (folder.absolute(), folder.resolve())))


def settings(templates: list[Path], hide: list[Path], no_edit: list[Path] = ()) -> dict:
    deny  = [rule for template in templates for rule in json.loads(template.read_text())['permissions']['deny']]
    deny += [f'Read(/{path}/**)' for path in _paths(hide)]           # //abs: an absolute path in a rule
    deny += [f'Edit(/{path}/**)' for path in _paths(no_edit)]
    made  = ', '.join(str(template) for template in templates)

    return {'$comment': f'{MADE} from {made}: edit those, then run link_agent_dir.sh again.',
            'permissions': {'deny': deny}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('out', type=Path)
    parser.add_argument('templates', type=Path, nargs='+')
    parser.add_argument('--hide', type=Path, nargs='*', default=[], help='folders the session may not read')
    parser.add_argument('--no-edit', type=Path, nargs='*', default=[], help='folders the session may not edit')
    arguments = parser.parse_args()

    out = arguments.out

    if out.exists() and not out.is_symlink() and MADE not in out.read_text():
        return f'stop: {out} is a file that a person wrote; move what you need into the templates, then run this again'

    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)                                         # an old link into git: never write through it
    out.write_text(json.dumps(settings(arguments.templates, arguments.hide, arguments.no_edit), indent=2) + '\n')


if __name__ == '__main__':
    sys.exit(main())
