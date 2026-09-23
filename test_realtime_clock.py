import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import realtime_clock as clock


class ClockTests(unittest.TestCase):
    def test_casual_questions(self):
        for text in ["现在几点", "你知道现在几点了吗？", "知不知道现在几点啊", "几点了", "现在什么时间", "北京时间", "现在的时间", "你看看现在都几点了", "what time is it?"]:
            with self.subTest(text=text):
                self.assertEqual(clock.clock_question(text), "time")

    def test_do_not_hijack_other_time_meanings(self):
        for text in ["明天几点叫我", "昨天这个时候几点", "你说‘现在几点’是什么意思", "纽约现在几点", "现在几点，帮我计划晚上的安排", "几点", "我累了", "今天几号发工资"]:
            with self.subTest(text=text):
                self.assertIsNone(clock.clock_question(text))

    def test_beijing_rollover_and_weekday(self):
        instant = datetime(2026, 9, 23, 16, 1, tzinfo=timezone.utc)
        with patch.object(clock, "datetime") as dt:
            dt.now.side_effect = lambda tz: instant.astimezone(tz)
            self.assertEqual(clock.render_clock_reply("[[LIVE_CLOCK]]。", "date"), "9月24日。")
            self.assertEqual(clock.render_clock_reply("[[LIVE_CLOCK]]。", "weekday"), "星期四。")
            self.assertEqual(clock.now_local().hour, 0)
            dt.now.assert_called_with(clock.BEIJING)

    def test_every_query_refreshes_and_ignores_old_replies(self):
        history = [{"role": "assistant", "content": "现在凌晨两点。"}, {"role": "user", "content": "现在几点"}]
        with patch.object(clock, "now_local", side_effect=[
            datetime(2026, 9, 23, 23, 59, tzinfo=clock.BEIJING),
            datetime(2026, 9, 24, 0, 0, tzinfo=clock.BEIJING),
        ]):
            kind = clock.clock_question(clock.latest_user_text(history))
            self.assertEqual(clock.render_clock_reply("[[LIVE_CLOCK]]。", kind), "晚上十一点五十九分。")
            self.assertEqual(clock.render_clock_reply("[[LIVE_CLOCK]]。", kind), "凌晨十二点整。")

    def test_invalid_clock_templates_cannot_leak_or_invent_numbers(self):
        with patch.object(clock, "now_local", return_value=datetime(2026, 9, 23, 14, 5, tzinfo=clock.BEIJING)):
            for answer in ["[[LIVE_CLOCK]]，现在03:00", "现在凌晨三点", "[[LIVE_CLOCK]] [[LIVE_CLOCK]]", "[[LIVE_", "[[LIVE_CLOCK]]，[[OTHER]]"]:
                self.assertEqual(clock.render_clock_reply(answer, "time"), "下午两点零五分。")

    def test_style_retry_keeps_original_clock_question(self):
        messages = [{"role": "user", "content": "现在几点"}, {"role": "user", "content": "【生成校正，不是用户的新消息】请自然回复"}]
        self.assertEqual(clock.clock_question(clock.latest_user_text(messages)), "time")


class ClockTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Import the real transport functions without Telegram credentials,
        # external packages, database access, or actual outbound messages.
        stubs = {name: MagicMock() for name in ["httpx", "telegram", "telegram.constants", "telegram.ext", "worldbooks"]}
        with patch.dict(sys.modules, stubs):
            spec = importlib.util.spec_from_file_location("clock_bot_under_test", Path(__file__).with_name("bot.py"))
            self.bot = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.bot)
        self.bot.build_system_prompt = lambda chat: "persona plus stale historical time 02:00"

    async def test_voice_request_generates_spoken_reply_and_retries_empty_promise(self):
        from types import SimpleNamespace
        import voice_output

        self.bot.stream_preferences[7] = False
        self.bot.current_model = lambda chat: "mock"
        self.bot.get_setting = lambda chat, key: None
        self.bot.ask_claude = AsyncMock(side_effect=["等一下，我给你发语音。", "你回来了。今天想听我说什么？"])
        self.bot.safe_reply = AsyncMock()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=7),
            message=SimpleNamespace(message_id=3, from_voice=False),
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        entry = {"role": "user", "content": "给我发条语音，陪我聊聊"}
        with patch.object(voice_output, "send_voice", new_callable=AsyncMock, return_value=True) as send:
            await self.bot.generate_and_send(update, context, entry, entry)
        self.assertEqual(self.bot.ask_claude.await_count, 2)
        self.assertIn("直接合成语音", self.bot.ask_claude.call_args_list[0].args[1][-1]["content"])
        self.assertEqual(send.await_args.args[2], "你回来了。今天想听我说什么？")
        self.assertEqual(self.bot.histories[7][-2:], [entry, {"role": "assistant", "content": "你回来了。今天想听我说什么？"}])
        self.bot.safe_reply.assert_not_awaited()

    async def test_voice_failure_sends_actual_reply_as_text(self):
        from types import SimpleNamespace
        import voice_output

        self.bot.stream_preferences[8] = False
        self.bot.current_model = lambda chat: "mock"
        self.bot.get_setting = lambda chat, key: None
        self.bot.ask_claude = AsyncMock(return_value="在呢。靠过来，我陪你。")
        self.bot.safe_reply = AsyncMock()
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=8), message=SimpleNamespace(message_id=4))
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        entry = {"role": "user", "content": "发语音给我"}
        with patch.object(voice_output, "send_voice", new_callable=AsyncMock, return_value=False):
            await self.bot.generate_and_send(update, context, entry, entry)
        self.bot.safe_reply.assert_awaited_once_with(update, "在呢。靠过来，我陪你。")

    async def test_clock_preserves_context_and_voice_in_both_modes(self):
        messages = [{"role": "user", "content": "你知道现在几点了吗"}]
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "[[LIVE_CLOCK]]了。你刚才说的那件事，我还记着。"}}]}
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        self.bot.httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=client)
        with patch.object(clock, "now_local", return_value=datetime(2026, 9, 23, 14, 32, tzinfo=clock.BEIJING)):
            expected = "下午两点三十二分了。你刚才说的那件事，我还记着。"
            self.assertEqual(await self.bot.ask_claude(1, messages, "mock"), expected)
            self.assertEqual([s async for s in self.bot.stream_claude(1, messages, "mock")], [expected])
        self.assertEqual(client.post.await_count, 2)
        client.stream.assert_not_called()
        self.assertEqual(client.post.call_args.kwargs["json"]["messages"][1:], messages)

    async def test_time_is_sampled_after_generation(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "[[LIVE_CLOCK]]了。"}}]}
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        self.bot.httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=client)
        with patch.object(clock, "now_local", side_effect=[
            datetime(2026, 9, 23, 23, 59, tzinfo=clock.BEIJING),
            datetime(2026, 9, 24, 0, 0, tzinfo=clock.BEIJING),
        ]):
            result = await self.bot.ask_claude(1, [{"role": "user", "content": "今天几号"}], "mock")
        self.assertEqual(result, "9月24日了。")

    async def test_fresh_clock_at_end_of_actual_request_both_modes(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "reply"}}]}
        async def lines():
            yield 'data: {"choices":[{"delta":{"content":"reply"}}]}'
            yield 'data: [DONE]'
        response.aiter_lines = lines
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        client.stream.return_value.__aenter__ = AsyncMock(return_value=response)
        self.bot.httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=client)
        messages = [{"role": "user", "content": "还在工作呢"}]
        for hour in [9, 21]:
            with patch.object(clock, "now_local", return_value=datetime(2026, 9, 23, hour, 32, tzinfo=clock.BEIJING)):
                await self.bot.ask_claude(1, messages, "mock")
                chunks = [s async for s in self.bot.stream_claude(1, messages, "mock")]
                self.assertEqual(chunks, ["reply"])
                for call in [client.post.call_args, client.stream.call_args]:
                    payload = call.kwargs["json"]
                    system = payload["messages"][0]["content"]
                    self.assertIn(f"2026-09-23 {hour:02d}:32:00", system)
                    self.assertGreater(system.index("本次请求的实时时钟"), system.index("stale historical"))
                    self.assertEqual(payload["messages"][1:], messages)


if __name__ == "__main__":
    unittest.main()
