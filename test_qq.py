import unittest
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

from qq_connector import QQConnector, _message_parts, _qq_image_url, last_chat_platform
from qq_gateway import QQGateway
import qq_stickers
import qq_status


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
    def test_qq_status_uses_beijing_time_and_actual_chat_activity(self):
        late = datetime(2026, 9, 24, 1, 0, tzinfo=qq_status.BEIJING).timestamp()
        daytime = datetime(2026, 9, 24, 14, 0, tzinfo=qq_status.BEIJING).timestamp()
        self.assertEqual(qq_status.choose(late)[1], "恋爱中")
        self.assertEqual(qq_status.choose(late, last_user_at=late - 10 * 60)[1], "熬夜中")
        self.assertEqual(qq_status.choose(late, last_user_at=late - 2 * 3600)[1], "恋爱中")
        self.assertEqual(qq_status.choose(daytime, last_user_at=daytime)[1], "恋爱中")
        self.assertEqual(qq_status.choose(late, last_user_at=late, love_until=late + 60)[1], "爱你")
        self.assertTrue(qq_status.affectionate("只喜欢你"))
        self.assertFalse(qq_status.affectionate("我不爱你"))

    async def test_qq_status_changes_only_when_needed_and_can_be_disabled(self):
        memory = FakeMemory()
        memory.set_setting(42, "qq_owner_uin_v1", "12345678")
        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=None, bot=object(), memory=memory)
        ws = object()
        connector.ws = ws
        sent = []

        async def action(ws, name, params):
            sent.append((name, params))
            return {}

        connector.action = action
        daytime = datetime(2026, 9, 24, 14, 0, tzinfo=qq_status.BEIJING).timestamp()
        with patch("qq_connector.time.time", return_value=daytime):
            await connector._refresh_status(ws)
            await connector._refresh_status(ws)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0], ("set_online_status", {
                "status": 10, "ext_status": 1051, "battery_status": 0,
            }))
            connector.love_until = daytime + qq_status.LOVE_SECONDS
            await connector._refresh_status(ws, urgent=True)
            self.assertEqual(sent[-1][1]["ext_status"], 2006)
        with patch.dict("os.environ", {"QQ_AUTO_STATUS": "off"}):
            connector.last_status = None
            await connector._refresh_status(ws)
            self.assertEqual(len(sent), 2)

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
        self.assertEqual(len([name for name, _ in sent if name == "send_private_msg"]), 1)

        await connector.handle_event(None, {**base, "message_id": 2,
            "message": [{"type": "text", "data": {"text": "给我发个表情包"}}]})
        self.assertEqual(len([name for name, _ in sent if name == "send_private_msg"]), 3)
        self.assertEqual([params for name, params in sent if name == "send_private_msg"][-1]["message"],
                         [{"type": "mface", "data": sticker}])
        self.assertTrue(memory.get_setting(42, qq_stickers.LAST_SENT_KEY))

    def test_sticker_requests_respect_negation_and_saved_stickers_are_bounded(self):
        self.assertTrue(qq_stickers.explicit_request("来张表情包"))
        self.assertTrue(qq_stickers.explicit_request("发表情包给我"))
        self.assertFalse(qq_stickers.explicit_request("别发个表情包了"))
        self.assertFalse(qq_stickers.explicit_request("怎么还没发表情包"))
        self.assertEqual(qq_stickers.remember(None, {"message": [{"type": "mface",
            "data": {"emoji_package_id": 1, "emoji_id": "", "key": "bad"}}]}), ([], False))

    async def test_owner_image_is_saved_before_download_timeout_and_can_be_sent(self):
        memory = FakeMemory()
        memory.set_setting(42, "qq_owner_uin_v1", "12345678")
        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=None, bot=object(), memory=memory)
        sent = []

        async def action(ws, name, params):
            sent.append((name, params))
            if name == "get_msg":
                return {"message": [{"type": "image", "data": {"file": "sticker.gif"}}]}
            return {"message_id": 100}

        async def unavailable(ws, data): raise TimeoutError()
        connector.action = action
        connector._image_content = unavailable
        base = {"post_type": "message", "message_type": "private", "self_id": 88888888,
                "user_id": 12345678}
        await connector.handle_event(None, {**base, "message_id": 42, "message": [
            {"type": "image", "data": {"file": "sticker.gif", "sub_type": 1}}]})
        saved = qq_stickers.load(memory.get_setting(42, qq_stickers.SETTING_KEY))
        self.assertEqual(saved[0]["file"], "sticker.gif")
        self.assertEqual(sent[-1][1]["message"], "这张图我收到了，但现在没加载出来，暂时看不清内容。")

        async def handler(update, context): await update.message.reply_text("发给你。")
        connector.handler = handler
        connector.gateway = None
        await connector.handle_event(None, {**base, "message_id": 43, "message": [
            {"type": "text", "data": {"text": "发表情包给我"}}]})
        self.assertEqual([p for name, p in sent if name == "send_private_msg"][-1]["message"], [{"type": "image", "data": {
            "file": "sticker.gif", "sub_type": 1}}])

    async def test_sticker_request_recovers_images_from_owner_history_only(self):
        memory = FakeMemory()
        memory.set_setting(42, "qq_owner_uin_v1", "12345678")

        async def handler(update, context): await update.message.reply_text("来了。")
        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=handler, bot=object(), memory=memory)
        sent = []

        async def action(ws, name, params):
            sent.append((name, params))
            if name == "get_friend_msg_history":
                return {"messages": [
                    {"user_id": 99999999, "message_id": 1, "message": [
                        {"type": "image", "data": {"file": "other.jpg"}}]},
                    {"user_id": 12345678, "message_id": 2, "message": [
                        {"type": "image", "data": {"file": "owner.gif", "sub_type": 1}}]},
                ]}
            return {"message_id": 5}

        connector.action = action
        await connector.handle_event(None, {"post_type": "message", "message_type": "private",
            "self_id": 88888888, "user_id": 12345678, "message_id": 4,
            "message": [{"type": "text", "data": {"text": "来张表情包"}}]})
        self.assertEqual([p for name, p in sent if name == "send_private_msg"][-1]["message"][0]["data"]["file"], "owner.gif")
        self.assertEqual([item["file"] for item in qq_stickers.load(
            memory.get_setting(42, qq_stickers.SETTING_KEY))], ["owner.gif"])

    async def test_market_sticker_is_discovered_even_with_saved_images(self):
        memory = FakeMemory()
        memory.set_setting(42, "qq_owner_uin_v1", "12345678")
        initial = {"kind": "image", "file": "image.gif", "message_id": 2,
                   "summary": "[图片表情]", "sub_type": 1}
        memory.set_setting(42, qq_stickers.SETTING_KEY, __import__('json').dumps([initial]))

        async def handler(update, context): await update.message.reply_text("给你挑了一张。")
        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=handler, bot=object(), memory=memory)
        sent = []

        async def action(ws, name, params):
            sent.append((name, params))
            if name == "get_friend_msg_history":
                return {"messages": [{"user_id": 12345678, "message_id": 3, "message": [
                    {"type": "image", "data": {"file": "another.gif", "sub_type": 1}},
                    {"type": "image", "data": {"emoji_package_id": "12", "emoji_id": "market-1",
                                                "key": "qq-market-key", "summary": "抱抱"}}]}]}
            return {"message_id": 10}

        connector.action = action
        event = {"post_type": "message", "message_type": "private", "self_id": 88888888,
                 "user_id": 12345678, "message_id": 4,
                 "message": [{"type": "text", "data": {"text": "给我发个表情包"}}]}
        await connector.handle_event(None, event)
        expected = {"emoji_package_id": 12, "emoji_id": "market-1", "key": "qq-market-key", "summary": "抱抱"}
        self.assertEqual([p for name, p in sent if name == "send_private_msg"][-1]["message"],
                         [{"type": "mface", "data": expected}])
        self.assertEqual(len(qq_stickers.load(memory.get_setting(42, qq_stickers.SETTING_KEY))), 2)
        await connector.handle_event(None, {**event, "message_id": 5})
        self.assertEqual(len([name for name, _ in sent if name == "get_friend_msg_history"]), 1)

    def test_untrusted_image_files_are_not_saved(self):
        for name in ("/etc/passwd", "https://qpic.cn/pic", "base64://AAAA", "..\\bad"):
            stickers, changed = qq_stickers.remember(None, {"message_id": 1, "message": [
                {"type": "image", "data": {"file": name}}]})
            self.assertEqual((stickers, changed), ([], False))

    async def test_custom_qq_expressions_can_be_sent_later_but_photos_cannot(self):
        memory = FakeMemory()
        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=None, bot=object(), memory=memory)
        connector._send_sticker = AsyncMock()
        stickers = [
            {"kind": "image", "file": "photo.jpg", "message_id": 1, "summary": "[图片]", "sub_type": 0},
            {"kind": "image", "file": "expression.gif", "message_id": 2, "summary": "[图片表情]", "sub_type": 1},
        ]
        with patch("qq_connector.random.random", return_value=0):
            self.assertFalse(await connector._maybe_send_sticker(None, "12345678", "抱抱", [
                {"type": "text", "text": "抱一下。"}], stickers, incoming_sticker=True))
            self.assertTrue(await connector._maybe_send_sticker(None, "12345678", "好", [
                {"type": "text", "text": "抱一下。"}], stickers))
        self.assertEqual(connector._send_sticker.await_args.args[-1]["file"], "expression.gif")
        self.assertEqual(qq_stickers.fitting_stickers(stickers, "明天见。"), [])

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

    async def test_qq_voice_is_transcribed_and_keeps_voice_reply_mode(self):
        memory = FakeMemory()
        memory.set_setting(42, "qq_owner_uin_v1", "12345678")
        received, sent = [], []

        async def handler(update, context):
            received.append((update.message.text, update.message.from_voice))
            await update.message.reply_text("听见了。")

        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=handler, bot=object(), memory=memory)

        async def action(ws, name, params):
            sent.append((name, params))
            return {"text": "今天好想你"} if name == "fetch_ptt_text" else {}

        connector.action = action
        await connector.handle_event(None, {"post_type": "message", "message_type": "private",
            "self_id": 88888888, "user_id": 12345678, "message_id": 4,
            "message": [{"type": "record", "data": {"file": "record-id"}}]})
        self.assertEqual(received, [("今天好想你", True)])
        self.assertEqual([p["message"] for n, p in sent if n == "send_private_msg"], ["听见了。"])
        self.assertEqual([p["event_type"] for n, p in sent if n == "set_input_status"], [1, 0])

    async def test_qq_voice_transcription_failure_gets_a_reply(self):
        memory = FakeMemory()
        memory.set_setting(42, "qq_owner_uin_v1", "12345678")
        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=AsyncMock(), bot=object(), memory=memory)
        sent = []

        async def action(ws, name, params):
            sent.append((name, params))
            return {"text": ""}

        connector.action = action
        await connector.handle_event(None, {"post_type": "message", "message_type": "private",
            "self_id": 88888888, "user_id": 12345678, "message_id": 5,
            "message": [{"type": "record", "data": {"file": "record-id"}}]})
        connector.handler.assert_not_awaited()
        self.assertEqual([p["message"] for n, p in sent if n == "send_private_msg"],
                         ["刚才那段语音没听清，打字告诉我，好吗？"])

    async def test_saved_custom_faces_are_only_sent_from_qq_media_hosts(self):
        memory = FakeMemory()
        connector = QQConnector(owner_chat_id=42, pair_code="secret-pairing-code-123",
                                expected_self_uin="88888888", handler=None, bot=object(), memory=memory)
        sent = []

        async def action(ws, name, params):
            sent.append((name, params))
            if name == "fetch_custom_face_detail":
                return [{"url": "http://gchat.qpic.cn/face.gif", "desc": "抱抱"},
                        {"url": "http://localhost/internal", "desc": "不可信"},
                        {"url": "https://bad.example/a", "desc": "不可信"}]
            return {}

        connector.action = action
        saved = await connector._saved_custom_faces(None)
        self.assertEqual(saved, [{"kind": "saved_custom", "url": "https://gchat.qpic.cn/face.gif",
                                  "summary": "抱抱"}])
        self.assertEqual(await connector._saved_custom_faces(None), saved)
        self.assertEqual(len([n for n, _ in sent if n == "fetch_custom_face_detail"]), 1)
        self.assertEqual(qq_stickers.fitting_stickers(saved, "来抱一下。"), saved)
        self.assertTrue(await connector._maybe_send_sticker(None, "12345678", "发个收藏的表情包", [], saved))
        self.assertEqual([p for n, p in sent if n == "send_private_msg"][-1]["message"],
                         [{"type": "image", "data": {"file": "https://gchat.qpic.cn/face.gif"}}])

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
