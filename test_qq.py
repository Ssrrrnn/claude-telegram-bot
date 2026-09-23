import unittest
import time
from unittest.mock import AsyncMock, patch

from qq_connector import QQConnector, _message_parts, _qq_image_url, last_chat_platform
from qq_gateway import QQGateway
import qq_stickers


class FakeMemory:
    def __init__(self):
        self.settings = {}
        self.saved = {}

    def get_setting(self, chat_id, key):
        return self.settings.get((chat_id, key))

    def set_setting(self, chat_id, key, value):
        self.settings[(chat_id, key)] = value

    def _connect(self):
        memory = self

        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def cursor(self): return self
            def execute(self, sql, params): self.params = params
            def fetchone(self):
                answer = memory.saved.get(self.params)
                return {"assistant_text": answer} if answer else None

        return Connection()


class QQTests(unittest.IsolatedAsyncioTestCase):
    async def test_spontaneous_sticker_needs_matching_tone_and_cooldown(self):
        memory = FakeMemory()
        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=None, bot=object(), memory=memory)
        connector._send_market_sticker = AsyncMock()
        stickers = [
            {"emoji_package_id": 1, "emoji_id": "1", "key": "token", "summary": "抱抱"},
            {"emoji_package_id": 1, "emoji_id": "2", "key": "token", "summary": "大笑"},
        ]
        memory.set_setting(42, qq_stickers.LAST_SENT_KEY, str(time.time()))
        with patch("qq_connector.random.random", return_value=0):
            self.assertFalse(await connector._maybe_send_sticker(None, "12345678", "早", [
                {"type": "text", "text": "想你，抱一下。"}], stickers))
        connector._send_market_sticker.assert_not_awaited()
        memory.set_setting(42, qq_stickers.LAST_SENT_KEY, "0")
        with patch("qq_connector.random.random", return_value=0):
            self.assertTrue(await connector._maybe_send_sticker(None, "12345678", "早", [
                {"type": "text", "text": "想你，抱一下。"}], stickers))
        self.assertEqual(connector._send_market_sticker.await_args.args[-1]["summary"], "抱抱")
        self.assertFalse(await connector._maybe_send_sticker(None, "12345678", "早", [
            {"type": "text", "text": "想你，抱一下。"}], stickers))

    async def test_owner_sticker_is_saved_and_can_be_sent_as_qq_market_sticker(self):
        memory = FakeMemory()
        memory.set_setting(42, "qq_owner_uin_v1", "12345678")

        async def handler(update, context):
            await update.message.reply_text("好，给你。")

        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=handler, bot=object(), memory=memory)
        sent = []

        async def action(ws, name, params):
            sent.append((name, params))
            return {"message_id": 10}

        connector.action = action
        base = {"post_type": "message", "message_type": "private", "self_id": 88888888,
                "user_id": 12345678}
        sticker = {"emoji_package_id": 1, "emoji_id": "hello", "key": "token", "summary": "抱抱"}
        await connector.handle_event(None, {**base, "message_id": 1,
            "message": [{"type": "mface", "data": sticker}]})
        self.assertEqual(len(qq_stickers.load(memory.get_setting(42, qq_stickers.SETTING_KEY))), 1)
        self.assertEqual(len(sent), 1)  # Receiving it does not immediately echo it back.

        await connector.handle_event(None, {**base, "message_id": 2,
            "message": [{"type": "text", "data": {"text": "给我发个表情包"}}]})
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[-1][1]["message"], [{"type": "mface", "data": sticker}])
        self.assertTrue(memory.get_setting(42, qq_stickers.LAST_SENT_KEY))

    def test_sticker_requests_respect_negation_and_saved_stickers_are_bounded(self):
        self.assertTrue(qq_stickers.explicit_request("来张表情包"))
        self.assertFalse(qq_stickers.explicit_request("别发个表情包了"))
        self.assertFalse(qq_stickers.explicit_request("怎么还没发表情包"))
        self.assertEqual(qq_stickers.remember(None, {"message": [{"type": "mface",
            "data": {"emoji_package_id": 1, "emoji_id": "", "key": "bad"}}]}), ([], False))

    def test_qq_sticker_segments_are_not_dropped(self):
        event = {"message": [
            {"type": "text", "data": {"text": "给你"}},
            {"type": "face", "data": {"id": "14"}},
            {"type": "mface", "data": {"summary": "抱抱"}},
            {"type": "image", "data": {"file": "img-id", "url": "https://gchat.qpic.cn/a"}},
        ]}
        text, images = _message_parts(event)
        self.assertEqual(text, "给你[QQ表情：微笑][QQ表情包：抱抱][用户发送了一张图片或表情包]")
        self.assertEqual(len(images), 1)
        self.assertIsNone(_qq_image_url("http://localhost/private"))
        self.assertIsNone(_qq_image_url("https://gchat.qpic.cn.evil.example/a"))
        self.assertEqual(_qq_image_url("http://gchat.qpic.cn/a"), "https://gchat.qpic.cn/a")

    async def test_qq_image_reaches_multimodal_handler(self):
        memory = FakeMemory()
        memory.set_setting(42, "qq_owner_uin_v1", "12345678")
        received = []

        async def handler(update, context):
            received.append((update.message.text, update.message.image_base64, update.message.image_mime))
            await update.message.reply_text("看到了。")

        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=handler, bot=object(), memory=memory)
        connector._image_content = lambda ws, data: fake_image()
        sent = []

        async def fake_image(): return "YWJj", "image/jpeg"
        async def send_text(ws, sender, text): sent.append(text)
        connector._send_text = send_text
        await connector.handle_event(None, {
            "post_type": "message", "message_type": "private", "self_id": 88888888,
            "user_id": 12345678, "message_id": 14,
            "message": [{"type": "image", "data": {"url": "https://gchat.qpic.cn/a"}}],
        })
        self.assertEqual(received, [("[用户发送了一张图片或表情包]", "YWJj", "image/jpeg")])
        self.assertEqual(sent, ["看到了。"])

    def test_first_upgrade_recovers_last_human_platform(self):
        class Memory:
            def __init__(self, message_id): self.message_id = message_id
            def _connect(self): return self
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def cursor(self): return self
            def execute(self, query, params): pass
            def fetchone(self): return {"telegram_message_id": self.message_id}

        self.assertEqual(last_chat_platform(Memory(-123), 42), "qq")
        self.assertEqual(last_chat_platform(Memory(123), 42), "telegram")

    async def test_proactive_sends_to_paired_qq_owner(self):
        memory = FakeMemory()
        memory.set_setting(42, "qq_owner_uin_v1", "12345678")
        connector = QQConnector(
            owner_chat_id=42, pair_code="secret-pairing-code-123",
            expected_self_uin="88888888", handler=None,
            bot=None, memory=memory,
        )
        connector.ws = object()
        delivered = []

        async def send_text(ws, sender, text):
            delivered.append((sender, text))

        connector._send_text = send_text
        sent_as_voice = await connector.send_proactive("想你了。", ["想你了。"])
        self.assertFalse(sent_as_voice)
        self.assertEqual(delivered, [("12345678", "想你了。")])

    async def test_pairing_accepts_only_code_in_private_message_and_then_owner(self):
        memory = FakeMemory()
        received = []

        async def handler(update, context):
            received.append((update.effective_chat.id, update.message.text))
            await update.message.reply_text("好，记得你。")

        connector = QQConnector(
            owner_chat_id=42, pair_code="secret-pairing-code-123",
            expected_self_uin="88888888", handler=handler,
            bot=object(), memory=memory,
        )
        sent = []

        async def send_text(ws, sender, text):
            sent.append((sender, text))

        async def send_replies(ws, sender, replies):
            sent.extend((sender, r["text"]) for r in replies)

        connector._send_text = send_text
        connector._send_replies = send_replies
        base = {"post_type": "message", "message_type": "private", "self_id": 88888888}

        await connector.handle_event(None, {**base, "user_id": 12345678, "message_id": 1, "message": [{"type": "text", "data": {"text": "你好"}}]})
        await connector.handle_event(None, {**base, "message_type": "group", "user_id": 12345678, "message_id": 2, "message": [{"type": "text", "data": {"text": "绑定 secret-pairing-code-123"}}]})
        self.assertEqual(sent, [])

        await connector.handle_event(None, {**base, "user_id": 12345678, "message_id": 3, "message": [{"type": "text", "data": {"text": "绑定 secret-pairing-code-123"}}]})
        self.assertEqual(memory.get_setting(42, "qq_owner_uin_v1"), "12345678")

        await connector.handle_event(None, {**base, "user_id": 87654321, "message_id": 4, "message": [{"type": "text", "data": {"text": "你好"}}]})
        await connector.handle_event(None, {**base, "user_id": 12345678, "message_id": 5, "message": [{"type": "text", "data": {"text": "你好"}}]})
        self.assertEqual(received, [(42, "你好")])
        self.assertEqual(sent[-1], ("12345678", "好，记得你。"))

    async def test_gateway_reuses_telegram_chat_and_deduplicates_qq_message(self):
        memory = FakeMemory()
        processed = []

        async def handler(update, context):
            processed.append(update.message.message_id)
            await update.message.reply_text("回来了。")
            memory.saved[(update.effective_chat.id, update.message.message_id)] = "回来了。"

        gateway = QQGateway(
            owner_uin="12345678", shared_chat_id=42,
            handler=handler, bot=object(), memory=memory,
        )
        event = {"sender_id": "12345678", "message_id": 321, "text": "我回来了"}
        first = await gateway.process(event)
        second = await gateway.process(event)
        self.assertEqual(processed, [-321])
        self.assertEqual(first["replies"][0]["text"], "回来了。")
        self.assertTrue(second["duplicate"])
        with self.assertRaises(ValueError):
            await gateway.process({**event, "sender_id": "87654321"})


if __name__ == "__main__":
    unittest.main()
