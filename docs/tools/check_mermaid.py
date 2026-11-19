import re
import sys
import json

from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT      = Path(__file__).resolve().parents[2]
SKIP_DIRS = {'.git', 'build', 'install', 'log', 'site', 'node_modules', '.pytest_cache', '__pycache__', '.claude'}
BLOCK     = re.compile(r'^```mermaid\n(.*?)^```', re.M | re.S)
MERMAID   = 'https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js'


def blocks() -> list[tuple[str, int, str]]:
    found = []

    for path in sorted(ROOT.rglob('*.md')):
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue

        text = path.read_text()

        for match in BLOCK.finditer(text):
            found.append((path.relative_to(ROOT).as_posix(), text[:match.start()].count('\n') + 1, match.group(1)))

    return found


def main() -> int:
    diagrams = blocks()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome' if '--chrome' in sys.argv else None)
        page    = browser.new_page()
        page.set_content(f'<script src="{MERMAID}"></script>')
        page.wait_for_function('window.mermaid !== undefined')

        errors = page.evaluate('''async (sources) => {
            const errors = [];
            for (const [index, source] of sources.entries()) {
                try { await mermaid.parse(source); } catch (error) { errors.push([index, String(error.message || error)]); }
            }
            return errors;
        }''', [source for _, _, source in diagrams])
        browser.close()

    for index, message in errors:
        path, line, _ = diagrams[index]
        print(f'{path}:{line}: {message.splitlines()[0]}')

    print(json.dumps({'diagrams': len(diagrams), 'errors': len(errors)}))

    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
