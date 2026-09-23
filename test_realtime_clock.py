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
            reply = clock.direct_clock_reply([{"role": "user", "content": "今天几号"}])
            self.assertEqual(reply, "按北京时间，今天是2026年9月24日，星期四。")
            self.assertEqual(clock.now_local().hour, 0)
            dt.now.assert_called_with(clock.BEIJING)

    def test_every_query_refreshes_and_ignores_old_replies(self):
        history = [{"role": "assistant", "content": "现在凌晨两点。"}, {"role": "user", "content": "现在几点"}]
        with patch.object(clock, "now_local", side_effect=[
            datetime(2026, 9, 23, 23, 59, tzinfo=clock.BEIJING),
            datetime(2026, 9, 24, 0, 0, tzinfo=clock.BEIJING),
        ]):
            self.assertEqual(clock.direct_clock_reply(history), "现在是北京时间 23:59。")
            self.assertEqual(clock.direct_clock_reply(history), "现在是北京时间 00:00。")


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

    async def test_direct_clock_bypasses_api_in_both_modes(self):
        messages = [{"role": "user", "content": "你知道现在几点了吗"}]
        with patch.object(clock, "now_local", return_value=datetime(2026, 9, 23, 14, 32, tzinfo=clock.BEIJING)):
            self.assertEqual(await self.bot.ask_claude(1, messages, "mock"), "现在是北京时间 14:32。")
            self.assertEqual([s async for s in self.bot.stream_claude(1, messages, "mock")], ["现在是北京时间 14:32。"])
        self.bot.httpx.AsyncClient.assert_not_called()

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
