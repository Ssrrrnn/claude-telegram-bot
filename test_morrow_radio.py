import asyncio
import base64
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from ios_api import Gateway, create_web_app
from test_ios_api import FakeStore
from morrow_radio import RadioError, RadioSpeech


class RadioTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_voice_and_safe_tone_use_only_owner_settings(self):
        calls = []
        class Memory:
            def get_setting(self, owner, key):
                calls.append(owner)
                return {'voice_backend_v1': 'fish', 'voice_fish_id_v1': 'private-voice'}.get(key)
        speech = RadioSpeech(Memory(), 123)
        with patch('voice_output.FISH_API_KEY', 'private-key'), patch('voice_output.synthesize', AsyncMock(return_value=b'actual-audio')) as synth:
            result = await speech.read({'text': '[shouting] 慢慢读。', 'tone': 'night'})
            synth.assert_awaited_once_with('[soft tone] 慢慢读。', 'custom', 'fish', 'private-voice')
        self.assertTrue(all(owner == 123 for owner in calls))
        self.assertEqual(base64.b64decode(result['audio_base64']), b'actual-audio')
        self.assertNotIn('private', str(result))

    async def test_cache_reuses_audio_but_voice_changes_invalidate_it(self):
        voice = ['voice-a']
        class Memory:
            def get_setting(self, owner, key):
                return {'voice_backend_v1': 'fish', 'voice_fish_id_v1': voice[0]}.get(key)
        speech = RadioSpeech(Memory(), 123)
        with patch('voice_output.FISH_API_KEY', 'secret'), patch('voice_output.synthesize', AsyncMock(return_value=b'audio')) as synth:
            await speech.read({'text': '相同的内容。'})
            await speech.read({'text': '相同的内容。'})
            self.assertEqual(synth.await_count, 1)
            voice[0] = 'voice-b'
            await speech.read({'text': '相同的内容。'})
            self.assertEqual(synth.await_count, 2)

    async def test_invalid_or_caller_provider_parameters_never_generate_audio(self):
        speech = RadioSpeech(None, 123)
        with patch('voice_output.synthesize', AsyncMock()) as synth:
            for data in [None, {}, {'text': '字' * 451}, {'text': '  '}, {'text': '读一读', 'owner': 999}, {'text': '读一读', 'voice_id': 'other'}, {'text': '读一读', 'tone': 'invented'}]:
                with self.assertRaises(RadioError) as error:
                    await speech.read(data)
                self.assertEqual(error.exception.status, 400)
            synth.assert_not_awaited()

    async def test_concurrent_generation_is_bounded_and_failure_is_redacted(self):
        speech = RadioSpeech(None, 123)
        async with speech.lock:
            with self.assertRaises(RadioError) as error:
                await speech.read({'text': '正在生成'})
            self.assertEqual(error.exception.status, 429)
        with patch('voice_output.synthesize', AsyncMock(side_effect=RuntimeError('secret provider details'))):
            with self.assertRaises(RadioError) as error:
                await speech.read({'text': '再读一遍'})
            self.assertEqual(error.exception.status, 503)
            self.assertNotIn('secret', error.exception.message)
        self.assertFalse(speech.lock.locked())

    async def test_cache_has_a_small_fixed_bound(self):
        speech = RadioSpeech(None, 123)
        with patch('voice_output.synthesize', AsyncMock(return_value=b'audio')):
            for number in range(10):
                await speech.read({'text': f'第{number}段'})
        self.assertEqual(len(speech.cache), 8)

    async def test_authenticated_endpoint_does_not_create_a_chat_turn(self):
        calls = []
        async def handler(update, context): calls.append(update)
        gateway = Gateway(owner=123, store=FakeStore(), handler=handler, token='a' * 32)
        client = TestClient(TestServer(create_web_app(gateway)))
        await client.start_server()
        try:
            response = await client.post('/v1/radio/speech', json={'text': '想读给你听。'})
            self.assertEqual(response.status, 401)
            with patch('voice_output.synthesize', AsyncMock(return_value=b'audio')):
                response = await client.post('/v1/radio/speech', json={'text': '想读给你听。'}, headers={'Authorization': 'Bearer ' + 'a' * 32})
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertTrue((await response.json())['audio_base64'])
            self.assertEqual(calls, [])
            self.assertEqual(gateway.store.rows, {})
        finally:
            await client.close()
