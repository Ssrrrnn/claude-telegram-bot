import asyncio
import sys
import types
import unittest
from unittest.mock import patch

import voice_output


class VoiceDeliveryTests(unittest.TestCase):
    def test_mode_and_reply_selection(self):
        self.assertEqual(voice_output.mode(lambda chat, key: None, 7), "auto")
        self.assertFalse(voice_output.wants_audio("auto", user_text="今天怎么样"))
        self.assertTrue(voice_output.wants_audio("auto", from_voice=True))
        self.assertTrue(voice_output.wants_audio("auto", user_text="给我发条语音"))
        self.assertFalse(voice_output.wants_audio("off", from_voice=True))
        self.assertTrue(voice_output.wants_audio("on", proactive=True))
        self.assertFalse(voice_output.wants_audio("auto", proactive=True))

    def test_voice_failure_returns_false_for_text_fallback(self):
        class BrokenBot:
            async def send_voice(self, **kwargs):
                raise RuntimeError("telegram unavailable")

        async def fake_synthesis(text):
            return b"audio"

        fake_telegram = types.SimpleNamespace(InputFile=lambda stream, filename: stream)
        with patch.dict(sys.modules, {"telegram": fake_telegram}), patch.object(voice_output, "synthesize", fake_synthesis):
            self.assertFalse(asyncio.run(voice_output.send_voice(BrokenBot(), 7, "我在这儿")))

    def test_voice_sends_mp3_with_readable_caption(self):
        sent = {}

        class FakeBot:
            async def send_voice(self, **kwargs):
                sent.update(kwargs)

        async def fake_synthesis(text):
            self.assertEqual(text, "我在这儿。")
            return b"mp3 bytes"

        fake_telegram = types.SimpleNamespace(InputFile=lambda stream, filename: (stream.read(), filename))
        with patch.dict(sys.modules, {"telegram": fake_telegram}), patch.object(voice_output, "synthesize", fake_synthesis):
            self.assertTrue(asyncio.run(voice_output.send_voice(FakeBot(), 7, "我在这儿。", reply_to_message_id=3)))
        self.assertEqual(sent["voice"], (b"mp3 bytes", "reply.mp3"))
        self.assertEqual(sent["caption"], "我在这儿。")
        self.assertEqual(sent["reply_to_message_id"], 3)


if __name__ == "__main__":
    unittest.main()
