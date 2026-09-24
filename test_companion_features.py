import asyncio
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import milestones
import qq_music
from qq_connector import QQConnector, _requested_game
from qq_gateway import QQGateway
from test_qq import FakeMemory


class MilestonesTests(unittest.TestCase):
    def test_save_list_delete_and_leap_day(self):
        mem = FakeMemory()
        call = lambda value: milestones.handle(value, chat_id=42,
            get_setting=mem.get_setting, set_setting=mem.set_setting)
        self.assertIn('9月24日', call('我的生日是9月24日'))
        self.assertIn('纪念日', call('记住2月29日是我们的纪念日'))
        self.assertEqual(len(milestones.due(mem.get_setting(42, milestones.KEY), date(2028, 2, 29))), 1)
        self.assertEqual(milestones.due(mem.get_setting(42, milestones.KEY), date(2027, 2, 28)), [])
        self.assertIn('#2', call('查看重要日子'))
        self.assertIn('纪念日', call('删除重要日子 #2'))
        self.assertEqual(len(milestones.load(mem.get_setting(42, milestones.KEY))), 1)
        self.assertIn('没看明白', call('记住2月30日是约会日'))
        self.assertIsNone(call('9月24日天气不错'))


class QQNativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mem = FakeMemory()
        self.mem.set_setting(42, 'qq_owner_uin_v1', '12345678')
        self.events = []

        async def handler(update, context):
            self.events.append(update.message)
            await update.message.reply_text('接住了。')

        self.connector = QQConnector(owner_chat_id=42, pair_code='secret-pairing-code-123',
            expected_self_uin='88888888', handler=handler, bot=object(), memory=self.mem)
        self.connector._refresh_status = AsyncMock()
        self.connector._remember_stickers = AsyncMock(return_value=[])
        self.connector._maybe_react_or_poke = AsyncMock()
        self.connector._maybe_send_sticker = AsyncMock(return_value=False)
        self.calls = []

        async def action(ws, name, params):
            self.calls.append((name, params))
            if name == 'get_msg':
                return {'user_id': 88888888, 'message_type': 'private',
                    'message': [{'type': 'text', 'data': {'text': '你答应过周五去看电影'}}]}
            return {'message_id': 100}

        self.connector.action = action

    def event(self, text, extra=()):
        return {'post_type': 'message', 'message_type': 'private',
            'self_id': 88888888, 'user_id': 12345678, 'message_id': 222,
            'message': [*extra, {'type': 'text', 'data': {'text': text}}]}

    async def test_quote_is_fetched_and_other_sender_rejected(self):
        reply = {'type': 'reply', 'data': {'id': '111'}}
        await self.connector.handle_event(None, self.event('那天还去吗', [reply]))
        self.assertIn('周五去看电影', self.events[-1].quoted_context)
        self.assertEqual(self.events[-1].text, '那天还去吗')
        async def cross_chat(ws, name, params):
            if name == 'get_msg':
                return {'user_id': 999, 'message': [{'type': 'text', 'data': {'text': 'private secret'}}]}
            return {}
        self.connector.action = cross_chat
        await self.connector.handle_event(None, self.event('这句呢', [reply]))
        self.assertNotIn('private secret', self.events[-1].quoted_context)

    async def test_native_game_and_music_follow_text_once(self):
        self.assertEqual(_requested_game('来玩石头剪刀布'), 'rps')
        self.assertIsNone(_requested_game('昨天掷骰子好玩'))
        await self.connector.handle_event(None, self.event('给我掷骰子'))
        sends = [p['message'] for name, p in self.calls if name == 'send_private_msg']
        self.assertEqual(sends[-1], [{'type': 'dice', 'data': {}}])
        self.assertIn('不知道结果', self.events[-1].qq_native_status)
        self.calls.clear()
        await self.connector.handle_event(None, self.event('分享这首歌 https://music.163.com/#/song?id=12345'))
        sends = [p['message'] for name, p in self.calls if name == 'send_private_msg']
        self.assertEqual(sends[-1], [{'type': 'music', 'data': {'type': '163', 'id': '12345'}}])
        self.assertIsNone(qq_music.card('分享这首歌 https://music.163.com.evil.com/song?id=12345'))
        self.assertEqual(qq_music.card('给我发歌曲 https://y.qq.com/n/ryqq/songDetail/ABc12345'),
            {'type': 'music', 'data': {'type': 'qq', 'id': 'ABc12345'}})

    async def test_offline_alert_only_after_successful_connection(self):
        tg = SimpleNamespace(send_message=AsyncMock())
        self.connector.telegram_bot = tg
        with patch('qq_connector.OFFLINE_ALERT_DELAY_SECONDS', 0):
            await self.connector._alert_if_still_offline()
            tg.send_message.assert_not_awaited()
            self.connector.ever_connected = True
            # The run loop schedules this only after a successful connection.
            await self.connector._alert_if_still_offline()
        tg.send_message.assert_awaited_once()
        self.connector.stopping = True
        with patch('qq_connector.OFFLINE_ALERT_DELAY_SECONDS', 0):
            await self.connector._alert_if_still_offline()
        tg.send_message.assert_awaited_once()

    async def test_account_logout_is_detected_even_with_websocket_open(self):
        tg = SimpleNamespace(send_message=AsyncMock())
        self.connector.telegram_bot = tg
        self.connector.ever_connected = True
        ws = self.connector.ws = object()
        async def login(ws, name, params):
            return {'online': False} if name == 'get_status' else {'user_id': 88888888}
        self.connector.action = login
        with patch('qq_connector.OFFLINE_ALERT_DELAY_SECONDS', 0):
            self.assertFalse(await self.connector._check_login(ws))
            self.assertIs(self.connector.ws, ws)
            self.assertFalse(await self.connector._check_login(ws))
            self.assertIsNone(self.connector.ws)
            await self.connector.offline_alert_task
        self.assertIn('重新扫码', tg.send_message.call_args.kwargs['text'])
        async def restored(ws, name, params):
            return {'online': True} if name == 'get_status' else {'user_id': 88888888}
        self.connector.action = restored
        self.assertTrue(await self.connector._check_login(ws))
        self.assertIs(self.connector.ws, ws)


if __name__ == '__main__':
    unittest.main()
