import os
import re
import sys
import shutil
import posixpath
import subprocess

from pathlib import Path

import yaml
import griffe


ROOT      = Path(__file__).resolve().parents[2]
SKIP_DIRS = {'.git', '.github', 'build', 'install', 'log', 'site', '_site_src', 'node_modules', '.pytest_cache',
             '__pycache__', '.claude'}
ASSETS    = {'.png', '.jpg', '.jpeg', '.svg', '.gif'}
LINK      = re.compile(r'(\]\()([^)\s#]*)(#[^)\s]*)?(\))')
HEADING   = re.compile(r'^#\s+(.+)$', re.M)
SKIP_CODE = re.compile(r'(^|/)(test|tests|launch|scripts)(/|$)')


def _skipped(path: Path) -> bool:
    return bool(SKIP_DIRS & set(path.relative_to(ROOT).parts))


def _branch() -> str:
    if os.environ.get('GITHUB_REF_NAME'):
        return os.environ['GITHUB_REF_NAME']

    result = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--abbrev-ref', 'HEAD'], capture_output=True, text=True)
    name   = result.stdout.strip()

    return name if name and name != 'HEAD' else 'main'


def _url(rel: str) -> str:
    """The page's URL path inside the site: README.md is its folder's index"""
    if Path(rel).name == 'README.md':
        folder = posixpath.dirname(rel)
        return f'/{folder}/' if folder else '/'

    return '/' + rel[:-3] + '.html'


def _relative(from_url: str, to_url: str) -> str:
    start  = from_url if from_url.endswith('/') else posixpath.dirname(from_url) + '/'
    target = posixpath.relpath(to_url, start)

    return target + '/' if to_url.endswith('/') and target != '.' else ('./' if target == '.' else target)


# ========== API reference ==========

def _signature(function) -> str:
    parts = []

    for parameter in function.parameters:
        if parameter.name in ('self', 'cls'):
            continue

        prefix = {'variadic positional': '*', 'variadic keyword': '**'}.get(parameter.kind.value, '')
        text   = prefix + parameter.name + (f': {parameter.annotation}' if parameter.annotation else '')
        parts.append(text + (f' = {parameter.default}' if parameter.default is not None else ''))

    returns = f' -> {function.returns}' if function.returns else ''

    return f'({", ".join(parts)}){returns}'


def _doc(obj) -> str:
    return obj.docstring.value.strip() if obj.docstring else ''


def _public(obj) -> list:
    return [member for name, member in obj.members.items() if not member.is_alias and not name.startswith('_')]


def _render_module(module) -> list[str]:
    lines = [f'## `{module.path}`', '']

    if _doc(module):
        lines += [_doc(module), '']

    for member in _public(module):
        if member.kind.value == 'class':
            bases = ', '.join(str(base) for base in member.bases)
            init  = member.members.get('__init__')
            lines += [f'### class `{member.name}`' + (f'({bases})' if bases else ''), '']
            lines += [f'`{member.name}{_signature(init)}`', ''] if init is not None and not init.is_alias else []
            lines += [_doc(member), ''] if _doc(member) else []

            for method in _public(member):
                if method.kind.value == 'function':
                    summary = _doc(method).split('\n\n')[0].replace('\n', ' ')
                    lines.append(f'- `{method.name}{_signature(method)}`' + (f': {summary}' if summary else ''))
                elif method.kind.value == 'attribute':
                    lines.append(f'- `{method.name}`' + (f': `{method.annotation}`' if method.annotation else ''))

            lines.append('')
        elif member.kind.value == 'function':
            lines += [f'### def `{member.name}{_signature(member)}`', '']
            lines += [_doc(member), ''] if _doc(member) else []

    return lines


def api_pages(roots: list[str]) -> dict[str, tuple[str, str]]:
    """One page per Python package under the source roots: path -> (title, Markdown)"""
    pages = {}

    for root in roots:
        base = ROOT / root

        for init in sorted(base.rglob('__init__.py')):
            folder = init.parent
            parts  = folder.relative_to(base).parts

            if not parts or _skipped(folder) or SKIP_CODE.search(folder.relative_to(ROOT).as_posix()):
                continue

            top     = griffe.load(parts[0], search_paths=[str(base)], allow_inspection=False, resolve_aliases=False)
            package = top if len(parts) == 1 else top['.'.join(parts[1:])]
            dotted  = '.'.join(parts)
            lines   = [f'# {dotted}', '', f'The API of `{folder.relative_to(ROOT).as_posix()}`, generated from the code.', '']
            lines  += _render_module(package)[2:]

            for path in sorted(folder.glob('*.py')):
                if path.stem != '__init__' and path.stem in package.members:
                    lines += _render_module(package.members[path.stem])

            pages[f'reference/{dotted.replace(".", "/")}.md'] = (dotted, '\n'.join(lines) + '\n')

    return pages


# ========== The site ==========

def _nav(entries: list, parent: str = '', grand_parent: str = '') -> tuple[dict, list, dict]:
    """Front matter per nav page, external links, and generated section pages"""
    matter, external, sections = {}, [], {}

    for order, entry in enumerate(entries, start=1):
        (title, target), = entry.items()
        base = {'title': title, 'nav_order': order, **({'parent': parent} if parent else {}),
                **({'grand_parent': grand_parent} if grand_parent else {})}

        if isinstance(target, list):
            slug = re.sub(r'[^a-z0-9]+', '-', f'{parent} {title}'.lower()).strip('-')
            sections[f'sections/{slug}.md'] = {**base, 'has_children': True, 'has_toc': True}
            child_matter, child_external, child_sections = _nav(target, title, parent)
            matter.update(child_matter)
            external += child_external
            sections.update(child_sections)
        elif re.match(r'[a-z]+://', target):
            external.append({'title': title, 'url': target})
        else:
            matter[target] = base

    return matter, external, sections


def _rewrite(text: str, rel: str, pages: set[str], repo: str, branch: str) -> str:
    """Links to pages go to their URL in the site, links to other repository files go to GitHub"""
    here = _url(rel)
    base = posixpath.dirname(rel)

    def rewrite(match):
        prefix, target, anchor, suffix = match.group(1), match.group(2), match.group(3) or '', match.group(4)

        if not target or re.match(r'[a-z]+:', target):
            return match.group(0)

        resolved = posixpath.normpath(posixpath.join(base, target))
        path     = ROOT / resolved

        if resolved in pages:
            return f'{prefix}{_relative(here, _url(resolved))}{anchor}{suffix}'

        if resolved.startswith('..') or not path.exists() or path.suffix.lower() in ASSETS:
            return match.group(0)

        kind = 'tree' if path.is_dir() else 'blob'

        return f'{prefix}{repo}/{kind}/{branch}/{resolved}{anchor}{suffix}'

    return LINK.sub(rewrite, text)


def _front(fields: dict) -> str:
    return '---\n' + yaml.safe_dump(fields, sort_keys=False, allow_unicode=True) + '---\n\n'


def build(out: Path):
    config = yaml.safe_load((ROOT / 'docs/tools/site.yml').read_text())
    repo   = config['repo_url'].rstrip('/')
    branch = _branch()

    if out.exists():
        shutil.rmtree(out)

    # (1) Every Markdown file and image of the repository, at its own path, and the API reference
    sources = {path.relative_to(ROOT).as_posix(): path.read_text() for path in sorted(ROOT.rglob('*.md'))
               if path.is_file() and not _skipped(path)}
    api     = api_pages(config.get('api_roots', []))
    sources.update({path: text for path, (_, text) in api.items()})

    for path in sorted(ROOT.rglob('*')):
        if path.is_file() and path.suffix.lower() in ASSETS and not _skipped(path):
            target = out / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    # (2) The sidebar: the nav of site.yml, then the API reference
    nav = list(config['nav'])

    if api:
        nav.append({'API reference': [{title: path} for path, (title, _) in api.items()]})

    matter, external, sections = _nav(nav)
    missing = [target for target in matter if target not in sources]

    if missing:
        sys.exit(f'site.yml names pages that do not exist: {missing}')

    # (3) The pages, with front matter and rewritten links
    for rel, text in sources.items():
        headings = HEADING.findall(text)
        fields   = matter.get(rel) or {'title': headings[0].strip() if headings else Path(rel).stem, 'nav_exclude': True}
        fields   = {'layout': 'default', **fields, 'permalink': _url(rel), 'render_with_liquid': False}
        target   = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_front(fields) + _rewrite(text, rel, set(sources), repo, branch))

    for rel, fields in sections.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_front({'layout': 'default', **fields, 'permalink': '/' + rel[:-3] + '/'}) + f'# {fields["title"]}\n')

    # (4) The Jekyll configuration
    site = {
        'title':          config['title'],
        'description':    config.get('description', ''),
        'url':            'https://eigenvectorrobotics.github.io',
        'baseurl':        '/' + repo.rsplit('/', 1)[-1],
        'theme':          'just-the-docs',
        'search_enabled': True,
        'heading_anchors': True,
        'back_to_top':    True,
        'mermaid':        {'version': '11.4.1'},
        'aux_links':      {'GitHub': [repo], **{link['title']: [link['url']] for link in config.get('links', [])}},
        'nav_external_links': [{**link, 'hide_icon': False} for link in external],
        'gh_edit_link':   False,
        'kramdown':       {'input': 'GFM', 'hard_wrap': False, 'syntax_highlighter': 'rouge'},
        'footer_content': f'Built from <code>{branch}</code> of <a href="{repo}">{repo.rsplit("/", 1)[-1]}</a>.',
    }
    (out / '_config.yml').write_text(yaml.safe_dump(site, sort_keys=False, allow_unicode=True))
    print(f'{len(sources)} pages, {len(sections)} sections, {len(api)} API pages -> {out}')


if __name__ == '__main__':
    build(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / '_site_src').resolve())
