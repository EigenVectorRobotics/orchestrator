import os
import re
import subprocess

from pathlib import Path

from mkdocs.structure.files import File, Files


ROOT        = Path(__file__).resolve().parents[2]
SKIP_DIRS   = {'.git', '.github', 'build', 'install', 'log', 'site', 'node_modules', '.pytest_cache', '__pycache__', '.claude'}
ASSETS      = {'.png', '.jpg', '.jpeg', '.svg', '.gif'}
LINK        = re.compile(r'(\]\()([^)\s#]+)(#[^)\s]*)?(\))')
SKIP_MODULE = re.compile(r'(^|/)(test|tests|launch|scripts)(/|$)|(^|/)setup\.py$')


def _branch() -> str:
    if os.environ.get('GITHUB_REF_NAME'):
        return os.environ['GITHUB_REF_NAME']

    result = subprocess.run(['git', '-C', str(ROOT), 'branch', '--show-current'], capture_output=True, text=True)

    return result.stdout.strip() or 'main'


def _skipped(path: Path) -> bool:
    return bool(SKIP_DIRS & set(path.relative_to(ROOT).parts))


def _packages(config) -> list[tuple[str, Path]]:
    """Each Python package under the configured source roots: (dotted name, folder)"""
    found = []

    for root in config['extra'].get('api_roots', []):
        base = ROOT / root

        for init in sorted(base.rglob('__init__.py')):
            folder = init.parent
            dotted = '.'.join(folder.relative_to(base).parts)

            if dotted and not _skipped(folder) and not SKIP_MODULE.search(folder.relative_to(ROOT).as_posix()):
                found.append((dotted, folder))

    return found


def _reference_page(dotted: str, folder: Path) -> str:
    modules = [dotted] + [f'{dotted}.{path.stem}' for path in sorted(folder.glob('*.py')) if path.stem != '__init__']
    blocks  = '\n\n'.join(f'::: {module}' for module in modules)

    return f'# {dotted}\n\nThe API of `{folder.relative_to(ROOT).as_posix()}`, generated from the code.\n\n{blocks}\n'


def on_config(config):
    """The API reference pages join the navigation, one per Python package"""
    pages = [{dotted: f'reference/{dotted.replace(".", "/")}.md'} for dotted, _ in _packages(config)]

    if pages:
        config['nav'].append({'API reference': pages})

    return config


def on_files(files, config):
    """Every Markdown file of the repository at its own path, so relative links work here as on GitHub; README.md is
    its folder's index page"""
    served = Files([])

    for path in sorted(ROOT.rglob('*')):
        if path.is_file() and not _skipped(path) and (path.suffix == '.md' or path.suffix.lower() in ASSETS):
            served.append(File(path.relative_to(ROOT).as_posix(), str(ROOT), config['site_dir'], config['use_directory_urls']))

    for dotted, folder in _packages(config):
        served.append(File.generated(config, f'reference/{dotted.replace(".", "/")}.md', content=_reference_page(dotted, folder)))

    return served


def on_page_markdown(markdown, page, config, files):
    """A link to a repository file that is not a page (code, config, a folder) goes to it on GitHub"""
    base   = (ROOT / page.file.src_uri).parent
    github = f'{config["repo_url"].rstrip("/")}/{{kind}}/{_branch()}/{{path}}'

    def rewrite(match):
        prefix, target, anchor, suffix = match.group(1), match.group(2), match.group(3) or '', match.group(4)

        if re.match(r'[a-z]+:', target) or page.file.generated_by is not None:
            return match.group(0)

        resolved = (base / target).resolve()

        if not resolved.exists() or ROOT not in resolved.parents or resolved.suffix == '.md' or resolved.suffix.lower() in ASSETS:
            return match.group(0)

        kind = 'tree' if resolved.is_dir() else 'blob'
        path = resolved.relative_to(ROOT).as_posix()

        return f'{prefix}{github.format(kind=kind, path=path)}{anchor}{suffix}'

    return LINK.sub(rewrite, markdown)
