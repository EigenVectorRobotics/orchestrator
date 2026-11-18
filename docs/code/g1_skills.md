# g1_skills

The G1's skills, one folder each, and the code they share. `g1_skills/README.md` explains how a folder is made.

- **`<skill>/contract.yaml`**: Every skill: what it needs, does and how the executor reaches it.
- **`press_button/`**: The press action, and the three frames kept around it (capture.py).
- **`set_press_offset/`**: Sets the arm planner's press offset and keeps it for the press skills.
- **`button_in_view/`**: Seeds the tracker and judges the button's box and pose; picks the arm.
- **`check_reach/`**: Asks the IK check whether the arm reaches the tracked button, with the offset in force.
- **`press_judge/`**: Agent skill: the model judges the press frames, pressed or not_pressed.
- **`manipulation_recovery/`**: Agent skill: why a press failed and what next; its rules, stance evidence and prompt.
- **`operator_confirm/`**: Agent skill: a yes/no question for the operator in the bridge terminal.
- **`navigate_to/`**: Contract and the navigation experience log.
- **`lib/`**: Shared code: press state, tracking, prompt text.
