import copy
import json
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer
import phone_bridge as bridge
from ios_api import Gateway, create_web_app
from test_ios_api import FakeStore


class PhoneMemory:
    owner = 123

    def __init__(self):
        self.records, self.actions = {}, {}
        self.context = None
        self.auto_receipt = False

    def entries(self, begin, end):
        return [copy.deepcopy(e) for e in self.records.values() if begin <= e['day'] <= end]

    def put_entry(self, entry):
        old = self.records.get(entry['id'])
        if old and old['actor'] != entry['actor']:
            raise bridge.BridgeError('不能覆盖另一方')
        self.records[entry['id']] = copy.deepcopy(entry)
        return entry

    def delete_entry(self, identifier):
        if self.records.get(identifier, {}).get('actor') != 'user':
            return False
        del self.records[identifier]
        return True

    def put_snapshot(self, snapshot):
        if self.context and self.context['device_id'] != snapshot['device_id']:
            raise bridge.BridgeError('设备未绑定')
        self.context = {**snapshot, 'received_at': time.time()}

    def snapshot(self):
        return copy.deepcopy(self.context)

    def queue(self, action, snapshot, entry):
        if not self.context['calendar_write']:
            raise bridge.BridgeError('未授权')
        if action['id'] not in self.actions:
            self.actions[action['id']] = {'data': action, 'device_id': snapshot['device_id'], 'status': 'queued', 'result': None}
            self.records[entry['id']] = entry
        if self.auto_receipt:
            self.acknowledge(snapshot['device_id'], action['id'], {'success': True, 'event_id': 'eventkit-real-id', 'message': '保存成功'})

    def result(self, identifier):
        return self.actions.get(identifier)

    def pending(self, device):
        return [v['data'] for v in self.actions.values() if v['device_id'] == device and v['status'] == 'queued']

    def acknowledge(self, device, identifier, result):
        row = self.actions[identifier]
        if row['device_id'] != device:
            raise bridge.BridgeError('不是此手机的操作')
        if row['status'] == 'queued':
            row['status'] = 'done' if result['success'] and result['event_id'] else 'failed'
            row['result'] = copy.deepcopy(result)
            self.records[identifier]['status'] = row['status']
        return row['result']


class PhoneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phone = PhoneMemory()
        self.device = str(uuid4())
        self.phone.put_snapshot(bridge.normalize_snapshot({'device_id': self.device, 'calendar_read': True, 'calendar_write': True, 'battery': {'level': 71}, 'events': []}))
        self.phone.context['received_at'] -= 180  # offline: no wall-clock wait
        self.turn = {'key': 'chat:message', 'results': {}}
        self.previous = bridge._store
        bridge.install(self.phone)
        async def handler(update, context):
            await update.message.reply_text('我在。')
        self.client = TestClient(TestServer(create_web_app(Gateway(owner=123, handler=handler, store=FakeStore(), token='a'*32), self.phone)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer ' + 'a'*32}

    async def asyncTearDown(self):
        bridge.install(self.previous)
        await self.client.close()

    def event(self, **changes):
        start = datetime.now(bridge.TZ) + timedelta(days=2)
        return {'title': '一起读书', 'start': start.isoformat(), 'end': (start + timedelta(hours=1)).isoformat(), **changes}

    async def test_private_routes_auth_before_input_processing(self):
        for method, path in [('get', '/v1/shared?month=2026-10'), ('post', '/v1/phone/context'), ('get', '/v1/phone/actions'), ('post', '/v1/phone/actions/id/receipt'), ('delete', '/v1/shared/id')]:
            r = await getattr(self.client, method)(path)
            self.assertEqual(r.status, 401)
            self.assertEqual(r.headers['Cache-Control'], 'no-store')

    async def test_user_mood_saved_without_chat_or_actor_spoof(self):
        entry = {'id': str(uuid4()), 'actor': 'user', 'kind': 'mood', 'day': '2026-10-05', 'text': '自己的记录', 'emoji': '🤍'}
        r = await self.client.post('/v1/shared', json=entry, headers=self.headers)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.client.server.app._state, {})
        r = await self.client.get('/v1/shared?month=2026-10', headers=self.headers)
        self.assertEqual((await r.json())['entries'][0]['actor'], 'user')
        r = await self.client.post('/v1/shared', json={**entry, 'actor': 'assistant'}, headers=self.headers)
        self.assertEqual(r.status, 400)

    async def test_permission_revocation_removes_previous_data(self):
        self.phone.context['events'] = [{'title': '以前的日程'}]
        r = await self.client.post('/v1/phone/context', json={'device_id': self.device, 'calendar_read': False, 'calendar_write': False, 'events': [{'title': '不能保留'}]}, headers=self.headers)
        self.assertEqual(r.status, 200)
        context = await bridge.execute('read_phone_context', {}, self.phone, self.turn)
        self.assertEqual(context['events'], [])
        self.assertIsNone(context['battery'])
        with self.assertRaises(bridge.BridgeError):
            await bridge.execute('create_phone_calendar_event', self.event(), self.phone, self.turn)
        self.assertFalse(self.phone.actions)

    async def test_offline_action_queued_then_receipt_updates_calendar(self):
        args = self.event()
        queued = await bridge.execute('create_phone_calendar_event', args, self.phone, self.turn)
        self.assertEqual(queued['status'], 'queued')
        self.assertFalse(queued['success'])
        await bridge.execute('create_phone_calendar_event', args, self.phone, self.turn)
        self.assertEqual(len(self.phone.actions), 1)
        r = await self.client.get('/v1/phone/actions?device_id=' + self.device, headers=self.headers)
        action = (await r.json())['actions'][0]
        result = {'device_id': self.device, 'success': True, 'event_id': 'native-event-id', 'message': '已保存'}
        r = await self.client.post('/v1/phone/actions/' + action['id'] + '/receipt', json=result, headers=self.headers)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.phone.records[action['id']]['status'], 'done')
        r = await self.client.post('/v1/phone/actions/' + action['id'] + '/receipt', json={**result, 'success': False}, headers=self.headers)
        self.assertTrue((await r.json())['result']['success'])
        self.assertFalse(self.phone.pending(self.device))

    async def test_wrong_device_cannot_receive_or_complete_action(self):
        await bridge.execute('create_phone_calendar_event', self.event(), self.phone, self.turn)
        identifier = next(iter(self.phone.actions))
        other = str(uuid4())
        r = await self.client.get('/v1/phone/actions?device_id=' + other, headers=self.headers)
        self.assertEqual((await r.json())['actions'], [])
        r = await self.client.post('/v1/phone/actions/' + identifier + '/receipt', json={'device_id': other, 'success': True, 'event_id': 'fake'}, headers=self.headers)
        self.assertEqual(r.status, 400)
        self.assertEqual(self.phone.records[identifier]['status'], 'queued')

    async def test_real_success_requires_nonempty_receipt(self):
        self.phone.auto_receipt = True
        result = await bridge.execute('create_phone_calendar_event', self.event(), self.phone, self.turn)
        self.assertEqual(result['status'], 'done')
        self.assertTrue(result['success'])
        self.assertEqual(next(iter(self.phone.records.values()))['status'], 'done')

    async def test_invalid_or_unrelated_operations_never_queued(self):
        for name, args in [('shell', {'command': 'anything'}), ('create_phone_calendar_event', self.event(start='2026-10-06T10:00:00')), ('create_phone_calendar_event', self.event(alarm_minutes=True)), ('create_phone_calendar_event', self.event(title='')), ('read_shared_calendar', {'from_day': '2026-10-05', 'to_day': '2026-09-01'})]:
            with self.assertRaises((bridge.BridgeError, ValueError)):
                await bridge.execute(name, args, self.phone, self.turn)
        self.assertFalse(self.phone.actions)
        r = await self.client.post('/v1/phone/context', json={'device_id': 123}, headers=self.headers)
        self.assertEqual(r.status, 400)

    async def test_assistant_mood_uses_own_actor_and_deduplicates_retry(self):
        args = {'day': '2026-10-05', 'emoji': '📖', 'text': '今天喜欢的一句话'}
        for turn in [self.turn, {'key': self.turn['key'], 'results': {}}]:
            result = await bridge.execute('record_shared_mood', args, self.phone, turn)
            self.assertEqual(result['entry']['actor'], 'assistant')
        self.assertEqual(len(self.phone.records), 1)
        identifier = next(iter(self.phone.records))
        r = await self.client.delete('/v1/shared/' + identifier, headers=self.headers)
        self.assertFalse((await r.json())['removed'])

    async def test_structured_model_call_executes_and_passes_real_result(self):
        import httpx
        requests = []
        args = {'day': '2026-10-05', 'emoji': '🤍', 'text': '留在日历里的心情'}
        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == 1:
                answer = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'record_shared_mood', 'arguments': json.dumps(args)}}]}
            else:
                result = json.loads(payload['messages'][-1]['content'])
                self.assertEqual(payload['messages'][-1]['role'], 'tool')
                self.assertEqual(result['status'], 'saved')
                self.assertEqual(len(self.phone.records), 1)
                answer = {'role': 'assistant', 'content': '放到我们的日历里了。'}
            return httpx.Response(200, json={'choices': [{'message': answer}]})
        original_client = httpx.AsyncClient
        bot = SimpleNamespace(REQUEST_TIMEOUT_SECONDS=30, LLMHUB_BASE_URL='https://model.invalid', api_headers=lambda: {}, extract_text=lambda d: d['choices'][0]['message']['content'])
        with patch('httpx.AsyncClient', side_effect=lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw)):
            text = await bridge.ask_with_tools(bot, {'model': 'test', 'messages': [{'role': 'system', 'content': '正常人格'}, {'role': 'user', 'content': '写一条心情'}]}, 123)
        self.assertEqual(text, '放到我们的日历里了。')
        self.assertEqual(len(requests), 2)
        self.assertIn('tools', requests[0])

    async def test_provider_without_tool_support_keeps_chat_honest(self):
        import httpx
        requests = []
        def handler(request):
            payload = json.loads(request.content); requests.append(payload)
            if len(requests) == 1:
                return httpx.Response(422, json={})
            self.assertNotIn('tools', payload)
            self.assertIn('本轮工具不可用', payload['messages'][0]['content'])
            return httpx.Response(200, json={'choices': [{'message': {'content': '这次没有保存。'}}]})
        original_client = httpx.AsyncClient
        bot = SimpleNamespace(REQUEST_TIMEOUT_SECONDS=30, LLMHUB_BASE_URL='https://model.invalid', api_headers=lambda: {}, extract_text=lambda d: d['choices'][0]['message']['content'])
        with patch('httpx.AsyncClient', side_effect=lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw)):
            text = await bridge.ask_with_tools(bot, {'messages': [{'role': 'system', 'content': '人格'}]}, 123)
        self.assertEqual(text, '这次没有保存。')
        self.assertFalse(self.phone.records)

    async def test_protocol_probe_does_not_execute_or_store_model_output(self):
        import httpx
        calls = []
        def handler(request):
            payload = json.loads(request.content); calls.append(payload)
            self.assertNotIn('自己的记录', request.content.decode())
            return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [{'function': {'name': 'phone_bridge_probe', 'arguments': '{}'}}]}}]})
        original_client = httpx.AsyncClient
        bot = SimpleNamespace(current_model=lambda owner: 'test', LLMHUB_BASE_URL='https://model.invalid', api_headers=lambda: {})
        with patch('httpx.AsyncClient', side_effect=lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw)):
            self.assertEqual(await bridge.probe_provider(bot, 123), 'supported')
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.phone.actions)
        self.assertFalse(self.phone.records)
