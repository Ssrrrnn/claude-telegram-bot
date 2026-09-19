import asyncio
import importlib.util
import sys
import types
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import proactive_policy as policy


class PolicyTests(unittest.TestCase):
    def test_quiet_boundaries(self):
        for hour, minute, expected in [(0, 29, False), (0, 30, True), (8, 29, True), (8, 30, False), (23, 59, False)]:
            self.assertEqual(policy.is_quiet(datetime(2026, 9, 18, hour, minute)), expected)

    def test_interval_and_silence_backoff(self):
        now = datetime(2026, 9, 18, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
        for count, low in [(0, 20), (1, 40), (2, 60), (8, 90)]:
            for _ in range(100):
                seconds = (policy.next_due(now, count) - now).total_seconds()
                self.assertGreaterEqual(seconds, low * 60)
                self.assertLessEqual(seconds, 10800)

    def test_quiet_rollover(self):
        now = datetime(2026, 9, 18, 23, 50, tzinfo=ZoneInfo('Asia/Shanghai'))
        with patch.object(policy.random, 'randint', return_value=0):
            due = policy.after_quiet(now.replace(day=19, hour=0, minute=50))
        self.assertEqual(due.isoformat(), '2026-09-19T08:30:00+08:00')

    def test_repetition(self):
        self.assertTrue(policy.is_repetition('忙完了吗？', ['忙完了吗。']))
        self.assertTrue(policy.is_repetition('今天是不是特别忙呀', ['今天是不是特别忙']))
        self.assertFalse(policy.is_repetition('刚想到你昨天说的那本书', ['忙完了吗']))

    def test_type_rotation_in_daytime_silence(self):
        now = datetime(2026, 9, 18, 12)
        for _ in range(30):
            self.assertNotIn(policy.choose_type(now, ['想念', '分享'], 4), ['想念', '分享', '深夜陪伴', '吃醋', '关心'])


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = {}
        memory = types.SimpleNamespace(
            get_setting=lambda chat, key: self.settings.get((chat, key)),
            set_setting=lambda chat, key, value: self.settings.__setitem__((chat, key), value),
            save_recent_proactive=lambda chat, text: None,
        )
        bot = types.SimpleNamespace(
            build_system_prompt=lambda chat: 'persona', TELEGRAM_OWNER_ID=1,
            histories=defaultdict(list), chat_locks=defaultdict(asyncio.Lock),
            is_authorized=lambda chat: True, current_model=lambda chat: 'mock',
            ask_claude=AsyncMock(return_value='刚想到你昨天说的那本书。'), MAX_HISTORY_MESSAGES=40,
        )
        runner = types.SimpleNamespace(bot=bot, memory_pg=memory, handle_text_with_memory=AsyncMock(),
                                       post_init=AsyncMock(), _looks_like_emoji_only=lambda text: False)
        constants = types.ModuleType('telegram.constants')
        constants.ParseMode = types.SimpleNamespace(MARKDOWN='Markdown')
        with patch.dict(sys.modules, {'runner': runner, 'telegram.constants': constants}):
            spec = importlib.util.spec_from_file_location('proactive_under_test', Path(__file__).with_name('runner_strict_bubbles.py'))
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.now = datetime(2026, 9, 18, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.patches = [patch.object(self.app, '_now_local', return_value=self.now),
                        patch.object(self.app.time, 'time', return_value=self.now.timestamp())]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.settings[1, self.app.KEY_NEXT_AT] = str(int(self.now.timestamp()) - 1)
        self.telegram = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=AsyncMock()))

    async def test_daily_counter_cannot_block_and_history_persists(self):
        self.settings[1, 'proactive_day_count'] = '999'
        await self.app._proactive_tick(self.telegram)
        self.telegram.bot.send_message.assert_awaited_once()
        state = self.app._load_state(1)
        self.assertEqual(state['silence_count'], 1)
        self.assertFalse(state['user_replied_after_proactive'])
        self.assertEqual(state['last_proactive_content'], self.app.runner.bot.histories[1][-1]['content'])
        self.assertEqual(len(state['recent']), 1)
        await self.app._proactive_tick(self.telegram)
        self.assertEqual(self.telegram.bot.send_message.await_count, 1)

    async def test_duplicate_retries_then_skips_even_after_memory_restart(self):
        state = self.app._load_state(1)
        state['recent'] = [{'type': '想念', 'content': '刚想到你昨天说的那本书。'}]
        self.app._save_state(1, state)
        self.app.runner.bot.histories.clear()
        await self.app._proactive_tick(self.telegram)
        self.assertEqual(self.app.runner.bot.ask_claude.await_count, 3)
        self.telegram.bot.send_message.assert_not_awaited()

    async def test_user_arrives_during_generation(self):
        async def generate(*args):
            self.settings[1, self.app.KEY_LAST_USER_AT] = str(int(self.now.timestamp()))
            return '突然有点想你。'
        self.app.runner.bot.ask_claude.side_effect = generate
        await self.app._proactive_tick(self.telegram)
        self.telegram.bot.send_message.assert_not_awaited()

    async def test_send_failure_keeps_cooldown_without_claiming_success(self):
        self.telegram.bot.send_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.app._proactive_tick(self.telegram)
        self.assertGreater(int(self.settings[1, self.app.KEY_NEXT_AT]), self.now.timestamp())
        self.assertEqual(self.app._load_state(1)['silence_count'], 0)
        await self.app._proactive_tick(self.telegram)
        self.assertEqual(self.telegram.bot.send_message.await_count, 1)

    async def test_database_failure_prevents_send(self):
        self.app.runner.memory_pg.get_setting = lambda *args: (_ for _ in ()).throw(ConnectionError())
        with self.assertRaises(ConnectionError):
            await self.app._proactive_tick(self.telegram)
        self.telegram.bot.send_message.assert_not_awaited()

    async def test_user_reply_resets_silence(self):
        state = self.app._load_state(1)
        state.update(silence_count=8, user_replied_after_proactive=False)
        self.app._save_state(1, state)
        update = types.SimpleNamespace(message=types.SimpleNamespace(text='我回来了'), effective_chat=types.SimpleNamespace(id=1))
        await self.app.handle_text_with_proactive_reset(update, None)
        state = self.app._load_state(1)
        self.assertEqual(state['silence_count'], 0)
        self.assertTrue(state['user_replied_after_proactive'])

    async def test_disabled_and_quiet_do_not_send(self):
        self.settings[1, self.app.KEY_ENABLED] = '0'
        await self.app._proactive_tick(self.telegram)
        self.settings[1, self.app.KEY_ENABLED] = '1'
        with patch.object(self.app, '_now_local', return_value=self.now.replace(hour=3)):
            await self.app._proactive_tick(self.telegram)
        self.telegram.bot.send_message.assert_not_awaited()

    def test_daytime_time_block_forbids_unsolicited_sleep(self):
        block = self.app._time_awareness_block(1)
        self.assertIn("唯一有效的当前时间", block)
        self.assertIn("06:00-23:00行为锁", block)
        self.assertIn("禁止说去睡", block)


if __name__ == '__main__':
    unittest.main()
