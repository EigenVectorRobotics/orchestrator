from pathlib import Path

from PIL import Image

from g1_skills.lib import fingertip
from g1_skills.manipulation_recovery import prompt as recovery_prompt, spec as recovery_spec
from g1_skills.press_judge import prompt as judge_prompt


def test_both_prompts_say_what_the_fingertip_is():
    assert fingertip.FINGERTIP_TEXT in judge_prompt.system({})
    assert fingertip.FINGERTIP_TEXT in recovery_prompt.recovery_system(recovery_spec.limits())
    assert 'not the end of the silver bar' in recovery_prompt.recovery_system(recovery_spec.limits())


def test_the_references_are_named_only_when_there_are_some(tmp_path, monkeypatch):
    monkeypatch.setattr(fingertip, 'REFERENCE_DIR', tmp_path / 'none')
    assert fingertip.reference_images() == [] and fingertip.REFERENCE_TEXT not in judge_prompt.system({})

    monkeypatch.setattr(fingertip, 'REFERENCE_DIR', tmp_path)
    for name in ('b', 'a'):
        Image.new('RGB', (8, 8)).save(tmp_path / f'{name}.jpg')

    assert [name for name, _ in recovery_prompt.references()] == ['reference_a', 'reference_b']
    assert [name for name, _ in judge_prompt.references()] == ['reference_a', 'reference_b']
    assert fingertip.REFERENCE_TEXT in judge_prompt.system({}) and "before this press's frames" in judge_prompt.system({})


def test_the_shipped_references_are_images():
    for _, path in fingertip.reference_images():
        assert Image.open(Path(path)).size[0] > 0


CAPTURE = {'max_depth_m': -0.004, 'residual_m': [-0.004, 0.012, -0.024], 'residual_from': 'encoders'}
FAILURE = {'skill': 'press_judge', 'status': 'failure', 'message': 'not_pressed'}


def test_with_aim_crops_the_residual_gives_only_the_depth():
    """y and z of the residual once undid move_to_pose's correction; with contact_aim they come from the image alone"""
    lines = '\n'.join(recovery_prompt.failure_lines(FAILURE, {'capture': CAPTURE}, [0.0, 0.0, 0.0],
                                                    recovery_spec.limits(), aim_shown=True))

    assert 'Residual-based ox' in lines and '+0.004' in lines
    assert '+0.012' not in lines and '-0.024' not in lines and '+0.024' not in lines
    assert 'take them from contact_aim' in lines


def test_without_aim_crops_the_residual_gives_the_whole_offset():
    lines = '\n'.join(recovery_prompt.failure_lines(FAILURE, {'capture': CAPTURE}, [0.0, 0.0, 0.0], recovery_spec.limits()))

    assert 'Residual-based offset' in lines and '+0.024' in lines and '-0.012' in lines


def test_with_crops_but_no_fingertip_the_rules_keep_y_and_z():
    assert 'keep oy and oz of the offset in force' in recovery_prompt.recovery_system(recovery_spec.limits())
