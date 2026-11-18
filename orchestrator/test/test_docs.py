"""The documents stay in step with the repository: CLAUDE.md lists every document, links resolve, file paths in
backticks exist, and no retired experience id (E-0xx, N-0xx) is left"""

import re

from pathlib import Path

import pytest


ROOT      = Path(__file__).resolve().parents[2]
SKIP_DIRS = {'.git', 'build', 'install', 'log', 'node_modules', '.pytest_cache', '__pycache__', '.claude'}
TOP_DIRS  = ('orchestrator/', 'skill_registry/', 'mission_control/', 'docs/', 'run_viewer/')
LINK      = re.compile(r'\[[^\]]*\]\(([^)\s]+)\)')
CODE      = re.compile(r'`([^`\n]+)`')
HEADING   = re.compile(r'^#{1,6}\s+(.*)$', re.M)
OLD_ID    = re.compile(r'\b[EN]-0\d\d\b')


def documents() -> list[Path]:
    return sorted(path for path in ROOT.rglob('*.md') if not SKIP_DIRS & set(path.relative_to(ROOT).parts))


def slug(heading: str) -> str:
    """GitHub's anchor for a heading"""
    text = re.sub(r'[`*_]', '', heading.strip().lower())
    text = re.sub(r'[^\w\- ]', '', text)

    return text.replace(' ', '-')


def anchors(path: Path) -> set[str]:
    return {slug(heading) for heading in HEADING.findall(path.read_text())}


def test_claude_md_lists_every_document():
    listed  = (ROOT / 'CLAUDE.md').read_text()
    missing = [str(path.relative_to(ROOT)) for path in documents()
               if path.name != 'CLAUDE.md' and f'`{path.relative_to(ROOT)}`' not in listed]

    assert not missing, f'documents no session is told to read (add them to CLAUDE.md, or delete them): {missing}'


@pytest.mark.parametrize('path', documents(), ids=lambda path: str(path.relative_to(ROOT)))
def test_links_resolve(path):
    broken = []

    for target in LINK.findall(path.read_text()):
        if re.match(r'[a-z]+://|mailto:', target):
            continue

        file, _, anchor = target.partition('#')
        linked = (path.parent / file).resolve() if file else path

        if not linked.exists():
            broken.append(target)
        elif anchor and linked.suffix == '.md' and anchor not in anchors(linked):
            broken.append(target)

    assert not broken, f'{path.relative_to(ROOT)}: links that do not resolve: {broken}'


@pytest.mark.parametrize('path', documents(), ids=lambda path: str(path.relative_to(ROOT)))
def test_paths_in_backticks_exist(path):
    missing = []

    for text in CODE.findall(path.read_text()):
        candidate = text.split(':')[0].split()[0] if text.split() else ''

        if not candidate.startswith(TOP_DIRS) or re.search(r'[<>{}$]', candidate):
            continue

        if '*' in candidate:
            if not list(ROOT.glob(candidate)):
                missing.append(candidate)
        elif not (ROOT / candidate.rstrip('/')).exists():
            missing.append(candidate)

    assert not missing, f'{path.relative_to(ROOT)}: paths that do not exist: {sorted(set(missing))}'


@pytest.mark.parametrize('path', documents(), ids=lambda path: str(path.relative_to(ROOT)))
def test_no_retired_experience_ids(path):
    found = sorted(set(OLD_ID.findall(path.read_text())))

    assert not found, f'{path.relative_to(ROOT)}: retired experience ids: {found}'
