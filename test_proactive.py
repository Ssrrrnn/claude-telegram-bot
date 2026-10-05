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
import continuity
import milestones


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
        def mutate(chat, operation):
            state = operation(continuity.decode(self.settings.get((chat, continuity.KEY))))
            self.settings[chat, continuity.KEY] = __import__('json').dumps(state)
            return state
        memory.mutate_continuity = mutate
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
        p = patch.object(self.app.voice_output, 'auto_spontaneous', return_value=False)
        p.start()
        self.addCleanup(p.stop)

    async def test_music_link_is_atomic_but_surrounding_prose_still_splits(self):
        card = '[你好吗？ · 歌手](https://y.qq.com/n/ryqq/songDetail/Test123)'
        self.assertEqual(self.app.strict_split_bubbles('想把这首给你。' + card + '\n听听看！'),
                         ['想把这首给你。', card, '听听看！'])

    async def test_daily_counter_cannot_block_and_history_persists(self):
        self.settings[1, 'proactive_day_count'] = '999'
        await self.app._proactive_tick(self.telegram)
        self.assertEqual(self.app.runner.bot.ask_claude.await_count, 1)
        self.assertGreater(self.telegram.bot.send_message.await_count, 0)
        state = self.app._load_state(1)
        self.assertEqual(state['silence_count'], 1)
        self.assertFalse(state['user_replied_after_proactive'])
        self.assertEqual(state['last_proactive_content'], self.app.runner.bot.histories[1][-1]['content'])
        self.assertEqual(len(state['recent']), 1)
        await self.app._proactive_tick(self.telegram)
        self.assertEqual(self.telegram.bot.send_message.await_count, 1)

    async def test_morrow_only_writes_durable_inbox_without_qq_or_telegram(self):
        saved=[]
        self.app.runner.memory_pg.save_recent_proactive=lambda chat,text:saved.append(text)
        qq=types.SimpleNamespace(ws=object(),send_proactive=AsyncMock())
        self.telegram.bot_data={'qq_connector':qq}
        self.settings[1,'last_active_platform_v1']='qq'
        with patch.dict('os.environ',{'MORROW_ONLY':'1'}):
            await self.app._proactive_tick(self.telegram)
            await self.app._proactive_tick(self.telegram)
        self.assertEqual(len(saved),1)
        self.telegram.bot.send_message.assert_not_awaited();qq.send_proactive.assert_not_awaited()
        self.assertEqual(saved[0],self.app.runner.bot.histories[1][-1]['content'])

    async def test_recent_qq_contact_receives_single_proactive_in_qq(self):
        self.settings[1, 'last_active_platform_v1'] = 'qq'
        qq = types.SimpleNamespace(ws=object(), send_proactive=AsyncMock(return_value=False))
        self.telegram.bot_data = {'qq_connector': qq}
        await self.app._proactive_tick(self.telegram)
        qq.send_proactive.assert_awaited_once()
        self.telegram.bot.send_message.assert_not_awaited()
        self.assertEqual(self.app._load_state(1)['silence_count'], 1)

    async def test_disconnected_qq_proactive_reaches_telegram(self):
        self.settings[1, 'last_active_platform_v1'] = 'qq'
        qq = types.SimpleNamespace(ws=None, send_proactive=AsyncMock())
        self.telegram.bot_data = {'qq_connector': qq}
        await self.app._proactive_tick(self.telegram)
        qq.send_proactive.assert_not_awaited()
        self.telegram.bot.send_message.assert_awaited()
        self.assertEqual(self.app._load_state(1)['silence_count'], 1)

    async def test_auto_proactive_can_send_voice_and_save_cooldown(self):
        self.app.voice_output.auto_spontaneous.return_value = True
        with patch.object(self.app.voice_output, 'send_voice', new_callable=AsyncMock, return_value=True) as send:
            await self.app._proactive_tick(self.telegram)
        send.assert_awaited_once()
        self.telegram.bot.send_message.assert_not_awaited()
        self.assertTrue(self.settings[(1, self.app.voice_output.AUTO_LAST_SENT_KEY)])

    async def test_followup_has_reason_and_is_claimed_once(self):
        state = continuity.empty()
        now = self.now.timestamp()
        state['threads'] = [{'id': 'shot', 'title': '镜头修改', 'purpose': '问修改是否顺利',
            'category': 'work', 'subject': 'user', 'realm': 'real', 'status': 'open',
            'due_at': now - 1, 'expires_at': now + 3600, 'contacted_at': None}]
        self.app.runner.memory_pg.mutate_continuity(1, lambda _: state)
        self.settings[1, self.app.KEY_NEXT_AT] = str(int(now) + 7200)
        self.app.runner.bot.ask_claude.return_value = '那个镜头后来顺了吗？'
        await self.app._proactive_tick(self.telegram)
        sent = self.app.runner.bot.ask_claude.call_args.args[1][-1]['content']
        self.assertIn('问修改是否顺利', sent)
        self.assertIsNotNone(continuity.load(self.app.runner.memory_pg, 1)['threads'][0]['contacted_at'])
        await self.app._proactive_tick(self.telegram)
        self.assertEqual(self.telegram.bot.send_message.await_count, 1)

    async def test_model_can_decline_contact_without_sending_sentinel(self):
        self.app.runner.bot.ask_claude.return_value = 'NO_REPLY'
        await self.app._proactive_tick(self.telegram)
        self.telegram.bot.send_message.assert_not_called()
        self.assertGreater(int(self.settings[1, self.app.KEY_NEXT_AT]), self.now.timestamp())

    async def test_first_morning_contact_sends_even_if_model_declines(self):
        self.app.runner.bot.ask_claude.return_value = 'NO_REPLY'
        with patch.object(self.app, '_now_local', return_value=self.now.replace(hour=8, minute=50)):
            await self.app._proactive_tick(self.telegram)
            self.assertFalse(self.app._first_morning_contact(1))
        self.telegram.bot.send_message.assert_awaited()
        text = self.telegram.bot.send_message.call_args.kwargs['text']
        self.assertNotEqual(text, 'NO_REPLY')
        self.assertTrue(any(word in text for word in ('早', '醒了')))

    async def test_morning_message_does_not_repeat_yesterday(self):
        yesterday = '早。醒了再回我，想听你说说话。'
        state = self.app._load_state(1)
        state['recent'] = [{'type': '想念', 'content': yesterday}]
        self.app._save_state(1, state)
        self.app.runner.bot.ask_claude.return_value = 'NO_REPLY'
        with patch.object(self.app, '_now_local', return_value=self.now.replace(hour=8, minute=50)):
            await self.app._proactive_tick(self.telegram)
        self.assertNotEqual(self.telegram.bot.send_message.call_args.kwargs['text'], yesterday)

    async def test_morning_bypasses_late_random_schedule_once(self):
        self.settings[1, self.app.KEY_NEXT_AT] = str(int(self.now.timestamp()) + 7200)
        self.app.runner.bot.ask_claude.return_value = '早。醒了再回我，今天想听你说话。'
        with patch.object(self.app, '_now_local', return_value=self.now.replace(hour=8, minute=50)):
            await self.app._proactive_tick(self.telegram)
            await self.app._proactive_tick(self.telegram)
        self.assertEqual(self.app.runner.bot.ask_claude.await_count, 1)
        self.assertGreater(self.telegram.bot.send_message.await_count, 0)

    async def test_morning_does_not_claim_user_just_woke_or_interrupt_recent_chat(self):
        self.settings[1, self.app.KEY_NEXT_AT] = str(int(self.now.timestamp()) + 7200)
        morning = self.now.replace(hour=8, minute=50)
        self.settings[1, self.app.KEY_LAST_USER_AT] = str(int(morning.timestamp()) - 60)
        with patch.object(self.app, '_now_local', return_value=morning):
            await self.app._proactive_tick(self.telegram)
        self.telegram.bot.send_message.assert_not_awaited()

    async def test_important_date_can_bypass_schedule_and_only_fires_once(self):
        self.settings[1, self.app.KEY_NEXT_AT] = str(int(self.now.timestamp()) + 7200)
        self.settings[1, milestones.KEY] = '[{"month": 9, "day": 18, "name": "我们的纪念日"}]'
        self.app.runner.bot.ask_claude.return_value = '今天是我们的纪念日。想和你说声，我记得。'
        await self.app._proactive_tick(self.telegram)
        await self.app._proactive_tick(self.telegram)
        self.assertEqual(self.app.runner.bot.ask_claude.await_count, 1)
        self.assertGreater(self.telegram.bot.send_message.await_count, 0)
        self.assertEqual(self.settings[1, milestones.LAST_SENT_KEY], '2026-09-18')

    async def test_declined_due_followup_does_not_retry_every_tick(self):
        state = continuity.empty()
        now = self.now.timestamp()
        state['threads'] = [{'id': 'shot', 'title': '镜头修改', 'purpose': '问修改是否顺利',
            'realm': 'real', 'status': 'open', 'due_at': now - 1,
            'expires_at': now + 20000, 'contacted_at': None}]
        self.app.runner.memory_pg.mutate_continuity(1, lambda _: state)
        self.app.runner.bot.ask_claude.return_value = 'NO_REPLY'
        await self.app._proactive_tick(self.telegram)
        await self.app._proactive_tick(self.telegram)
        self.assertEqual(self.app.runner.bot.ask_claude.await_count, 1)
        self.telegram.bot.send_message.assert_not_called()

    async def test_explicit_sleep_blocks_even_daytime_proactive_contact(self):
        state = continuity.receive_state(continuity.empty(), '我睡了', 5, self.now.timestamp())
        self.app.runner.memory_pg.mutate_continuity(1, lambda _: state)
        await self.app._proactive_tick(self.telegram)
        self.app.runner.bot.ask_claude.assert_not_called()
        self.telegram.bot.send_message.assert_not_called()

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
