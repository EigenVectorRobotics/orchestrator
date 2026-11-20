from pathlib import Path

from ..lib.fingertip import fingertip_text, reference_images
from ..lib.text import fmt, panel_type


JUDGE_FRAMES = ('press', 'release', 'baseline')

JUDGE_SYSTEM = '''You judge one elevator button press by a humanoid robot. The question: is the requested button pressed \
now? Answer pressed or not_pressed.

Evidence you get
- Frames from the head camera, looking down at the panel. baseline: fingertip 0.10 m before the button, nothing \
pushed yet. press: fingertip at its deepest point; judge it first. release: about 0.5 s after the retreat began. \
A crop is a zoom around the tracked button. Each frame is attached as an image, right after its label.
- The fingertip. {fingertip}
- The panel type, stated in the call. Car panel: floor buttons ("3", "2", "*1"), each with its own light ring. \
Call panel (outside the car): the call button, with its own light.
- Arm numbers. They measure the arm against its goal, not against the panel, so they never decide alone.

Rules, in order
1. Both panels: the requested button's own light decides. Lit in press or release: pressed. Dark in both: \
not_pressed, wherever the fingertip sits.
2. A light already on in baseline means the call or floor is registered: pressed. A missing baseline does not weaken \
a lit button.
3. Only the requested button's own light counts. The card reader (a bar light left of the buttons that turns red or \
green), the floor display and other buttons are never evidence.
4. When no frame shows the requested button's own light (the button or the fingertip is out of view, or the press and \
release frames are missing): not_pressed. The recovery can press again; a wrong pressed leaves the task waiting.

Reason, three short sentences: (1) the requested button's light in baseline, press and release; (2) where the fingertip sits and in which frame; (3) the rule number that decided.'''


def judge_prompt(request: dict, frames: set) -> str:
    press   = request.get('press') or {}
    capture = press.get('capture') or {}
    button  = request.get('button', '')
    panel   = panel_type(button)
    shown   = judge_images(frames)
    missing = [name for name in JUDGE_FRAMES if name not in frames]
    lines   = [f'Button: {button}. Panel: {panel}.', f'Arm: {"left" if press.get("left_arm") else "right"}.',
               f'Frames attached below: {", ".join(shown) or "none"}.' + (f' Missing: {", ".join(missing)}.' if missing else '')]

    lines.append(f'Arm against its goal, not the panel: deepest {fmt(capture.get("max_depth_m"))} m along the button '
                 f'normal (0 is the goal, negative is short), {fmt(capture.get("lateral_at_max_m"))} m sideways there.')

    return '\n'.join(lines)


def judge_images(frames: set) -> list[str]:
    """The judge's images in order: each frame, then its crop"""
    return [name for frame in JUDGE_FRAMES for name in (frame, f'{frame}_crop') if name in frames]


def system(limits: dict) -> str:
    return JUDGE_SYSTEM.replace('{fingertip}', fingertip_text("this press's frames"))


def references() -> list[tuple[str, Path]]:
    """The operator-labelled fingertip references the bridge attaches before the frames (lib/fingertip.py)"""
    return reference_images()


def content(request: dict, images: set) -> tuple[str, list[str]]:
    """The prompt text, and the images it shows in order: each frame, then its crop"""
    return judge_prompt(request, images), judge_images(images)
