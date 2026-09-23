import asyncio
import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import voice_output


class VoiceDeliveryTests(unittest.TestCase):
    def test_mode_and_reply_selection(self):
        self.assertEqual(voice_output.mode(lambda chat, key: None, 7), "auto")
        self.assertFalse(voice_output.wants_audio("auto", user_text="今天怎么样"))
        self.assertTrue(voice_output.wants_audio("auto", from_voice=True))
        self.assertTrue(voice_output.wants_audio("auto", user_text="给我发条语音"))
        self.assertTrue(voice_output.wants_audio("auto", user_text="想听你说话"))
        self.assertTrue(voice_output.wants_audio("auto", user_text="能用声音回我吗"))
        self.assertFalse(voice_output.wants_audio("off", from_voice=True))
        self.assertTrue(voice_output.wants_audio("on", proactive=True))
        self.assertFalse(voice_output.wants_audio("auto", proactive=True))
        self.assertFalse(voice_output.wants_audio("auto", user_text="别给我发语音"))

    def test_auto_speaks_sometimes_without_an_explicit_request(self):
        now = datetime(2026, 9, 23, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
        values = {}
        get = lambda chat, key: values.get((chat, key))
        set_value = lambda chat, key, value: values.__setitem__((chat, key), value)
        selection = dict(user_text="今天好累", reply_text="过来，我抱你一会儿。", now=now)
        self.assertTrue(voice_output.auto_spontaneous(get, 7, chance=0.2, **selection))
        self.assertFalse(voice_output.auto_spontaneous(get, 7, chance=0.6, **selection))
        voice_output.record_auto_voice(set_value, 7, sent_at=now.timestamp())
        self.assertFalse(voice_output.auto_spontaneous(get, 7, chance=0, **selection))
        self.assertTrue(voice_output.auto_spontaneous(get, 7, chance=0, **{**selection, "now": now + timedelta(minutes=41)}))

    def test_auto_does_not_ambush_sleep_or_unanswered_messages(self):
        get = lambda chat, key: None
        now = datetime(2026, 9, 23, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertFalse(voice_output.auto_spontaneous(get, 7, user_text="好累", reply_text="我陪你。", now=now, chance=0))
        now = now.replace(hour=20)
        self.assertFalse(voice_output.auto_spontaneous(get, 7, user_text="只要文字", reply_text="我陪你。", now=now, chance=0))
        self.assertFalse(voice_output.auto_spontaneous(get, 7, reply_text="我陪你。", proactive=True, unanswered=True, now=now, chance=0))
        self.assertFalse(voice_output.auto_spontaneous(get, 7, user_text="现在几点", reply_text="晚上八点。", now=now, chance=0))

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

    def test_fish_clone_creates_private_reusable_voice_without_transcript(self):
        sent = {}

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"_id": "fish-voice-id", "state": "trained"}

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

        with patch.dict(sys.modules, {"httpx": types.SimpleNamespace(AsyncClient=Client)}), patch.object(voice_output, "FISH_API_KEY", "test-key"):
            voice_id = asyncio.run(voice_output.upload_fish_voice(b"audio", "sample.mp3", "audio/mpeg", "qinche"))
        self.assertEqual(voice_id, "fish-voice-id")
        self.assertEqual(sent["data"]["visibility"], "private")
        self.assertEqual(sent["files"]["voices"], ("sample.mp3", b"audio", "audio/mpeg"))

    def test_fish_synthesis_uses_saved_voice_id(self):
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
                sent.update(url=url, **kwargs)
                return Response()

        with patch.dict(sys.modules, {"httpx": types.SimpleNamespace(AsyncClient=Client)}), patch.object(voice_output, "FISH_API_KEY", "test-key"):
            audio = asyncio.run(voice_output.synthesize("在呢", "custom", "fish", "fish-voice-id"))
        self.assertEqual(audio, b"mp3 bytes")
        self.assertEqual(sent["headers"]["model"], voice_output.FISH_MODEL)
        self.assertEqual(sent["json"]["reference_id"], "fish-voice-id")

    def test_existing_fish_voice_link_extracts_model_id(self):
        model_id = "09af743d3e16464ba4d0f3b7f0e12fd5"
        self.assertEqual(voice_output.fish_voice_id(f"https://fish.audio/m/{model_id}/?share=true"), model_id)
        self.assertEqual(voice_output.fish_voice_id(model_id), model_id)
        self.assertEqual(voice_output.fish_voice_id("https://other.example/m/" + model_id), "")
        self.assertEqual(voice_output.fish_voice_id("not-a-model-id"), "")

    def test_existing_fish_voice_is_checked_before_selection(self):
        sent = {}

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"_id": "abc", "state": "trained"}

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                sent.update(url=url, **kwargs)
                return Response()

        with patch.dict(sys.modules, {"httpx": types.SimpleNamespace(AsyncClient=Client)}), patch.object(voice_output, "FISH_API_KEY", "test-key"):
            asyncio.run(voice_output.verify_fish_voice("09af743d3e16464ba4d0f3b7f0e12fd5"))
        self.assertEqual(sent["url"], "https://api.fish.audio/model/09af743d3e16464ba4d0f3b7f0e12fd5")

    def test_railway_default_fish_voice_is_used_without_chat_setup(self):
        model_id = "7cb18d1a283247f9a2c3771384690f98"
        settings = lambda chat, key: None
        with patch.object(voice_output, "FISH_API_KEY", "test-key"), patch.object(voice_output, "FISH_DEFAULT_VOICE_ID", model_id):
            self.assertEqual(voice_output.backend(settings, 7), "fish")
            self.assertEqual(voice_output.profile(settings, 7), "custom")
            self.assertEqual(voice_output.fish_voice(settings, 7), model_id)
            self.assertEqual(voice_output.fish_voice(lambda chat, key: "other-id" if key == voice_output.FISH_VOICE_KEY else None, 7), "other-id")
            self.assertEqual(voice_output.backend(lambda chat, key: "edge" if key == voice_output.BACKEND_KEY else None, 7), "edge")

    def test_fish_emotion_tags_are_sparse(self):
        self.assertEqual(voice_output.fish_auto_expression("太好了！今天这事终于成了。"), "excited")
        self.assertEqual(voice_output.fish_auto_expression("辛苦了。先缓一缓。"), "soft tone")
        self.assertEqual(voice_output.fish_auto_expression("后来呢？"), "curious")
        self.assertEqual(voice_output.fish_auto_expression("你回来了。今天怎么样？"), "")

    def test_fish_emotion_is_only_sent_to_tts_not_shown_in_caption(self):
        spoken, captions = [], []

        class Bot:
            async def send_voice(self, **kwargs):
                captions.append(kwargs["caption"])

        async def fake_synthesis(text, voice_profile="natural", provider="edge", custom_voice=""):
            spoken.append((text, custom_voice))
            return b"mp3"

        def setting(chat, key):
            return {voice_output.BACKEND_KEY: "fish", voice_output.FISH_VOICE_KEY: "existing-voice"}.get(key)

        fake_telegram = types.SimpleNamespace(InputFile=lambda stream, filename: stream)
        with patch.dict(sys.modules, {"telegram": fake_telegram}), patch.object(voice_output, "synthesize", fake_synthesis), patch.object(voice_output, "FISH_API_KEY", "test-key"):
            self.assertTrue(asyncio.run(voice_output.send_voice(Bot(), 7, "太好了！", get_setting=setting)))
        self.assertEqual(spoken[0], ("[excited] 太好了！", "existing-voice"))
        self.assertEqual(captions[0], "太好了！")

        def disabled(chat, key):
            if key == voice_output.FISH_EXPRESSION_KEY:
                return "off"
            return setting(chat, key)

        with patch.dict(sys.modules, {"telegram": fake_telegram}), patch.object(voice_output, "synthesize", fake_synthesis), patch.object(voice_output, "FISH_API_KEY", "test-key"):
            self.assertTrue(asyncio.run(voice_output.send_voice(Bot(), 7, "太好了！", get_setting=disabled)))
            self.assertTrue(asyncio.run(voice_output.send_voice(Bot(), 7, "太好了！", get_setting=disabled, fish_cue="soft tone")))
        self.assertEqual(spoken[1][0], "太好了！")
        self.assertEqual(spoken[2][0], "[soft tone] 太好了！")


if __name__ == "__main__":
    unittest.main()
