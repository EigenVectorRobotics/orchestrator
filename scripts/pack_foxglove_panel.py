"""Pack foxglove/orchestrator_bt_panel into a .foxe file, and optionally install it into the local Foxglove desktop app.

    python3 scripts/pack_foxglove_panel.py [--install]
"""
import argparse
import json
import shutil
import zipfile
from pathlib import Path

PANEL = Path(__file__).resolve().parents[1] / 'foxglove' / 'orchestrator_bt_panel'
FILES = ['package.json', 'dist/extension.js', 'README.md']


def main() -> None:
    """Write <publisher>.<name>-<version>.foxe beside the panel; with --install, copy it unpacked to ~/.foxglove-studio/extensions."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    meta = json.loads((PANEL / 'package.json').read_text())
    stem = f'{meta["publisher"]}.{meta["name"]}-{meta["version"]}'
    foxe = PANEL / f'{stem}.foxe'
    with zipfile.ZipFile(foxe, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(PANEL / name, name)
    print(foxe)
    if args.install:
        target = Path.home() / '.foxglove-studio' / 'extensions' / stem
        shutil.rmtree(target, ignore_errors=True)
        for name in FILES:
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(PANEL / name, target / name)
        print(target)


if __name__ == '__main__':
    main()
