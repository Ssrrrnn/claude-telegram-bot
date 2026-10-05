import asyncio
import base64
import copy
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer
from ios_api import APIError, Gateway, Store, create_web_app


class FakeStore:
    def __init__(self):
        self.rows = {}
    def get(self, key):
        return self.rows.get(key)
    def begin(self, key, text):
        self.rows[key] = {'id': 1, 'user_text': text, 'status': 'processing', 'result': None}
        return 2**62 + 1
    def finish(self, key, result, status='done'):
        cached = copy.deepcopy(result)
        cached['replies'] = [{'type': 'text', 'text': reply.get('fallback_text', '')}
                             if reply['type'] == 'record' else reply
                             for reply in cached.get('replies', [])]
        self.rows[key].update(status=status, result=cached)
    def history(self):
        return [{'id': '1:assistant', 'role': 'assistant', 'text': '我在。'}]
    def assistant_id(self, message_id):
        return '1:assistant'
    def collection(self, kind, before=None, limit=50):
        return {'items': [{'id': '1', 'text': kind}], 'has_more': False, 'next_before': None}


class CollectionStoreTests(unittest.TestCase):
    def test_real_memory_query_is_owner_scoped_and_has_stable_cursor(self):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = [
            {'id': 9, 'text': 'Private owner memory', 'created_at': datetime(2026, 10, 5, tzinfo=timezone.utc), 'kind': 'manual', 'importance': 3},
            {'id': 8, 'text': 'Next page', 'created_at': datetime(2026, 10, 4, tzinfo=timezone.utc)}]
        connection = MagicMock(); connection.__enter__.return_value = connection; connection.cursor.return_value = cursor
        memory = MagicMock(); memory._connect.return_value = connection
        result = Store(memory, 123).collection('memories', before=10, limit=1)
        sql, params = cursor.execute.call_args.args
        self.assertIn('WHERE chat_id=%s AND id < %s', sql)
        self.assertEqual(params, [123, 10, 2])
        self.assertEqual(result['next_before'], '9')
        self.assertTrue(result['has_more'])
        self.assertEqual(result['items'][0]['importance'], 3)
        self.assertEqual(result['items'][0]['at'], '2026-10-05T00:00:00+00:00')
        with self.assertRaises(ValueError):
            Store(memory, 123).collection('memories WHERE TRUE')


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.store = FakeStore()
        async def handler(update, context):
            self.calls.append(update)
            await update.message.reply_text('我在。')
        self.gateway = Gateway(owner=123, handler=handler, store=self.store, token='a'*32)
        self.client = TestClient(TestServer(create_web_app(self.gateway)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer ' + 'a'*32}
    async def asyncTearDown(self):
        await self.client.close()
    def message(self, **changes):
        return {'request_id': str(uuid4()), 'text': '晚上好', 'sent_at': time.time(), **changes}

    async def test_all_private_routes_require_token(self):
        for route in ['/v1/history', '/v1/favorites', '/v1/diaries', '/v1/memories']:
            response = await self.client.get(route)
            self.assertEqual(response.status, 401)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        response = await self.client.post('/v1/chat', json=self.message())
        self.assertEqual(response.status, 401)
        self.assertEqual(self.calls, [])

    async def test_fixed_owner_and_transport(self):
        response = await self.client.post('/v1/chat', json=self.message(), headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.calls[0].effective_chat.id, 123)
        self.assertEqual(self.calls[0].message.source_platform, 'ios')
        self.assertGreater(self.calls[0].message.message_id, 2**62)

    async def test_owner_override_rejected(self):
        response = await self.client.post('/v1/chat', json=self.message(chat_id=999), headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertFalse(self.calls)

    async def test_replay_does_not_call_model(self):
        payload = self.message()
        for _ in range(2):
            response = await self.client.post('/v1/chat', json=payload, headers=self.headers)
            self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['duplicate'])
        self.assertEqual(len(self.calls), 1)

    async def test_reusing_uuid_for_new_text_rejected(self):
        payload = self.message()
        await self.gateway.process(payload)
        with self.assertRaises(APIError) as failure:
            await self.gateway.process({**payload, 'text': 'changed'})
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(len(self.calls), 1)

    async def test_invalid_inputs_rejected_before_model(self):
        for payload in [None, [], {}, self.message(text=''), self.message(text='x'*4001),
                        self.message(request_id='not-a-uuid'), self.message(sent_at=True),
                        self.message(sent_at=time.time()-8*86400), self.message(sent_at=float('nan'))]:
            with self.assertRaises(APIError):
                await self.gateway.process(payload)
        self.assertEqual(self.calls, [])

    async def test_concurrent_retry_and_new_message(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(update, context):
            entered.set()
            await release.wait()
            await update.message.reply_text('我在。')
        self.gateway.handler = handler
        payload = self.message()
        task = asyncio.create_task(self.gateway.process(payload))
        await entered.wait()
        with self.assertRaises(APIError) as same:
            await self.gateway.process(payload)
        self.assertEqual(same.exception.status, 409)
        with self.assertRaises(APIError) as other:
            await self.gateway.process(self.message())
        self.assertEqual(other.exception.status, 429)
        release.set()
        await task

    async def test_failed_request_does_not_leak_exception_or_regenerate(self):
        async def broken(update, context):
            self.calls.append(update)
            raise RuntimeError('private provider information')
        self.gateway.handler = broken
        payload = self.message()
        response = await self.client.post('/v1/chat', json=payload, headers=self.headers)
        self.assertEqual(response.status, 503)
        self.assertNotIn('private', await response.text())
        response = await self.client.post('/v1/chat', json=payload, headers=self.headers)
        self.assertEqual(response.status, 409)
        self.assertEqual(len(self.calls), 1)

    async def test_legacy_provider_error_is_redacted(self):
        async def handler(update, context):
            await update.message.reply_text('API 请求失败：secret-provider-url')
        self.gateway.handler = handler
        response = await self.client.post('/v1/chat', json=self.message(), headers=self.headers)
        self.assertEqual(response.status, 503)
        self.assertNotIn('secret', await response.text())

    async def test_voice_payload_and_text_replay(self):
        async def handler(update, context):
            from types import SimpleNamespace
            await context.bot.send_voice(voice=SimpleNamespace(input_file_content=b'mp3-data'), caption='靠近一点。')
        self.gateway.handler = handler
        payload = self.message()
        first = await self.gateway.process(payload)
        self.assertEqual(base64.b64decode(first['replies'][0]['audio_base64']), b'mp3-data')
        second = await self.gateway.process(payload)
        self.assertEqual(second['replies'], [{'type': 'text', 'text': '靠近一点。'}])

    async def test_history_and_collections(self):
        for route, key in [('/v1/history', 'messages'), ('/v1/favorites', 'items'), ('/v1/diaries', 'items')]:
            response = await self.client.get(route, headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())[key])

    async def test_bad_json(self):
        response = await self.client.post('/v1/chat', data='{', headers=self.headers)
        self.assertEqual(response.status, 400)

    async def test_collection_cursor_rejects_injection_or_unbounded_limits(self):
        for query in ['limit=101', 'limit=0', 'before=-1', 'before=1%20OR%201=1', 'before=9999999999999999999', 'owner=999']:
            response = await self.client.get('/v1/memories?' + query, headers=self.headers)
            self.assertEqual(response.status, 400)
        response = await self.client.get('/v1/memories?before=12&limit=50', headers=self.headers)
        self.assertEqual(response.status, 200)


if __name__ == '__main__':
    unittest.main()
