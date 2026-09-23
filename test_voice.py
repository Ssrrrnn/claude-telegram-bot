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

        async def fake_synthesis(text, voice_profile="natural", provider="edge", custom_voice=""):
            return b"audio"

        fake_telegram = types.SimpleNamespace(InputFile=lambda stream, filename: stream)
        with patch.dict(sys.modules, {"telegram": fake_telegram}), patch.object(voice_output, "synthesize", fake_synthesis):
            self.assertFalse(asyncio.run(voice_output.send_voice(BrokenBot(), 7, "我在这儿")))

    def test_voice_sends_mp3_with_readable_caption(self):
        sent = {}

        class FakeBot:
            async def send_voice(self, **kwargs):
                sent.update(kwargs)

        async def fake_synthesis(text, voice_profile="natural", provider="edge", custom_voice=""):
            self.assertEqual(text, "我在这儿。")
            self.assertEqual(voice_profile, "natural")
            self.assertEqual(provider, "edge")
            return b"mp3 bytes"

        fake_telegram = types.SimpleNamespace(InputFile=lambda stream, filename: (stream.read(), filename))
        with patch.dict(sys.modules, {"telegram": fake_telegram}), patch.object(voice_output, "synthesize", fake_synthesis):
            self.assertTrue(asyncio.run(voice_output.send_voice(FakeBot(), 7, "我在这儿。", reply_to_message_id=3)))
        self.assertEqual(sent["voice"], (b"mp3 bytes", "reply.mp3"))
        self.assertEqual(sent["caption"], "我在这儿。")
        self.assertEqual(sent["reply_to_message_id"], 3)

    def test_cosyvoice_requires_credentials_and_keeps_provider_choice(self):
        setting = lambda chat, key: {voice_output.BACKEND_KEY: "cosyvoice"}.get(key)
        with patch.object(voice_output, "COSYVOICE_API_KEY", ""):
            self.assertEqual(voice_output.backend(setting, 7), "edge")
        with patch.object(voice_output, "COSYVOICE_API_KEY", "test-key"):
            self.assertEqual(voice_output.backend(setting, 7), "cosyvoice")

    def test_cosyvoice_uses_suzu_compatible_speech_request(self):
        recorded = {}

        class Response:
            content = b"mp3 bytes"
            headers = {"content-type": "audio/mpeg"}

            def raise_for_status(self):
                pass

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                recorded.update(url=url, **kwargs)
                return Response()

        fake_httpx = types.SimpleNamespace(AsyncClient=Client)
        with patch.dict(sys.modules, {"httpx": fake_httpx}), patch.object(voice_output, "COSYVOICE_API_KEY", "test-key"):
            result = asyncio.run(voice_output.synthesize("我在这儿", "deep", "cosyvoice"))
        self.assertEqual(result, b"mp3 bytes")
        self.assertEqual(recorded["json"]["voice"], "FunAudioLLM/CosyVoice2-0.5B:benjamin")
        self.assertEqual(recorded["json"]["response_format"], "mp3")
        self.assertEqual(recorded["url"], voice_output.COSYVOICE_API_URL)

    def test_custom_voice_uri_is_used_without_model_prefix(self):
        sent = {}

        class Response:
            content = b"mp3 bytes"
            headers = {"content-type": "audio/mpeg"}

            def raise_for_status(self):
                pass

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                sent.update(kwargs)
                return Response()

        with patch.dict(sys.modules, {"httpx": types.SimpleNamespace(AsyncClient=Client)}), patch.object(voice_output, "COSYVOICE_API_KEY", "test-key"):
            result = asyncio.run(voice_output.synthesize("在呢", "custom", "cosyvoice", "speech:myvoice:abc"))
        self.assertEqual(result, b"mp3 bytes")
        self.assertEqual(sent["json"]["voice"], "speech:myvoice:abc")

    def test_upload_voice_sends_reference_and_returns_uri(self):
        sent = {}

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"uri": "speech:myvoice:abc"}

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                sent.update(url=url, **kwargs)
                return Response()

        with patch.dict(sys.modules, {"httpx": types.SimpleNamespace(AsyncClient=Client)}), patch.object(voice_output, "COSYVOICE_API_KEY", "test-key"):
            uri = asyncio.run(voice_output.upload_custom_voice(b"audio", "sample.mp3", "audio/mpeg", "myvoice", "今天天气不错"))
        self.assertEqual(uri, "speech:myvoice:abc")
        self.assertEqual(sent["data"]["text"], "今天天气不错")
        self.assertEqual(sent["files"]["file"], ("sample.mp3", b"audio", "audio/mpeg"))


if __name__ == "__main__":
    unittest.main()
