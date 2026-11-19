"""Text for agent prompts: the panel type of a button, and number formatting."""

def panel_type(button: str) -> str:
    if 'up' in button.split() or 'down' in button.split() or 'call' in button:
        return 'call panel, outside the car: the call button has its own light, rules 1-3'

    if 'floor' in button or any(char.isdigit() for char in button):
        return 'car panel: each button has its own light ring, rules 1-3'

    return 'unknown: decide from the frames whether the button has a ring'


def fmt(value) -> str:
    return f'{value:+.3f}' if isinstance(value, (int, float)) else str(value)


def vec(values) -> str:
    return '[' + ', '.join(fmt(value) for value in values) + ']'
