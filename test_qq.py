import unittest

from qq_connector import QQConnector
from qq_gateway import QQGateway


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
