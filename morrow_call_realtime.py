"""Authenticated, interruptible call transport. No credentials or chat writes.

LLM SSE -> validated English/Chinese sentence -> streamed ElevenLabs PCM.
One speech task per connection. Interrupt/disconnect closes both upstream streams.
Only playback-acknowledged sentences are carried into later call turns.
"""
import asyncio
import base64
import json
import os
import re
import time
from contextlib import suppress

import httpx
from aiohttp import WSMsgType
from morrow_call import CallError, bilingual, identifier


class SentenceStream:
    """Decode JSON objects across arbitrary SSE token boundaries."""
    def __init__(self):
        self.buffer = ''
        self.count = 0

    def feed(self, text, final=False):
        self.buffer += text
        if len(self.buffer) > 12000:
            raise ValueError('Oversized sentence stream')
        output = []
        while self.buffer.strip():
            self.buffer = self.buffer.lstrip()
            if self.buffer.startswith('```'):
                if '\n' not in self.buffer:
                    if final and self.buffer.strip() == '```':
                        self.buffer = ''
                        break
                    break
                self.buffer = self.buffer.split('\n', 1)[1]
                continue
            try:
                value, end = json.JSONDecoder().raw_decode(self.buffer)
            except json.JSONDecodeError:
                if final:
                    raise ValueError('Incomplete bilingual sentence') from None
                break
            # Accept the old wrapper if a provider follows the old persona formatting.
            segments = bilingual(value if isinstance(value, dict) and 'segments' in value
                                 else {'segments': [value]})
            self.count += len(segments)
            if self.count > 3:
                raise ValueError('Too many sentences')
            output.extend(segments)
            self.buffer = self.buffer[end:]
        if final and not self.count:
            raise ValueError('No bilingual sentence')
        return output


async def sentence_stream(service, text, greeting, session):
    bot, owner = service.heartbeat.bot, service.gateway.owner
    messages = await service.messages(text, greeting, session, streaming=True)
    decoder = SentenceStream()
    timeout = httpx.Timeout(60, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream('POST', bot.LLMHUB_BASE_URL + '/chat/completions',
                                 headers=bot.api_headers(),
                                 json={'model': bot.current_model(owner), 'messages': messages,
                                       'stream': True, 'max_tokens': 700}) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith('data:'):
                    continue
                payload = line[5:].strip()
                if payload == '[DONE]':
                    break
                if not payload:
                    continue
                data = json.loads(payload)
                if data.get('error'):
                    raise ValueError('Provider stream error')
                choices = data.get('choices', [])
                if not choices:
                    continue
                content = choices[0].get('delta', {}).get('content', '')
                if isinstance(content, list):
                    content = ''.join(x.get('text', '') for x in content if isinstance(x, dict))
                if isinstance(content, str) and content:
                    for segment in decoder.feed(content):
                        yield segment
    for segment in decoder.feed('', final=True):
        yield segment


async def pcm_stream(text):
    key, voice = os.getenv('ELEVENLABS_API_KEY', ''), os.getenv('ELEVENLABS_VOICE_ID', '')
    if not key or not re.fullmatch('[A-Za-z0-9_-]{1,100}', voice):
        raise CallError(503, 'ElevenLabs 音色尚未配置。')
    total, carry = 0, b''
    async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=15)) as client:
        async with client.stream('POST', 'https://api.elevenlabs.io/v1/text-to-speech/' + voice + '/stream',
                                 params={'output_format': 'pcm_24000'}, headers={'xi-api-key': key},
                                 json={'text': text, 'model_id': os.getenv('ELEVENLABS_CALL_TTS_MODEL', 'eleven_flash_v2_5'),
                                       'language_code': 'en'}) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > 3_000_000:
                    raise ValueError('Audio exceeds limit')
                carry += chunk
                # Bound socket messages and never split an Int16 sample.
                while len(carry) >= 2:
                    length = min(len(carry) // 2 * 2, 16384)
                    yield carry[:length]
                    carry = carry[length:]
    if carry or total == 0:
        raise ValueError('Incomplete PCM audio')


class RealtimeCall:
    def __init__(self, service, socket):
        self.service, self.socket = service, socket
        self.task = None
        self.current = None
        self.states = {}
        self.persistence = asyncio.Lock()
        self.sentences = lambda text, greeting, session: sentence_stream(service, text, greeting, session)
        self.pcm = pcm_stream
        self.session = None

    async def emit(self, kind, request_id=None, **fields):
        await self.socket.send_json({'type': kind, 'request_id': request_id, **fields})

    async def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.task = None
        self.current = None

    async def persist(self, state):
        async with self.persistence:
            await asyncio.to_thread(self.service.gateway.store.finish, state['id'],
                                    state['result'], state['status'])

    async def played(self, data):
        if set(data) != {'type', 'request_id', 'segment'}:
            raise CallError(400, '播放回执不正确。')
        rid = identifier(data.get('request_id'))
        index = data.get('segment')
        state = self.states.get(rid)
        if not state or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(state['result']['segments']):
            raise CallError(400, '播放回执不正确。')
        indices = state['result']['played_indices']
        if index not in indices:
            indices.append(index)
            indices.sort()
            await self.persist(state)

    async def turn(self, data):
        if set(data) - {'type', 'request_id', 'session_id', 'text', 'greeting'}:
            raise CallError(400, '通话内容格式不正确。')
        rid, session = identifier(data.get('request_id')), identifier(data.get('session_id'))
        text, greeting = data.get('text', ''), data.get('greeting', False)
        if not isinstance(text, str) or len(text) > 1500 or not isinstance(greeting, bool) or (greeting and text) or (not greeting and not text.strip()):
            raise CallError(400, '请说一段不超过1500字的话。')
        if self.session and self.session != session:
            raise CallError(409, '这一通电话的编号不能更换。')
        self.session = session
        receipt = 'call:' + session + ':' + ('greeting' if greeting else text.strip())
        old = await asyncio.to_thread(self.service.gateway.store.get, rid)
        if old:
            raise CallError(409, '这句已处理或被打断，请继续说下一句。')
        gateway, heartbeat = self.service.gateway, self.service.heartbeat
        lock = heartbeat.bot.chat_locks[gateway.owner] if heartbeat else None
        if lock is None:
            raise CallError(503, '伴侣通话暂未连接。')
        if gateway.lock.locked() or lock.locked():
            raise CallError(429, '伴侣正在回复，请稍后再说。')
        async with gateway.lock, lock:
            await asyncio.to_thread(gateway.store.begin, rid, receipt)
            state = {'id': rid, 'status': 'processing',
                     'result': {'request_id': rid, 'session_id': session, 'segments': [], 'played_indices': []}}
            self.states[rid] = state
            while len(self.states) > 20:
                self.states.pop(next(iter(self.states)))
            producer = None
            try:
                await asyncio.to_thread(heartbeat.memory.set_setting, gateway.owner,
                                        'proactive_last_user_at', str(time.time()))
                await self.emit('thinking', rid)
                queue = asyncio.Queue(maxsize=3)

                async def produce():
                    try:
                        async for segment in self.sentences(text.strip(), greeting, session):
                            await queue.put(segment)
                    except Exception as exc:
                        await queue.put(exc)
                    finally:
                        # Cancellation must not block on a full queue.
                        if not asyncio.current_task().cancelling():
                            await queue.put(None)

                producer = asyncio.create_task(produce())
                async with asyncio.timeout(150):
                    while True:
                        segment = await queue.get()
                        if segment is None:
                            break
                        if isinstance(segment, Exception):
                            raise segment
                        index = len(state['result']['segments'])
                        state['result']['segments'].append(segment)
                        await self.persist(state)
                        await self.emit('caption', rid, segment=index, **segment)
                        audio_ok = True
                        try:
                            async for chunk in self.pcm(segment['en']):
                                await self.emit('audio', rid, segment=index, audio_base64=base64.b64encode(chunk).decode())
                        except Exception as exc:
                            audio_ok = False
                            print('Realtime call audio unavailable: ' + type(exc).__name__, flush=True)
                            await self.emit('audio_error', rid, segment=index,
                                            message='这句声音暂不可用，中文和英文字幕已保留。')
                        await self.emit('audio_end', rid, segment=index, complete=audio_ok)
                if not state['result']['segments']:
                    raise ValueError('Empty call stream')
                state['status'] = 'done'
                await self.persist(state)
                await self.emit('done', rid)
            except asyncio.CancelledError:
                state['status'] = 'interrupted'
                await self.persist(state)
                raise
            except Exception as exc:
                state['status'] = 'interrupted'
                await self.persist(state)
                if isinstance(exc, CallError):
                    raise
                print('Realtime call unavailable: ' + type(exc).__name__, flush=True)
                raise CallError(503, '这句连接中断了，可以继续说下一句。') from None
            finally:
                if producer:
                    producer.cancel()
                    with suppress(asyncio.CancelledError):
                        await producer

    async def perform(self, data):
        try:
            await self.turn(data)
        except CallError as exc:
            if not self.socket.closed:
                await self.emit('error', data.get('request_id'), message=exc.message, status=exc.status)
        except (ConnectionError, RuntimeError):
            pass  # Socket disconnect; cancellation/finally retains interrupted receipt.
        except Exception as exc:
            print('Realtime call event failed: ' + type(exc).__name__, flush=True)
            if not self.socket.closed:
                await self.emit('error', data.get('request_id'), message='通话事件没有接上，可以继续说下一句。')

    async def run(self):
        try:
            await self.emit('ready', language='en', input_language='zh-CN', sample_rate=24000)
            async for message in self.socket:
                if message.type != WSMsgType.TEXT:
                    break
                data = None
                try:
                    data = json.loads(message.data)
                    if not isinstance(data, dict):
                        raise CallError(400, '通话事件不正确。')
                    kind = data.get('type')
                    if kind == 'turn':
                        await self.cancel()
                        self.current = data.get('request_id')
                        self.task = asyncio.create_task(self.perform(data))
                    elif kind == 'interrupt':
                        if set(data) != {'type', 'request_id'}:
                            raise CallError(400, '打断事件不正确。')
                        rid = identifier(data['request_id'])
                        if rid == self.current:
                            await self.cancel()
                            await self.emit('interrupted', rid)
                    elif kind == 'played':
                        await self.played(data)
                    elif kind == 'ping' and set(data) == {'type'}:
                        if self.service.heartbeat:
                            await asyncio.to_thread(self.service.heartbeat.memory.set_setting,
                                                    self.service.gateway.owner, 'proactive_last_user_at', str(time.time()))
                    elif kind == 'end' and set(data) == {'type'}:
                        break
                    else:
                        raise CallError(400, '通话事件不正确。')
                except (ValueError, TypeError, KeyError) as exc:
                    text = exc.message if isinstance(exc, CallError) else '通话事件不正确。'
                    rid = data.get('request_id') if isinstance(data, dict) else None
                    await self.emit('error', rid, message=text)
        finally:
            await self.cancel()
            await self.socket.close()
