"""The knowledge store from a shell: for the operator, and for an agent session outside the executor.

    knowledge list [--tier belief] [--about KIND=NAME ...] [--ready]
    knowledge show C-0007
    knowledge note "TEXT" --by agent:press_judge --evidence record:3 [--run ID] [--about place=P ...] [--key K --value V]
    knowledge confirm C-0007 --by operator --evidence E-019 [--run ID] [--value V]
    knowledge contradict C-0007 --by operator --evidence ... [--run ID]
    knowledge approve C-0007 [--text "the exact text"]       the operator only, at a terminal
    knowledge refuse C-0007 --reason "..."
    knowledge retire C-0007 --reason "..."
    knowledge prompt                                          the facts and beliefs as prompt text
    knowledge seed FILE [--by person:experience_log]          load claims written by hand (config/knowledge_seed.yaml)

--dir (default ~/orch_knowledge) holds the claims and the log. A value is read as JSON when it can be
(0.012, [0.005, 0.0075, 0.0034], true), else as a word.
"""

import sys
import json
import time
import argparse

import yaml

from dataclasses import asdict

from .knowledge import OPERATOR, TIERS, KnowledgeError, KnowledgeStore, Sighting, about_of


def _value(text: str | None):
    if text is None:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _tag(text: str) -> tuple[str, str]:
    if '=' not in text:
        raise argparse.ArgumentTypeError(f'a tag is kind=name, not {text!r}')

    return tuple(text.split('=', 1))


def _about(tags: list[tuple[str, str]]) -> dict:
    kinds = [kind for kind, _ in tags]

    if len(set(kinds)) != len(kinds):
        raise KnowledgeError(f'one name per tag kind, not {kinds}')

    return dict(tags)


def _sighting(arguments) -> Sighting:
    return Sighting(by=arguments.by, time=time.time(), run=arguments.run, evidence=arguments.evidence, value=_value(arguments.value))


def _row(claim) -> str:
    ready = '  READY' if claim.ready_for_fact() else ''
    value = f' = {claim.value}' if claim.key else ''

    return (f'{claim.id}  {claim.tier:<11} +{len(claim.confirmations)}/-{len(claim.contradictions)} '
            f'r={claim.reliability():.2f}{ready}  ({claim.subject()}) {claim.text}{value}')


def seed(store: KnowledgeStore, path: str, by: str) -> list[str]:
    """Each claim in the file as an observation by a person, with its evidence and no run, so it counts as one
    source. A claim whose text a live claim already has is skipped. Returns one line per claim"""
    entries = yaml.safe_load(open(path)) or []
    known   = {claim.text for claim in store.claims(('observation', 'belief', 'fact'))}
    lines   = []

    for entry in entries:
        text = ' '.join(str(entry['text']).split())

        if not entry.get('evidence'):
            raise KnowledgeError(f'a seeded claim needs evidence: {text}')

        if text in known:
            lines.append(f'skipped (already in the store): {text}')
            continue

        sighting = Sighting(by=by, time=time.time(), evidence=list(entry['evidence']), value=entry.get('value'))
        claim, change = store.note(text, sighting, entry.get('about'), entry.get('key'), entry.get('valid_for_s'))
        known.add(claim.text)
        lines.append(f'{claim.id}: {change} ({claim.subject()}) {claim.text}')

    return lines


def _confirm_at_terminal(claim_id: str):
    """Approval is the operator's: it needs a person at a terminal who types the id again"""
    if not sys.stdin.isatty():
        raise KnowledgeError('approve needs the operator at a terminal; an agent session cannot approve')

    if input(f'Type {claim_id} to approve it as a fact: ').strip() != claim_id:
        raise KnowledgeError('not approved')


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(prog='knowledge', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dir', default='~/orch_knowledge')
    commands = parser.add_subparsers(dest='command', required=True)

    listing = commands.add_parser('list')
    listing.add_argument('--tier', action='append')
    listing.add_argument('--about', action='append', default=[], type=_tag, help='kind=name, e.g. place=cabin; all must match')
    listing.add_argument('--ready', action='store_true', help='only beliefs the operator can approve')

    commands.add_parser('show').add_argument('id')
    commands.add_parser('prompt')

    seeding = commands.add_parser('seed')
    seeding.add_argument('file')
    seeding.add_argument('--by', default='person:experience_log', help='who wrote the claims')

    def sighting_arguments(command):
        command.add_argument('--by', required=True, help=f'{OPERATOR}, agent:<skill>, check:<skill>')
        command.add_argument('--evidence', action='append', required=True)
        command.add_argument('--run')
        command.add_argument('--value')

    note = commands.add_parser('note')
    note.add_argument('text')
    note.add_argument('--about', action='append', default=[], type=_tag, help='kind=name, e.g. place=cabin or device=thor')
    note.add_argument('--key')
    note.add_argument('--valid-for-s', type=float)
    sighting_arguments(note)

    for name in ('confirm', 'contradict'):
        command = commands.add_parser(name)
        command.add_argument('id')
        sighting_arguments(command)

    approve = commands.add_parser('approve')
    approve.add_argument('id')
    approve.add_argument('--text', help='the exact text of the fact, if it changes')

    for name in ('refuse', 'retire'):
        command = commands.add_parser(name)
        command.add_argument('id')
        command.add_argument('--reason', required=True)

    arguments = parser.parse_args(argv)
    store     = KnowledgeStore(arguments.dir)

    try:
        if arguments.command == 'list':
            claims = store.claims(tuple(arguments.tier or TIERS))
            tags   = about_of(_about(arguments.about)).items()
            claims = [c for c in claims if tags <= c.about.items()
                      and (not arguments.ready or c.ready_for_fact())]
            print('\n'.join(map(_row, claims)) or 'no claims')
        elif arguments.command == 'show':
            claim = store.get(arguments.id)
            print(_row(claim))
            print(json.dumps({**asdict(claim), 'reliability': claim.reliability(), 'value_stats': claim.value_stats()}, indent=2, default=str))
        elif arguments.command == 'prompt':
            print(store.prompt_section() or 'no facts or beliefs yet')
        elif arguments.command == 'seed':
            print('\n'.join(seed(store, arguments.file, arguments.by)) or 'no claims in the file')
        elif arguments.command == 'note':
            claim, change = store.note(arguments.text, _sighting(arguments), _about(arguments.about),
                                       arguments.key, arguments.valid_for_s)
            print(f'{claim.id}: {change} ({claim.tier})')
        elif arguments.command in ('confirm', 'contradict'):
            change = getattr(store, arguments.command)(arguments.id, _sighting(arguments))
            print(f'{arguments.id}: {change}')
        elif arguments.command == 'approve':
            claim = store.get(arguments.id)
            claim.check_ready()                     # before the prompt; approve checks again under the lock

            print(_row(claim))
            print(f'Fact text: {arguments.text or claim.text}')
            _confirm_at_terminal(claim.id)
            print(_row(store.approve(claim.id, OPERATOR, arguments.text)))
        elif arguments.command == 'refuse':
            store.refuse(arguments.id, OPERATOR, arguments.reason)
            print(f'{arguments.id}: refused, stays a belief')
        elif arguments.command == 'retire':
            store.retire(arguments.id, OPERATOR, arguments.reason)
            print(f'{arguments.id}: retired')
    except KnowledgeError as error:
        sys.exit(f'knowledge: {error}')


if __name__ == '__main__':
    main()
