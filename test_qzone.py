import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import qq_qzone
from qq_connector import QQConnector
from test_qq import FakeMemory


class QzoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = FakeMemory()
        self.memory.set_setting(42, 'qq_owner_uin_v1', '12345678')
        self.model = SimpleNamespace(
            ask_claude=AsyncMock(return_value='今天忽然想起你说过的话，心里暖了一下。'),
            current_model=lambda chat: 'test',
        )
        self.connector = QQConnector(owner_chat_id=42, pair_code='secret-pairing-code-123',
            expected_self_uin='88888888', handler=AsyncMock(), bot=self.model, memory=self.memory)
        self.calls = []

        async def action(ws, name, params):
            self.calls.append((name, params))
            return {'tid': 'post-123'}

        self.connector.action = action

    def event(self, text, mid=1, user=12345678):
        return {'post_type': 'message', 'message_type': 'private',
                'self_id': 88888888, 'user_id': user, 'message_id': mid,
                'message': [{'type': 'text', 'data': {'text': text}}]}

    async def test_explicit_body_publishes_immediately_and_is_idempotent(self):
        await self.connector.handle_event(None, self.event('发空间说说：今天想你了'))
        posts = [p for name, p in self.calls if name == 'send_qzone_msg']
        self.assertEqual(posts, [{'content': '今天想你了', 'images': [], 'ugc_right': 4, 'target_uins': []}])
        self.assertIn('发到 QQ 空间了', self.calls[-1][1]['message'])
        await self.connector.handle_event(None, self.event('发空间说说：今天想你了'))
        self.assertEqual(len([name for name, _ in self.calls if name == 'send_qzone_msg']), 1)

    async def test_natural_request_writes_and_publishes_without_model_promise(self):
        message = '老公你现在可以发说说了你去看一下嘛'
        self.assertEqual(qq_qzone.command(message), ('immediate', ''))
        self.assertEqual(qq_qzone.command('你能发空间说说吗'), ('help', ''))
        self.assertIsNone(qq_qzone.command('别发说说'))
        with patch('qq_qzone.generate_requested_post', new_callable=AsyncMock,
                   return_value='想我老婆了。') as generate:
            await self.connector.handle_event(None, self.event(message))
        generate.assert_awaited_once()
        posts = [p for name, p in self.calls if name == 'send_qzone_msg']
        self.assertEqual(posts[0]['content'], '想我老婆了。')
        self.assertIn('发到 QQ 空间了', self.calls[-1][1]['message'])
        self.model.ask_claude.assert_not_awaited()

    async def test_qzone_help_explains_real_invocation_without_publishing(self):
        await self.connector.handle_event(None, self.event('怎么发空间说说'))
        self.assertFalse(any(name == 'send_qzone_msg' for name, _ in self.calls))
        self.assertIn('发空间说说：正文', self.calls[-1][1]['message'])
        self.assertIn('没确认成功', self.calls[-1][1]['message'])
        self.model.ask_claude.assert_not_awaited()

    async def test_capability_question_reports_actual_publish_path(self):
        await self.connector.handle_event(None, self.event('你有没有实际调用QQ空间的权限'))
        self.assertFalse(any(name == 'send_qzone_msg' for name, _ in self.calls))
        self.assertIn('接了发空间说说的功能', self.calls[-1][1]['message'])
        self.calls.clear()
        self.memory.set_setting(42, qq_qzone.LAST_TOPIC_AT_KEY, str(int(__import__('time').time())))
        await self.connector.handle_event(None, self.event('你没有实际调用权限吗', mid=2))
        self.assertIn('接了发空间说说的功能', self.calls[-1][1]['message'])

    async def test_capability_reports_logged_out_account_truthfully(self):
        self.connector.account_offline = True
        await self.connector.handle_event(None, self.event('你有发空间说说的权限吗'))
        self.assertIn('QQ 号现在离线', self.calls[-1][1]['message'])
        self.assertFalse(any(name == 'send_qzone_msg' for name, _ in self.calls))

    async def test_one_time_live_probe_uses_real_publish_path_only_once(self):
        ws = self.connector.ws = object()
        await self.connector._probe_qzone_once(ws)
        await self.connector._probe_qzone_once(ws)
        posts = [p for name, p in self.calls if name == 'send_qzone_msg']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['content'], '测试一下')
        self.assertEqual(posts[0]['ugc_right'], 4)

    async def test_contextual_self_post_request_dispatches_actual_api(self):
        self.memory.set_setting(42, qq_qzone.LAST_TOPIC_AT_KEY, str(int(__import__('time').time())))
        with patch('qq_qzone.generate_requested_post', new_callable=AsyncMock,
                   return_value='想我老婆了。'):
            await self.connector.handle_event(None, self.event('那你自己发一条吧'))
        self.assertEqual(len([name for name, _ in self.calls if name == 'send_qzone_msg']), 1)

    async def test_short_followups_use_recent_qzone_topic_and_real_status(self):
        self.memory.set_setting(42, qq_qzone.LAST_TOPIC_AT_KEY, str(int(__import__('time').time())))
        with patch('qq_qzone.generate_requested_post', new_callable=AsyncMock,
                   return_value='想我老婆了。'):
            await self.connector.handle_event(None, self.event('发吧老公'))
        self.assertEqual(len([x for x in self.calls if x[0] == 'send_qzone_msg']), 1)
        await self.connector.handle_event(None, self.event('发了没', mid=2))
        self.assertIn('正文是：想我老婆了。', self.calls[-1][1]['message'])
        await self.connector.handle_event(None, self.event('我看看', mid=3))
        self.assertIn('没有可发的空间截图', self.calls[-1][1]['message'])

    async def test_short_reply_bootstraps_topic_from_recent_persisted_qq_turn(self):
        class Rows:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def cursor(self): return self
            def execute(self, query, params): pass
            def fetchall(self): return [{'user_text': '老公你现在可以发说说了'}]

        self.memory._connect = lambda: Rows()
        with patch('qq_qzone.generate_requested_post', new_callable=AsyncMock,
                   return_value='想我老婆了。'):
            await self.connector.handle_event(None, self.event('发吧老公'))
        self.assertEqual(len([x for x in self.calls if x[0] == 'send_qzone_msg']), 1)

    async def test_short_reply_without_topic_never_publishes(self):
        await self.connector.handle_event(None, self.event('发吧老公'))
        self.assertFalse(any(name == 'send_qzone_msg' for name, _ in self.calls))
        self.assertIsNone(qq_qzone.contextual_command('发了'))
        self.assertIsNone(qq_qzone.contextual_command('你发这个'))

    async def test_missing_post_admits_failure_and_no_fake_image(self):
        self.memory.set_setting(42, qq_qzone.LAST_TOPIC_AT_KEY, str(int(__import__('time').time())))
        await self.connector.handle_event(None, self.event('发了没'))
        self.assertIn('还没有确认发表成功', self.calls[-1][1]['message'])
        await self.connector.handle_event(None, self.event('根本没有', mid=2))
        self.assertIn('刚才我说发了是错的', self.calls[-1][1]['message'])
        self.assertNotIn('<image_placeholder>', self.calls[-1][1]['message'])

    async def test_model_image_placeholder_is_not_sent_as_a_fake_screenshot(self):
        await self.connector._send_replies(None, '12345678', [
            {'type': 'text', 'text': '<image_placeholder>'},
        ])
        self.assertIn('先不假装发给你', self.calls[-1][1]['message'])

    async def test_status_only_reports_confirmed_publishes(self):
        await self.connector.handle_event(None, self.event('说说发了吗'))
        self.assertIn('还没有确认发表成功', self.calls[-1][1]['message'])
        await self.connector.handle_event(None, self.event('发空间说说：想我老婆了', mid=2))
        await self.connector.handle_event(None, self.event('你发说说了吗', mid=3))
        self.assertIn('想我老婆了', self.calls[-1][1]['message'])
        self.assertEqual(len([name for name, _ in self.calls if name == 'send_qzone_msg']), 1)

    async def test_draft_from_model_is_shown_before_any_post(self):
        await self.connector.handle_event(None, self.event('写条空间说说：今天很想她'))
        self.model.ask_claude.assert_awaited_once()
        self.assertIn('今天忽然想起你说过的话', self.calls[-1][1]['message'])
        self.assertFalse(any(name == 'send_qzone_msg' for name, _ in self.calls))
        await self.connector.handle_event(None, self.event('取消发表空间说说', mid=2))
        await self.connector.handle_event(None, self.event('确认发表空间说说', mid=3))
        self.assertFalse(any(name == 'send_qzone_msg' for name, _ in self.calls))

    async def test_expired_draft_and_stranger_never_publish(self):
        self.memory.set_setting(42, qq_qzone.PENDING_KEY, qq_qzone.encode('一条旧草稿', now=100))
        with patch('qq_qzone.time.time', return_value=100 + qq_qzone.PENDING_SECONDS + 1):
            await self.connector.handle_event(None, self.event('确认发表空间说说', mid=2))
        await self.connector.handle_event(None, self.event('发空间说说：偷偷发', mid=3, user=999999))
        self.assertFalse(any(name == 'send_qzone_msg' for name, _ in self.calls))

    async def test_timeout_does_not_retry_unknown_publish(self):
        attempts = []

        async def uncertain(ws, name, params):
            if name == 'send_qzone_msg':
                attempts.append(params)
                raise TimeoutError()
            self.calls.append((name, params))
            return {}

        self.connector.action = uncertain
        await self.connector.handle_event(None, self.event('发空间说说：今天想你了', mid=2))
        await self.connector.handle_event(None, self.event('发空间说说：今天想你了', mid=2))
        self.assertEqual(len(attempts), 1)
        self.assertIn('还不能确认发成功', self.calls[-2][1]['message'])

    async def test_autonomous_post_is_friends_only_and_model_can_decline(self):
        now = datetime(2026, 9, 24, 14, tzinfo=qq_qzone.BEIJING).timestamp()
        ws = object()
        self.connector.ws = ws
        self.memory.set_setting(42, qq_qzone.AUTO_NEXT_THOUGHT_KEY, str(int(now) - 1))
        with (patch('qq_connector.time.time', return_value=now),
              patch('qq_qzone.next_thought', return_value=int(now) + 3600),
              patch('qq_qzone.decide_auto_post', new_callable=AsyncMock,
                    side_effect=[None, '心里放着一个人，连安静的时候都觉得不算空。']) as decide):
            await self.connector._qzone_auto_tick(ws)
            await self.connector._qzone_auto_tick(ws)
            await self.connector._qzone_auto_tick(ws, cue=True)
        self.assertEqual(decide.await_count, 2)
        posts = [p for name, p in self.calls if name == 'send_qzone_msg']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['ugc_right'], 4)
        self.assertEqual(self.memory.get_setting(42, qq_qzone.AUTO_NEXT_THOUGHT_KEY), str(int(now) + 3600))

    async def test_no_daytime_or_daily_limit_even_when_recently_posted(self):
        now = datetime(2026, 9, 24, 2, tzinfo=qq_qzone.BEIJING).timestamp()
        self.connector.ws = ws = object()
        self.memory.set_setting(42, qq_qzone.AUTO_LAST_POST_KEY, str(int(now) - 60))
        with (patch('qq_connector.time.time', return_value=now),
              patch('qq_qzone.decide_auto_post', new_callable=AsyncMock,
                    return_value='半夜忽然想起你，心里还是软了一下。') as decide):
            await self.connector._qzone_auto_tick(ws)
        decide.assert_awaited_once()
        self.assertTrue(any(name == 'send_qzone_msg' for name, _ in self.calls))

    async def test_auto_can_be_disabled_without_touching_manual_draft(self):
        await self.connector.handle_event(None, self.event('关闭自动空间说说'))
        self.assertEqual(self.memory.get_setting(42, qq_qzone.AUTO_ENABLED_KEY), '0')
        self.connector.ws = ws = object()
        with patch('qq_qzone.decide_auto_post', new_callable=AsyncMock) as decide:
            await self.connector._qzone_auto_tick(ws)
        decide.assert_not_awaited()
        self.model.ask_claude.return_value = '手动草稿'
        await self.connector.handle_event(None, self.event('写条空间说说：手动草稿', mid=2))
        self.assertEqual(qq_qzone.pending(self.memory.get_setting(42, qq_qzone.PENDING_KEY)), '手动草稿')


if __name__ == '__main__':
    unittest.main()
