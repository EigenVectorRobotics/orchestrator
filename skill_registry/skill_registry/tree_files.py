"""Reads a tree file and expands its subtree includes."""

import re

from pathlib import Path


INCLUDE_PATTERN = re.compile(r'^[ \t]*<include\s+path="([^"]+)"\s*/>[ \t]*\n?', re.MULTILINE)
ROOT_PATTERN    = re.compile(r'<root\b[^>]*>(.*)</root>', re.DOTALL)
TREE_ID_PATTERN = re.compile(r'<BehaviorTree\s+ID="([^"]+)"')


class IncludeError(ValueError):
    pass


def load_tree(path: str | Path) -> str:
    """The tree file as one XML text, with each <include path="..."/> replaced by the trees of that file (relative to the including file)"""
    path = Path(path).resolve()
    text = _expand(path, stack=[], seen=set())

    ids        = TREE_ID_PATTERN.findall(text)
    duplicates = sorted({tree_id for tree_id in ids if ids.count(tree_id) > 1})

    if duplicates:
        raise IncludeError(f'{path.name}: BehaviorTree defined more than once: {", ".join(duplicates)}')

    return text


def _expand(path: Path, stack: list[Path], seen: set[Path]) -> str:
    if path in stack:
        raise IncludeError(f'include cycle: {" -> ".join(p.name for p in [*stack, path])}')

    if not path.is_file():
        raise IncludeError(f'included file not found: {path}' + (f' (from {stack[-1].name})' if stack else ''))

    seen.add(path)
    text = path.read_text()

    def inline(match: re.Match) -> str:
        child = (path.parent / match.group(1)).resolve()

        if child in stack or child == path:
            raise IncludeError(f'include cycle: {" -> ".join(p.name for p in [*stack, path, child])}')

        if child in seen:                                   # a file included twice gives its trees once
            return ''

        body = ROOT_PATTERN.search(_expand(child, [*stack, path], seen))

        if not body:
            raise IncludeError(f'{child.name} has no <root> element')

        return body.group(1).strip('\n') + '\n'

    return INCLUDE_PATTERN.sub(inline, text)
