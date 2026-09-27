import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from zoneinfo import ZoneInfo

import companion_features as features
import continuity
import qq_signature
import qq_connector
from qq_connector import QQConnector


LOCAL = ZoneInfo('Asia/Shanghai')


class Memory:
    def __init__(self):
        self.settings = {}
        self.state = continuity.empty()
        self.actions = {}
        self.diary = {}
        self.next_diary = 1

    def get_setting(self, chat, key): return self.settings.get((chat, key))
    def set_setting(self, chat, key, value): self.settings[chat, key] = value
    def mutate_continuity(self, chat, operation):
        self.state = operation(self.state)
        return self.state
    def begin_action(self, chat, key, kind):
        if (chat, key) in self.actions: return False
        self.actions[chat, key] = ('attempting', kind, None)
        return True
    def finish_action(self, chat, key, status, remote_id=None):
        self.actions[chat, key] = (status, self.actions[chat, key][1], remote_id)
    def load_recent_messages(self, chat, limit):
        return [{'role': 'user', 'content': '今天工作好累'},
                {'role': 'assistant', 'content': '辛苦了，先歇一会儿'}]
    def save_diary(self, chat, mid, text):
        if (chat, mid) in self.diary: return self.diary[chat, mid]['id']
        idx = self.next_diary
        self.next_diary += 1
        self.diary[chat, mid] = {'id': idx, 'text': text, 'created_at': datetime.now(LOCAL)}
        return idx
    def get_diary(self, chat, idx):
        return next((v for (c, _), v in self.diary.items() if c == chat and v['id'] == idx), None)
    def list_diary(self, chat): return [v for (c, _), v in self.diary.items() if c == chat]
    def delete_diary(self, chat, idx):
        return self.diary.pop(next(((c, m) for (c, m), v in self.diary.items()
                                    if c == chat and v['id'] == idx), None), None) is not None
    def diary_context_since(self, chat, since): return self.load_recent_messages(chat, 12) * 2


class FeaturesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = Memory()
        self.now = datetime(2026, 9, 27, 14, 0, tzinfo=LOCAL)

    async def test_requested_followup_is_grounded_once_and_cancellable(self):
        reply = features.followup_command(self.memory, 42,
            '明天下午三点面试，结束后问我怎么样', 100, now=self.now)
        item = self.memory.state['threads'][0]
        self.assertIn('09月28日 16:30', reply)
        self.assertEqual(item['title'], '面试，怎么样')
        self.assertEqual(continuity.due_followup(self.memory.state,
                         item['due_at'] - 60), None)
        self.assertEqual(continuity.due_followup(self.memory.state,
                         item['due_at'])['id'], item['id'])
        features.followup_command(self.memory, 42, '明天下午三点面试，结束后问我怎么样',
                                  100, now=self.now)
        self.assertEqual(len(self.memory.state['threads']), 1)
        self.assertIn('不再问', features.followup_command(
            self.memory, 42, '取消回访 #' + item['id'], 101, now=self.now))
        self.assertIsNone(continuity.due_followup(self.memory.state, item['due_at']))

    async def test_manual_diary_stays_private_and_can_be_removed(self):
        bot = SimpleNamespace(ask_claude=AsyncMock(return_value='今天你说工作很累，我记住了这句。先不用撑着，想说话我就在这里陪你。'),
                              current_model=lambda chat: 'test')
        reply = await features.diary_command(self.memory, bot, 42, '写一篇双人日记', 202)
        self.assertIn('私密日记 #1', reply)
        self.assertIn('今天你说工作很累', await features.diary_command(
            self.memory, bot, 42, '看日记 #1', 203))
        self.assertIn('QQ 私聊', await features.diary_command(
            self.memory, bot, 42, '发日记 #1 到空间', 204))
        self.assertEqual(len(self.memory.diary), 1)
        self.assertIn('删掉', await features.diary_command(self.memory, bot, 42, '删除日记 #1', 205))

    async def test_automatic_diary_only_once_on_a_day_with_conversation(self):
        bot = SimpleNamespace(ask_claude=AsyncMock(return_value='今天你说工作很累，我记住了这句。先不用撑着，想说话我就在这里陪你。'),
                              current_model=lambda chat: 'test')
        await features.auto_diary_tick(self.memory, bot, 42, now=self.now)
        self.assertEqual(len(self.memory.diary), 0)
        evening = self.now.replace(hour=21)
        await features.auto_diary_tick(self.memory, bot, 42, now=evening)
        await features.auto_diary_tick(self.memory, bot, 42, now=evening + timedelta(minutes=5))
        self.assertEqual(len(self.memory.diary), 1)
        bot.ask_claude.assert_awaited_once()

    async def test_signature_and_post_receipts_require_real_action_result(self):
        self.memory.set_setting(42, 'qq_owner_uin_v1', '12345678')
        bot = SimpleNamespace(current_model=lambda chat: 'test')
        connector = QQConnector(owner_chat_id=42, pair_code='x'*20,
            expected_self_uin='88888888', handler=AsyncMock(), bot=bot, memory=self.memory)
        ws = object()
        connector.ws = ws
        calls = []
        async def action(ws, name, params):
            calls.append((name, params))
            return {'result': 0} if name == 'set_self_longnick' else {'tid': 'real-post'}
        connector.action = action
        self.assertEqual(qq_signature.command('换个性签名'), ('generate', ''))
        await connector._set_signature(ws, '只把偏爱留给一个人', 'qq:signature:request:1')
        self.assertEqual(calls[0], ('set_self_longnick', {'longNick': '只把偏爱留给一个人'}))
        self.assertEqual(self.memory.actions[42, 'qq:signature:request:1'][0], 'success')
        self.assertEqual(await connector._post_qzone(ws, '有些话只想留给你', visibility=4,
                                                     action_key='qq:qzone:1'), 'real-post')
        self.assertEqual(self.memory.actions[42, 'qq:qzone:1'], ('success', '空间说说', 'real-post'))
        with self.assertRaises(RuntimeError):
            await connector._post_qzone(ws, '有些话只想留给你', visibility=4, action_key='qq:qzone:1')
        self.assertEqual(len([name for name, _ in calls if name == 'send_qzone_msg']), 1)

    async def test_owner_can_publish_one_private_diary_page_to_qzone(self):
        self.memory.set_setting(42, 'qq_owner_uin_v1', '12345678')
        self.memory.save_diary(42, -20260927, '这段话先收进日记。')
        bot = SimpleNamespace(current_model=lambda chat: 'test')
        connector = QQConnector(owner_chat_id=42, pair_code='x'*20,
            expected_self_uin='88888888', handler=AsyncMock(), bot=bot, memory=self.memory)
        ws = object()
        connector.ws = ws
        calls = []
        async def action(ws, name, params):
            calls.append((name, params))
            return {'tid': 'diary-post'} if name == 'send_qzone_msg' else {}
        connector.action = action
        event = {'post_type': 'message', 'message_type': 'private', 'self_id': 88888888,
                 'user_id': 12345678, 'message_id': 88,
                 'message': [{'type': 'text', 'data': {'text': '发日记 #1 到空间'}}]}
        await connector.handle_event(ws, event)
        self.assertEqual([params['content'] for name, params in calls
                          if name == 'send_qzone_msg'], ['这段话先收进日记。'])
        self.assertIn('发到 QQ 空间了', calls[-1][1]['message'])
        await connector.handle_event(ws, event)
        self.assertEqual(len([name for name, _ in calls if name == 'send_qzone_msg']), 1)

    async def test_auto_signature_updates_once_and_never_uses_private_history(self):
        self.memory.set_setting(42, 'qq_owner_uin_v1', '12345678')
        connector = QQConnector(owner_chat_id=42, pair_code='x'*20,
            expected_self_uin='88888888', handler=AsyncMock(),
            bot=SimpleNamespace(), memory=self.memory)
        ws = object()
        connector.ws = ws
        connector.action = AsyncMock(return_value={'result': 0})
        with patch.object(qq_signature, 'generate', new_callable=AsyncMock,
                          return_value='偏爱这件事，不必说得太响') as draft:
            await connector._refresh_signature(ws)
            await connector._refresh_signature(ws)
        draft.assert_awaited_once_with(connector.bot, 42, '')
        connector.action.assert_awaited_once_with(ws, 'set_self_longnick',
                                                 {'longNick': '偏爱这件事，不必说得太响'})
        self.assertEqual(self.memory.actions[42, next(k for c, k in self.memory.actions
            if c == 42 and k.startswith('qq:signature:auto:'))][0], 'success')
