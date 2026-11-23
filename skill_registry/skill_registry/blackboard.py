"""SetBlackboard values: a copy {key}, a copy whose key is built from entries {{a}_b_{c}}, and text with {a} in it."""

import re


KEY_PART = re.compile(r'\{(\w+)\}')
COPY     = re.compile(r'\{\w+\}')
BUILT    = re.compile(r'\{(?:\w*\{\w+\})+\w*\}')


def value_kind(
    value: str,
) -> str:
    """copy for {key}, built for {{a}_b_{c}}, text for anything else"""
    if COPY.fullmatch(value):
        return 'copy'

    if BUILT.fullmatch(value):
        return 'built'

    return 'text'


def fill(
    template: str,
    blackboard: dict,
) -> str:
    """The template with each {key} replaced by the text of that blackboard entry"""
    return KEY_PART.sub(lambda match: str(blackboard[match.group(1)]), template)


def key_pattern(
    template: str,
) -> re.Pattern:
    """The keys that a built value {{a}_b_{c}} can name: each {a} matches any word"""
    pieces = KEY_PART.split(template[1:-1])

    return re.compile(''.join(re.escape(piece) if index % 2 == 0 else r'\w+' for index, piece in enumerate(pieces)))
