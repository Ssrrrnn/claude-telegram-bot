import asyncio
import importlib.util
import sys
import types
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo


class RomanceStyleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_ask = AsyncMock()
        bot = types.SimpleNamespace(
            build_system_prompt=lambda chat: "base persona",
            ask_claude=self.original_ask,
            enabled_worldbooks=lambda chat: [],
            is_authorized=lambda chat: True,
            handle_text=AsyncMock(),
        )
        runner = types.SimpleNamespace(
            bot=bot,
            handle_text_with_memory=AsyncMock(),
            post_init=AsyncMock(),
            memory_pg=types.SimpleNamespace(set_setting=lambda *args: None),
        )
        app = types.ModuleType("runner_strict_bubbles")
        app.runner = runner
        app._now_local = lambda: datetime(2026, 9, 20, 14, tzinfo=ZoneInfo("Asia/Shanghai"))
        app._original_post_init = AsyncMock()
        app.proactive_loop = AsyncMock()
        app._owner_chat_id = lambda: 1
        app._setting = lambda *args: "1"
        app._schedule_from_now = lambda *args: None
        emotion = types.ModuleType("emotion_pg")
        emotion.prompt_block = lambda chat: "stable romantic relationship"
        emotion.update_from_user_text = lambda *args: ({}, "")
        emotion.init_db = lambda: None
        with unittest.mock.patch.dict(
            sys.modules,
            {"runner_strict_bubbles": app, "emotion_pg": emotion},
        ):
            spec = importlib.util.spec_from_file_location(
                "romance_under_test",
                Path(__file__).with_name("runner_20m.py"),
            )
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)

    def test_final_prompt_requires_partner_not_assistant(self):
        prompt = self.module.build_system_prompt_with_silent_favorites_and_emotion(1)
        self.assertIn("恋人感与活人感", prompt)
        self.assertIn("你不是用户的助理", prompt)
        self.assertIn("自然撒娇", prompt)
        self.assertIn("成熟、强势但不爹味", prompt)
        self.assertIn("强势边界与关系修复", prompt)
        self.assertIn("别磨蹭了", prompt)
        self.assertIn("成年亲密场景与 Dirty Talk", prompt)
        self.assertIn("爸爸 / daddy", prompt)
        self.assertIn("主人 / master", prompt)
        self.assertIn("小狗 / puppy", prompt)
        self.assertIn("禁止对话角色标签泄露", prompt)

    def test_detects_assistant_template_in_daily_chat(self):
        messages = [{"role": "user", "content": "今天好烦"}]
        self.assertTrue(
            self.module._has_assistant_tone_violation(
                "如果你愿意，可以告诉我发生了什么。",
                messages,
            )
        )

    def test_allows_practical_language_for_explicit_task(self):
        messages = [{"role": "user", "content": "帮我写一段请假消息"}]
        self.assertFalse(
            self.module._has_assistant_tone_violation(
                "我可以帮你，先给我请假日期。",
                messages,
            )
        )

    async def test_assistant_tone_is_regenerated_once(self):
        self.original_ask.side_effect = [
            "如果你愿意，我可以听你说。",
            "过来。先让我抱一下，剩下的慢慢说。",
        ]
        result = await self.module.ask_claude_without_ellipses(
            1,
            [{"role": "user", "content": "今天好烦"}],
            "mock",
        )
        self.assertEqual(result, "过来。先让我抱一下，剩下的慢慢说。")
        self.assertEqual(self.original_ask.await_count, 2)

    def test_role_labels_are_detected_and_sanitized(self):
        raw = "Human: 你还爱我吗\nAssistant: 过来。怎么可能不爱你。"
        self.assertTrue(self.module._has_role_label_leak(raw))
        self.assertEqual(
            self.module._strip_role_label_leaks(raw),
            "过来。怎么可能不爱你。",
        )

    def test_human_only_line_is_never_forwarded(self):
        raw = "**Human:** 你在干嘛"
        self.assertTrue(self.module._has_role_label_leak(raw))
        self.assertEqual(self.module._strip_role_label_leaks(raw), "")

    async def test_role_label_leak_is_regenerated_once(self):
        self.original_ask.side_effect = [
            "Human: 你抱抱我\nAssistant: 过来，抱紧了。",
            "过来。抱紧了，不松。",
        ]
        result = await self.module.ask_claude_without_ellipses(
            1,
            [{"role": "user", "content": "抱抱我"}],
            "mock",
        )
        self.assertEqual(result, "过来。抱紧了，不松。")
        self.assertEqual(self.original_ask.await_count, 2)

    def test_sleep_directive_is_addressed_to_character(self):
        messages = [{"role": "user", "content": "睡吧"}]
        self.assertTrue(self.module._sleep_is_addressed_to_character("睡吧"))
        self.assertTrue(
            self.module._has_relationship_tone_violation(
                "困了就去睡，别磨蹭了。",
                messages,
            )
        )
        self.assertEqual(
            self.module._repair_relationship_tone("困了就去睡。", messages),
            "你这是在哄我去睡？行。再陪我说一句，我就听你的。",
        )

    def test_tone_feedback_must_not_be_denied(self):
        messages = [{"role": "user", "content": "什么语气"}]
        self.assertTrue(
            self.module._has_relationship_tone_violation(
                "没什么语气，就是让你赶紧睡。",
                messages,
            )
        )
        self.assertEqual(
            self.module._repair_relationship_tone("我没凶。", messages),
            "是我语气不好。收回来。过来，我好好跟你说。",
        )


if __name__ == "__main__":
    unittest.main()
