#!/usr/bin/env python3
import sys
import json
import time
import base64
import struct
import asyncio
import argparse

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_prompts as prompts


USAGE = '''Answer the executor's press_judge and manipulation_recovery calls on the laptop (agent_backend:=push).

The executor keeps one TCP link to this bridge and pushes each call with its evidence and frames. The bridge keeps
each call in <local>/<call id>/ (call.json, the frames, bridge.json) and asks the model with the evidence as text and
the frames as images, no tools. A refused decision goes back to the model, at most twice. One warm model process waits
per kind of call, so a call starts without process startup. Run it in the venv that has claude-agent-sdk.'''

HEADER        = struct.Struct('!II')
PING_S        = 5.0
MAX_RETRIES   = 2
MODEL_TIMEOUT = 120.0
SYSTEMS       = {'press_judge': prompts.JUDGE_SYSTEM, 'manipulation_recovery': prompts.RECOVERY_SYSTEM}
KINDS         = [                                       # warmed at start: the executor's PHASE_DECISIONS
    ('press_judge', 'judge', ('pressed', 'not_pressed')),
    ('manipulation_recovery', 'diagnose', ('reposition', 'switch_arm', 'offset_retry', 'retry', 'ask_help', 'abort')),
    ('manipulation_recovery', 'stance_check', ('continue', 'reposition', 'abort')),
]


def main():
    parser = argparse.ArgumentParser(description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--listen', default='100.107.208.25', help='address to listen on: the laptop on Tailscale')
    parser.add_argument('--port', type=int, default=8771)
    parser.add_argument('--model', default='claude-opus-5-5')
    parser.add_argument('--effort', default='low')
    parser.add_argument('--local', type=Path, default=Path('~/agent_calls').expanduser())
    arguments = parser.parse_args()
    arguments.local.mkdir(parents=True, exist_ok=True)

    asyncio.run(Bridge(arguments).serve())


def schema(skill: str, decisions: tuple) -> dict:
    properties = {'decision': {'type': 'string', 'enum': list(decisions)}, 'reason': {'type': 'string'}}

    if skill == 'manipulation_recovery':
        properties.update({name: {'type': 'number'} for name in ('dx', 'dy', 'dyaw', 'ox', 'oy', 'oz')})
        properties['use_left_arm'] = {'type': 'boolean'}

    return {'type': 'object', 'properties': properties, 'required': ['decision', 'reason']}


def content(call: dict, images: dict) -> tuple[str, list[dict]]:
    """The prompt text, and the user message: the text, then each image after its label"""
    if call['skill'] == 'press_judge':
        text, names = prompts.judge_prompt(call, set(images)), prompts.judge_images(set(images))
    else:
        text, names = prompts.recovery_prompt(call, 'camera' in images), ['camera'] if 'camera' in images else []

    blocks = [{'type': 'text', 'text': text}]

    for name in names:
        media, data = images[name]
        blocks += [{'type': 'text', 'text': f'{name}:'},
                   {'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': base64.b64encode(data).decode()}}]

    return text, blocks


class Model:
    """One Claude Code process, connected before its call comes, used for one call and its retries, then closed"""

    def __init__(self, options: ClaudeAgentOptions):
        self.jobs = asyncio.Queue()
        self.task = asyncio.create_task(self._run(options))

    async def ask(self, message: str | list[dict]) -> tuple[dict | None, dict]:
        future = asyncio.get_running_loop().create_future()
        await self.jobs.put((message, future))

        return await asyncio.wait_for(future, MODEL_TIMEOUT)

    def close(self):
        self.task.cancel()

    async def _run(self, options: ClaudeAgentOptions):
        # The SDK client lives in the task that connected it
        async with ClaudeSDKClient(options) as client:
            while True:
                message, future = await self.jobs.get()

                try:
                    future.set_result(await self._ask(client, message))
                except Exception as error:
                    if not future.done():
                        future.set_exception(error)

    async def _ask(self, client: ClaudeSDKClient, message: str | list[dict]) -> tuple[dict | None, dict]:
        async def stream():
            yield {'type': 'user', 'message': {'role': 'user', 'content': message}, 'parent_tool_use_id': None}

        await client.query(message if isinstance(message, str) else stream())
        result = None

        async for reply in client.receive_response():
            if isinstance(reply, ResultMessage):
                result = reply

        usage = (result and result.usage) or {}
        meta  = {'cost_usd': result and result.total_cost_usd, 'model_s': result and round(result.duration_ms / 1000, 2),
                 'tokens_in': sum(usage.get(key) or 0 for key in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')),
                 'tokens_out': usage.get('output_tokens')}

        if result is None or result.is_error or not isinstance(result.structured_output, dict):
            return None, {**meta, 'error': str(result and (result.result or result.errors))[:300]}

        return result.structured_output, meta


class Bridge:
    def __init__(self, arguments):
        self.arguments = arguments
        self.writer    = None
        self.spares    = {}
        self.running   = {}                 # call id -> its task
        self.replies   = {}                 # call id -> the executor's replies
        self.last      = {}                 # call id -> the last message sent, sent again when the call comes again

    async def serve(self):
        for skill, phase, decisions in KINDS:
            self.spares[(skill, decisions)] = self._model(skill, decisions)

        server = await asyncio.start_server(self._on_connect, self.arguments.listen, self.arguments.port)
        print(f'listening on {self.arguments.listen}:{self.arguments.port}; {self.arguments.model} {self.arguments.effort}', flush=True)

        async with server:
            while True:
                await asyncio.sleep(PING_S)
                await self.send({'type': 'ping'})

    def _model(self, skill: str, decisions: tuple) -> Model:
        return Model(ClaudeAgentOptions(
            model=self.arguments.model, effort=self.arguments.effort, tools=[], system_prompt=SYSTEMS[skill],
            setting_sources=[], output_format={'type': 'json_schema', 'schema': schema(skill, decisions)},
            extra_args={'strict-mcp-config': None, 'no-session-persistence': None}, cwd=str(self.arguments.local),
        ))

    def _take(self, skill: str, decisions: tuple) -> Model:
        model = self.spares.pop((skill, decisions), None) or self._model(skill, decisions)
        self.spares[(skill, decisions)] = self._model(skill, decisions)      # warm for the next call of this kind

        return model

    async def send(self, header: dict):
        if self.writer is None:
            return

        data = json.dumps(header, default=str).encode()

        try:
            self.writer.write(HEADER.pack(len(data), 0) + data)
            await self.writer.drain()
        except (ConnectionError, RuntimeError):        # the executor sends the call again when it reconnects
            pass

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if self.writer is not None:
            self.writer.close()

        self.writer = writer
        print(f'executor connected from {writer.get_extra_info("peername")}', flush=True)

        try:
            while True:
                sizes  = HEADER.unpack(await reader.readexactly(HEADER.size))
                header = json.loads(await reader.readexactly(sizes[0]))
                binary = await reader.readexactly(sizes[1])
                await self._dispatch(header, binary, time.time())
        except (asyncio.IncompleteReadError, ConnectionError, json.JSONDecodeError) as error:
            print(f'executor link closed: {error.__class__.__name__}', flush=True)
        finally:
            if self.writer is writer:
                self.writer = None

    async def _dispatch(self, header: dict, binary: bytes, received: float):
        call = header.get('id')

        if header['type'] == 'call' and call in self.last:
            await self.send(self.last[call])
        elif header['type'] == 'call' and call not in self.running:
            self.replies[call] = asyncio.Queue()
            self.running[call] = asyncio.create_task(self._answer(header, binary, received))
        elif header['type'] == 'reply' and call in self.replies:
            await self.replies[call].put(header)
        elif header['type'] == 'cancel' and call in self.running:
            self.running[call].cancel()
            print(f'{call}: cancelled by the executor', flush=True)

    async def _answer(self, call: dict, binary: bytes, received: float):
        # (1) Keep the call and its frames
        folder, images, start = self.arguments.local / call['id'], {}, 0
        folder.mkdir(parents=True, exist_ok=True)

        for image in call.get('images', []):
            images[image['name']] = (image['media_type'], binary[start:start + image['size']])
            (folder / f'{image["name"]}.jpg').write_bytes(images[image['name']][1])
            start += image['size']

        (folder / 'call.json').write_text(json.dumps(call, indent=1, default=str))

        # (2) Ask the warm model; a refused decision goes back to it with the executor's reason
        decisions    = tuple(call['decisions'])
        model        = self._take(call['skill'], decisions)
        text, blocks = content(call, images)
        log          = {'call': call['id'], 'skill': call['skill'], 'phase': call.get('phase'), 'model': self.arguments.model,
                        'effort': self.arguments.effort, 'received': received, 'answers': []}
        await self.send({'type': 'started', 'id': call['id'], 'model': self.arguments.model, 'effort': self.arguments.effort, 'prompt': text})
        log['model_start'] = time.time()
        message = blocks

        try:
            for attempt in range(1 + MAX_RETRIES):
                decision, meta = await model.ask(message)

                if decision is None:
                    self.last[call['id']] = {'type': 'failed', 'id': call['id'], **meta}
                    await self.send(self.last[call['id']])
                    log['answers'].append({**meta, 'sent': time.time()})
                    break

                self.last[call['id']] = {'type': 'decision', 'id': call['id'], **decision, **meta}
                await self.send(self.last[call['id']])
                log['answers'].append({'decision': decision, **meta, 'sent': time.time()})
                reply = await self.replies[call['id']].get()
                log['answers'][-1]['reply'] = reply

                if reply.get('accepted'):
                    break

                message = f'The executor refused that decision: {reply.get("message")}. Decide again, same schema.'
        except asyncio.CancelledError:
            log['cancelled'] = time.time()
        except Exception as error:                      # the model process or a timeout: the executor fails the call
            self.last[call['id']] = {'type': 'failed', 'id': call['id'], 'error': f'{error.__class__.__name__}: {error}'[:300]}
            await self.send(self.last[call['id']])
            log['error'] = self.last[call['id']]['error']
        finally:
            model.close()
            self.running.pop(call['id'], None)
            self._log(folder, text, log)

    def _log(self, folder: Path, text: str, log: dict):
        first   = log['answers'][0] if log['answers'] else {}
        timings = {'receive_to_model_s': round(log['model_start'] - log['received'], 3),
                   'model_s': first.get('model_s'),
                   'receive_to_reply_s': round(first['sent'] - log['received'], 3) if first else None}
        (folder / 'bridge.json').write_text(json.dumps({**log, **timings, 'prompt': text}, indent=1, default=str))

        with (self.arguments.local / 'bridge_timing.jsonl').open('a') as file:
            file.write(json.dumps({'call': log['call'], 'skill': log['skill'], 'time': log['received'], **timings}) + '\n')

        answer = first.get('decision', {}).get('decision') or log.get('error') or first.get('error') or 'cancelled'
        print(f'{log["call"]} {log["skill"]}/{log["phase"]}: {answer}; receive->model {timings["receive_to_model_s"]} s, '
              f'model {timings["model_s"]} s, receive->reply {timings["receive_to_reply_s"]} s, '
              f'{len(log["answers"])} answers, ${(log["answers"][-1].get("cost_usd") if log["answers"] else 0) or 0:.3f} in all', flush=True)


if __name__ == '__main__':
    main()
