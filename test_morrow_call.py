import asyncio
import base64
import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer
from ios_api import Gateway, create_web_app
from test_ios_api import FakeStore
from morrow_call import CallError, CallService, bilingual


class CallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = patch.dict(os.environ, {'ELEVENLABS_API_KEY': 'private-key', 'ELEVENLABS_VOICE_ID': 'private-voice'})
        self.config.start()
        self.memory = SimpleNamespace(set_setting=lambda *args: None, save_recent_turn=lambda *args: self.saved.append(args))
        self.saved = []
        self.bot = SimpleNamespace(chat_locks={123: asyncio.Lock()})
        self.heartbeat = SimpleNamespace(bot=self.bot, memory=self.memory)
        self.gateway = Gateway(owner=123, handler=AsyncMock(), store=FakeStore(), token='a'*32)
        self.service = CallService(self.gateway, self.heartbeat)
        self.service.generate = AsyncMock(return_value=[{'en': "I'm here.", 'zh': '我在呢。'}])
        self.service.synthesize = AsyncMock(return_value=b'mp3-audio')
        self.request = {'request_id': str(uuid4()), 'session_id': str(uuid4()), 'text': '今天有点累。'}

    async def asyncTearDown(self):
        self.config.stop()

    async def testDurableTextReplayDoesNotGenerateOrRememberTwice(self):
        result = await self.service.turn(self.request)
        duplicate = await self.service.turn(self.request)
        self.assertEqual(result['segments'], duplicate['segments'])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(self.service.generate.await_count, 1)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0][0], 123)
        self.assertIn('我在呢。', self.saved[0][-1])
        fresh = CallService(self.gateway, self.heartbeat)
        self.assertTrue((await fresh.turn(self.request))['duplicate'])

    async def testAudioUsesOnlyStoredEnglishAndCacheAvoidsRepeatedSynthesis(self):
        await self.service.turn(self.request)
        params = {'request_id': self.request['request_id'], 'segment': 0}
        first = await self.service.audio(params)
        self.assertEqual(base64.b64decode(first['audio_base64']), b'mp3-audio')
        self.assertEqual(await self.service.audio(params), first)
        self.service.synthesize.assert_awaited_once_with("I'm here.")
        with self.assertRaises(CallError): await self.service.audio({**params, 'text': 'arbitrary text'})

    async def testUUIDCannotBeReusedForDifferentWordsOrSession(self):
        await self.service.turn(self.request)
        for change in [{'text':'换一句'}, {'session_id':str(uuid4())}]:
            with self.assertRaises(CallError) as error: await self.service.turn({**self.request, **change})
            self.assertEqual(error.exception.status, 409)

    async def testFailureNeverBecomesCompletedReceiptAndCannotSilentlyRegenerate(self):
        self.service.generate.side_effect = RuntimeError('provider secret')
        with self.assertRaises(CallError) as error: await self.service.turn(self.request)
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(self.gateway.store.get(self.request['request_id'])['status'], 'interrupted')
        with self.assertRaises(CallError): await self.service.turn(self.request)
        self.assertFalse(self.saved)

    async def testBusyChatAndMissingVoiceRejectBeforeBeginning(self):
        await self.bot.chat_locks[123].acquire()
        with self.assertRaises(CallError) as error: await self.service.turn(self.request)
        self.assertEqual(error.exception.status, 429)
        self.bot.chat_locks[123].release()
        with patch.dict(os.environ, {'ELEVENLABS_API_KEY': ''}):
            with self.assertRaises(CallError): await self.service.turn(self.request)
        self.assertFalse(self.gateway.store.rows)

    async def testNoCallerSelectedOwnerModelVoiceProviderOrHistory(self):
        for key in ['owner', 'voice_id', 'model', 'api_key', 'history']:
            with self.assertRaises(CallError): await self.service.turn({**self.request, key:'override'})
        with self.assertRaises(CallError): await self.service.audio({'request_id':str(uuid4()), 'segment':0})
        self.assertFalse(self.gateway.store.rows)

    async def testVoiceFailureRetainsRealBilingualTextWithoutLeakingSecrets(self):
        await self.service.turn(self.request)
        self.service.synthesize.side_effect = RuntimeError('private-key')
        with self.assertRaises(CallError) as error: await self.service.audio({'request_id':self.request['request_id'],'segment':0})
        self.assertNotIn('private-key', str(error.exception))
        self.assertEqual(self.gateway.store.get(self.request['request_id'])['status'], 'done')

    async def testModelReceivesExistingPersonaAndRealConversationMessageFormat(self):
        from unittest.mock import MagicMock
        self.bot.build_system_prompt = lambda owner: 'existing private persona'
        self.bot.current_model = lambda owner: 'existing-model'
        self.bot.api_headers = lambda: {'Authorization':'private-model-key'}
        self.bot.LLMHUB_BASE_URL = 'https://model.example/v1'
        self.bot.extract_text = lambda data: '{"segments":[{"en":"I remember.","zh":"我记得。"}]}'
        self.memory.load_recent_messages = lambda *a: [{'role':'user','content':'我们的真实对话'}, {'role':'assistant','content':'真实回复'}]
        client = AsyncMock()
        response = MagicMock(); response.json.return_value = {}
        client.post.return_value = response
        context = AsyncMock(); context.__aenter__.return_value = client
        with patch('morrow_call.httpx.AsyncClient', return_value=context):
            result = await CallService.generate(self.service, '继续说', False)
        sent = client.post.call_args.kwargs['json']
        self.assertEqual(sent['model'],'existing-model')
        self.assertIn('existing private persona',sent['messages'][0]['content'])
        self.assertIn({'role':'user','content':'我们的真实对话'},sent['messages'])
        self.assertEqual(result[0]['zh'],'我记得。')

    async def testAllCallRoutesRequireOwnerTokenAndStatusContainsNoCredentials(self):
        client = TestClient(TestServer(create_web_app(self.gateway, heartbeat=self.heartbeat)))
        await client.start_server()
        try:
            for method, path in [('get','/v1/call/status'),('post','/v1/call/turn'),('post','/v1/call/audio')]:
                response = await getattr(client, method)(path)
                self.assertEqual(response.status, 401)
            response = await client.get('/v1/call/status', headers={'Authorization':'Bearer '+'a'*32})
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual((await response.json())['configured'], True)
            self.assertNotIn('private-', await response.text())
        finally: await client.close()

    def testTranslationsAreRequiredAndNeverCopiedFromEnglish(self):
        valid = {'segments':[{'en':'Good evening.','zh':'晚上好。'}]}
        self.assertEqual(bilingual(copy.deepcopy(valid)), valid['segments'])
        for invalid in [{'segments':[]}, {'segments':[{'en':'晚上好。','zh':'晚上好。'}]},
                        {'segments':[{'en':'Good evening.','zh':'Good evening.'}]},
                        {'segments':[{'en':'x'*451,'zh':'好'}]}]:
            with self.assertRaises(ValueError): bilingual(invalid)
