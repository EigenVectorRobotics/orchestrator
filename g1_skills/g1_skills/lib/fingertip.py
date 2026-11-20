"""What the fingertip is, for the agent skills that look for it in the camera frames (press_judge, manipulation_recovery).

FINGERTIP_TEXT goes into their system prompts. reference_images() are operator-labelled frames of the same gripper with
its fingertip circled; the agent bridge attaches them to each call of a skill whose prompt module offers them
(references()). Without this text a model asked for "the fingertip" took the end of the silver bar, about 45 mm from the
operator-marked tip in every try (offline probe, session 20261120T004319Z-eed273); with it and the marked estimate,
3 of 4 tries came within 8 mm.
"""
from pathlib import Path

REFERENCE_DIR = Path(__file__).with_name('fingertip_references')     # <name>.jpg, the fingertip circled in red

FINGERTIP_TEXT = ('The gripper is a Unitree Dex1, held closed. The point that presses is the tip of its upper, black '
                  'finger: the angled end above the silver bar. The silver bar with the black end cap below it is not '
                  'the fingertip; never take its end cap for it.')

REFERENCE_TEXT = ('Reference images, labelled by the operator: the same gripper, closed, near the panel, with its '
                  'fingertip circled in red. They are not this press.')


def fingertip_text(before: str) -> str:
    """FINGERTIP_TEXT, and REFERENCE_TEXT when there are references, attached before `before`"""
    if not reference_images():
        return FINGERTIP_TEXT

    return f'{FINGERTIP_TEXT} {REFERENCE_TEXT} They come before {before}.'


def reference_images() -> list[tuple[str, Path]]:
    """(name, path) of each reference image, sorted by name; none when the folder is empty or missing"""
    if not REFERENCE_DIR.is_dir():
        return []

    return [(f'reference_{path.stem}', path) for path in sorted(REFERENCE_DIR.glob('*.jpg'))]
