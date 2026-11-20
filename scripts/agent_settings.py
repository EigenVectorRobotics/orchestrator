#!/usr/bin/env python3
"""Make a session's Claude Code settings, and its Codex rules, from the templates in git (link_agent_dir.sh runs it).

    agent_settings.py OUT TEMPLATE [TEMPLATE ...] [--hide DIR ...] [--no-edit DIR ...] [--codex RULES]

The deny lists of the templates are joined in order; their other settings are merged, a later template wins. Each DIR to hide becomes a Read deny (and each --no-edit DIR an
Edit deny) with its absolute path, which a template cannot hold: the workspace is in a different place on each
machine. Edit the templates, not OUT. A file at OUT that this script did not make stops it.

--codex RULES also writes the Bash rules as Codex rules (codex execpolicy: prefix_rule, forbidden), which Codex reads
from .codex/rules/ in the folder where it starts. Codex has no Read or Edit rules: it reads files with shell commands,
so a Codex task session can still read the stack repos."""

import re
import sys
import json
import argparse

from pathlib import Path


MADE = 'Made by agent_settings.py'      # in each file it makes: it replaces only those


def _paths(folders: list[Path]) -> list[str]:
    """Each folder as given and as resolved: a rule on one path may not match a link to it"""
    return list(dict.fromkeys(str(path) for folder in folders for path in (folder.absolute(), folder.resolve())))


def settings(templates: list[Path], hide: list[Path], no_edit: list[Path] = ()) -> dict:
    loaded = [json.loads(template.read_text()) for template in templates]
    deny   = _deny(loaded)
    deny += [f'Read(/{path}/**)' for path in _paths(hide)]           # //abs: an absolute path in a rule
    deny += [f'Edit(/{path}/**)' for path in _paths(no_edit)]
    made  = ', '.join(str(template) for template in templates)

    other = {key: value for template in loaded for key, value in template.items() if key not in ('$comment', 'permissions')}

    return {'$comment': f'{MADE} from {made}: edit those, then run link_agent_dir.sh again.',
            **other, 'permissions': {'deny': deny}}


def _deny(loaded: list[dict]) -> list[str]:
    return [rule for template in loaded for rule in template['permissions']['deny']]


def codex_rules(templates: list[Path]) -> str:
    """The shell rules of the templates as Codex prefix rules: Bash(ros2 topic pub:*) forbids `ros2 topic pub ...`"""
    lines = [f'# {MADE} from {", ".join(map(str, templates))}: edit those, then run link_agent_dir.sh again.']

    for rule in _deny([json.loads(template.read_text()) for template in templates]):
        if not rule.startswith('Bash'):
            continue                                                    # a file rule: Codex has none

        shell = re.fullmatch(r'Bash\(([^*?]+?):\*\)', rule)

        if shell is None:
            raise SystemExit(f'stop: {rule} has no Codex form; a shell rule must be Bash(<command words>:*)')

        lines.append(f'prefix_rule(pattern = {json.dumps(shell[1].split())}, decision = "forbidden", '
                     f'justification = {json.dumps("denied for agent sessions: " + shell[1])})')

    return '\n'.join(lines) + '\n'


def _person_wrote(out: Path) -> str | None:
    """Why this script must not replace out, or None: it replaces only a file that it made, or an old link"""
    if out.exists() and not out.is_symlink() and MADE not in out.read_text():
        return f'stop: {out} is a file that a person wrote; move what you need into the templates, then run this again'

    return None


def _write(out: Path, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)                                         # an old link into git: never write through it
    out.write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('out', type=Path)
    parser.add_argument('templates', type=Path, nargs='+')
    parser.add_argument('--hide', type=Path, nargs='*', default=[], help='folders the session may not read')
    parser.add_argument('--no-edit', type=Path, nargs='*', default=[], help='folders the session may not edit')
    parser.add_argument('--codex', type=Path, help='also write the shell rules as Codex rules here')
    arguments = parser.parse_args()

    files = {arguments.out: json.dumps(settings(arguments.templates, arguments.hide, arguments.no_edit), indent=2) + '\n'}

    if arguments.codex:
        files[arguments.codex] = codex_rules(arguments.templates)

    if stop := next(filter(None, map(_person_wrote, files)), None):     # all or none: the two tools keep the same rules
        return stop

    for out, text in files.items():
        _write(out, text)


if __name__ == '__main__':
    sys.exit(main())
