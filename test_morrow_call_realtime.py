import asyncio
import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from ios_api import Gateway, Store, call_messages, create_web_app
from morrow_call import CallService
from morrow_call_realtime import RealtimeCall, SentenceStream, pcm_stream, sentence_stream
from test_ios_api import FakeStore


class CallHistoryTests(unittest.TestCase):
    def testPlaybackReceiptsExcludeUnheardReplyAndPreserveChineseUserInput(self):
        rows = [{'user_text': 'call:session:你好', 'result': {
            'segments': [{'en': 'Hello.', 'zh': '你好。'}, {'en': 'Unheard.', 'zh': '没听到。'}],
            'played_indices': [0]}}]
        self.assertEqual(call_messages(rows), [{'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': 'Hello.'}])
        rows[0]['result']['played_indices'] = []
        self.assertEqual(len(call_messages(rows)), 1)

    def testOldChatCallsAreHiddenWithoutDeletingOrdinaryChat(self):
        cursor = MagicMock(); cursor.__enter__.return_value = cursor; cursor.fetchall.return_value = []
        conn = MagicMock(); conn.__enter__.return_value = conn; conn.cursor.return_value = cursor
        memory = MagicMock(); memory._connect.return_value = conn
        Store(memory, 123).history()
        sql, params = cursor.execute.call_args.args
        self.assertIn('NOT EXISTS', sql)
        self.assertIn("r.user_text LIKE 'call:%%'", sql)
        self.assertIn('c.telegram_message_id=4611686018427387904+r.id', sql)
        self.assertEqual(params, (123,))

    def testCallContextIsScopedToOwnerAndSession(self):
        cursor = MagicMock(); cursor.__enter__.return_value = cursor; cursor.fetchall.return_value = []
        conn = MagicMock(); conn.__enter__.return_value = conn; conn.cursor.return_value = cursor
        memory = MagicMock(); memory._connect.return_value = conn
        Store(memory, 123).call_history('known-session')
        sql, params = cursor.execute.call_args.args
        self.assertIn('chat_id=%s', sql)
        self.assertEqual(params, (123, 'call:known-session:%'))

    def testSentenceArrivesBeforeEntireReplyWithFragmentedJSON(self):
        parser = SentenceStream()
        self.assertFalse(parser.feed('{"en":"Hel'))
        first = parser.feed('lo.","zh":"你好。"}\n{"en":')
        self.assertEqual(first, [{'en': 'Hello.', 'zh': '你好。'}])
        self.assertEqual(parser.feed('"How are you?","zh":"你好吗？"}'), [{'en': 'How are you?', 'zh': '你好吗？'}])
        self.assertFalse(parser.feed('', final=True))
        for invalid in ['{"en":"中文","zh":"中文"}', '{"en":"Hello","zh":"Hello"}', 'not-json']:
            with self.assertRaises(ValueError): SentenceStream().feed(invalid, final=True)


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = patch.dict(os.environ, {'ELEVENLABS_API_KEY': 'private-key', 'ELEVENLABS_VOICE_ID': 'private-voice'})
        self.config.start()
        self.store = FakeStore()
        self.memory = SimpleNamespace(set_setting=MagicMock(), save_recent_turn=MagicMock(), load_recent_messages=lambda *a: [])
        self.bot = SimpleNamespace(chat_locks={123: asyncio.Lock()}, build_system_prompt=lambda *a: 'persona',
                                   LLMHUB_BASE_URL='https://model.example/v1', current_model=lambda *a: 'existing-model', api_headers=lambda: {})
        self.heartbeat = SimpleNamespace(bot=self.bot, memory=self.memory)
        self.gateway = Gateway(owner=123, handler=AsyncMock(), store=self.store, token='a'*32)
        self.client = TestClient(TestServer(create_web_app(self.gateway, heartbeat=self.heartbeat)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer ' + 'a'*32}
        self.request = {'type': 'turn', 'request_id': str(uuid4()), 'session_id': str(uuid4()), 'text': '今天有点累'}
        self.generated = 0

    async def asyncTearDown(self):
        await self.client.close()
        self.config.stop()

    async def sentences(self, service, text, greeting, session):
        self.generated += 1
        yield {'en': 'Take your time.', 'zh': '慢慢说。'}

    async def audio(self, text):
        yield b'\x00\x00\xff\x7f'

    async def connect(self):
        socket = await self.client.ws_connect('/v1/call/realtime', headers=self.headers)
        self.assertEqual((await socket.receive_json())['type'], 'ready')
        return socket

    async def until(self, socket, kind):
        events = []
        async with asyncio.timeout(3):
            while True:
                event = await socket.receive_json(); events.append(event)
                if event['type'] == kind: return events

    async def testStreamingFirstAudioCancellationAndNextTurnReleaseLocks(self):
        stopped, hold = asyncio.Event(), asyncio.Event()
        async def streaming(service, text, greeting, session):
            try:
                yield {'en': 'I am here.', 'zh': '我在这里。'}
                if text == self.request['text']: await hold.wait()
            finally: stopped.set()
        with patch('morrow_call_realtime.sentence_stream', streaming), patch('morrow_call_realtime.pcm_stream', self.audio):
            socket = await self.connect()
            await socket.send_json(self.request)
            events = await self.until(socket, 'audio_end')
            self.assertEqual([x['type'] for x in events], ['thinking', 'caption', 'audio', 'audio_end'])
            self.assertEqual(base64.b64decode(events[2]['audio_base64']), b'\x00\x00\xff\x7f')
            await socket.send_json({'type':'played','request_id':self.request['request_id'],'segment':0})
            await socket.send_json({'type':'interrupt','request_id':self.request['request_id']})
            await self.until(socket, 'interrupted')
            self.assertTrue(stopped.is_set())
            receipt = self.store.get(self.request['request_id'])
            self.assertEqual(receipt['status'], 'interrupted')
            self.assertEqual(receipt['result']['played_indices'], [0])
            self.assertFalse(self.gateway.lock.locked())
            self.assertFalse(self.bot.chat_locks[123].locked())
            await socket.send_json({**self.request, 'request_id': str(uuid4()), 'text': '换一句'})
            await self.until(socket, 'done')
            self.memory.save_recent_turn.assert_not_called()
            await socket.close()

    async def testCompletedReceiptReplayCannotRebillAndAckIsDurable(self):
        with patch('morrow_call_realtime.sentence_stream', self.sentences), patch('morrow_call_realtime.pcm_stream', self.audio):
            socket = await self.connect(); await socket.send_json(self.request)
            await self.until(socket, 'done')
            await socket.send_json({'type':'played', 'request_id':self.request['request_id'],'segment':0})
            await socket.send_json(self.request)
            error = (await self.until(socket, 'error'))[-1]
            self.assertEqual(error['status'], 409)
            self.assertEqual(self.generated, 1)
            self.assertEqual(self.store.get(self.request['request_id'])['result']['played_indices'], [0])
            self.memory.save_recent_turn.assert_not_called(); await socket.close()

    async def testInterruptCancelsInFlightVoiceAndNextTurnUsesOnlyPlayedContext(self):
        closed, waiting = asyncio.Event(), asyncio.Event()
        self.store.call_history = lambda session: call_messages([
            row for row in self.store.rows.values()
            if row['user_text'].startswith('call:' + session + ':') and row['status'] in ('done', 'interrupted')])
        async def voice(text):
            try:
                yield b'\x00\x00'
                await waiting.wait()
            finally: closed.set()
        with patch('morrow_call_realtime.sentence_stream', self.sentences), patch('morrow_call_realtime.pcm_stream', voice):
            socket = await self.connect(); await socket.send_json(self.request)
            await self.until(socket, 'audio')
            await socket.send_json({'type':'interrupt','request_id':self.request['request_id']})
            await self.until(socket, 'interrupted')
            self.assertTrue(closed.is_set())
            service = CallService(self.gateway, self.heartbeat)
            messages = await service.messages('再说一句', False, self.request['session_id'], streaming=True)
            self.assertIn({'role':'user','content':self.request['text']}, messages)
            self.assertNotIn({'role':'assistant','content':'Take your time.'}, messages)
            self.assertEqual(messages[-1], {'role':'user','content':'再说一句'})
            await socket.close()

    async def testAudioFailureKeepsCaptionAndConnectionAcceptsNextUtterance(self):
        async def failure(text):
            raise RuntimeError('private-key')
            yield b''
        with patch('morrow_call_realtime.sentence_stream', self.sentences), patch('morrow_call_realtime.pcm_stream', failure):
            socket = await self.connect(); await socket.send_json(self.request)
            events = await self.until(socket, 'done')
            self.assertIn('audio_error', [x['type'] for x in events])
            self.assertNotIn('private-key', str(events))
            self.assertEqual(self.store.get(self.request['request_id'])['result']['segments'][0]['zh'], '慢慢说。')
            self.assertEqual(self.store.get(self.request['request_id'])['result']['played_indices'], [])
            await socket.send_json({**self.request,'request_id':str(uuid4()),'text':'我继续说'})
            await self.until(socket, 'done'); await socket.close()

    async def testDisconnectClosesUpstreamAndPersistsInterruptedReceipt(self):
        started, stopped, hold = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def pending(service, text, greeting, session):
            try:
                started.set(); await hold.wait(); yield {'en':'Hello.','zh':'你好。'}
            finally: stopped.set()
        with patch('morrow_call_realtime.sentence_stream', pending):
            socket = await self.connect(); await socket.send_json(self.request)
            await started.wait(); await socket.close()
            await asyncio.wait_for(stopped.wait(), 2)
            for _ in range(100):
                if self.store.get(self.request['request_id'])['status'] == 'interrupted': break
                await asyncio.sleep(0.01)
            self.assertEqual(self.store.get(self.request['request_id'])['status'], 'interrupted')

    async def testAuthSingleCallMalformedEventsAndNoProviderOverrides(self):
        with self.assertRaises(WSServerHandshakeError) as denied: await self.client.ws_connect('/v1/call/realtime')
        self.assertEqual(denied.exception.status, 401)
        socket = await self.connect()
        with self.assertRaises(WSServerHandshakeError) as busy: await self.client.ws_connect('/v1/call/realtime', headers=self.headers)
        self.assertEqual(busy.exception.status, 409)
        for payload in ['not-json', json.dumps({**self.request,'model':'override'}), json.dumps({'type':'played','request_id':str(uuid4()),'segment':False})]:
            await socket.send_str(payload)
            await self.until(socket, 'error')
        self.assertFalse(self.store.rows); await socket.close()

    async def testProvidersActuallyUseSSEAndPCMBeforeCompletion(self):
        requests = []
        async def handler(request):
            requests.append(request)
            if 'chat/completions' in str(request.url):
                pieces = ['{"en":"Hello.",', '"zh":"你好。"}']
                body = ''.join('data: ' + json.dumps({'choices':[{'delta':{'content':x}}]}) + '\n\n' for x in pieces) + 'data: [DONE]\n\n'
                return httpx.Response(200, text=body)
            return httpx.Response(200, content=b'\x00\x00\xff\x7f')
        real_client = httpx.AsyncClient
        def client(**kwargs): return real_client(transport=httpx.MockTransport(handler), **kwargs)
        service = CallService(self.gateway, self.heartbeat)
        with patch('morrow_call_realtime.httpx.AsyncClient', client):
            words = [x async for x in sentence_stream(service, '你好', False, self.request['session_id'])]
            audio = b''.join([x async for x in pcm_stream(words[0]['en'])])
        sent = json.loads(requests[0].content)
        self.assertTrue(sent['stream']); self.assertEqual(sent['model'], 'existing-model')
        self.assertIn('用户说中文', sent['messages'][0]['content'])
        self.assertIn('pcm_24000', str(requests[1].url))
        self.assertEqual(json.loads(requests[1].content)['language_code'], 'en')
        self.assertEqual(audio, b'\x00\x00\xff\x7f')


if __name__ == '__main__': unittest.main()
