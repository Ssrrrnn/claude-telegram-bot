"""Private OneBot WebSocket client for the Railway NapCat service."""

import asyncio
import json
import os
import uuid

from qq_gateway import QQGateway

OWNER_KEY = "qq_owner_uin_v1"
MAX_MESSAGE_CHARS = 4000


def _message_text(event):
    segments = event.get("message")
    if isinstance(segments, list):
        return "".join(
            str(segment.get("data", {}).get("text", ""))
            for segment in segments if isinstance(segment, dict) and segment.get("type") == "text"
        ).strip()
    return str(event.get("raw_message", "") or "").strip()


def _private_message(event, self_uin):
    if event.get("post_type") != "message" or event.get("message_type") != "private":
        return False
    sender = event.get("user_id")
    return str(sender).isdigit() and str(sender) != str(self_uin)


class QQConnector:
    def __init__(self, *, owner_chat_id, pair_code, expected_self_uin, handler, bot, memory):
        self.owner_chat_id = int(owner_chat_id)
        self.pair_code = pair_code
        self.expected_self_uin = str(expected_self_uin)
        self.handler = handler
        self.bot = bot
        self.memory = memory
        self.pending = {}
        self.gateway = None
        self.gateway_uin = None

    def owner_uin(self):
        configured = os.getenv("QQ_OWNER_UIN", "").strip()
        return configured or (self.memory.get_setting(self.owner_chat_id, OWNER_KEY) or "")

    def _gateway(self, owner_uin):
        if owner_uin != self.gateway_uin:
            self.gateway = QQGateway(
                owner_uin=owner_uin, shared_chat_id=self.owner_chat_id,
                handler=self.handler, bot=self.bot, memory=self.memory,
            )
            self.gateway_uin = owner_uin
        return self.gateway

    async def action(self, ws, name, params):
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await ws.send(json.dumps({"action": name, "params": params, "echo": echo}, ensure_ascii=False))
            result = await asyncio.wait_for(future, timeout=20)
            if result.get("status") != "ok":
                raise RuntimeError(f"OneBot action {name} failed")
            return result.get("data")
        finally:
            self.pending.pop(echo, None)

    async def _send_text(self, ws, sender, text):
        if not isinstance(text, str) or not text:
            return
        await self.action(ws, "send_private_msg", {"user_id": int(sender), "message": text})

    async def _send_replies(self, ws, sender, replies):
        for reply in replies:
            if reply.get("type") == "record":
                try:
                    await self.action(ws, "send_private_msg", {
                        "user_id": int(sender),
                        "message": [{"type": "record", "data": {"file": "base64://" + reply["audio_base64"]}}],
                    })
                    continue
                except Exception as exc:
                    print(f"QQ voice delivery failed: {type(exc).__name__}", flush=True)
                if reply.get("fallback_text"):
                    await self._send_text(ws, sender, reply["fallback_text"])
            elif reply.get("type") == "text":
                await self._send_text(ws, sender, reply.get("text", ""))

    async def handle_event(self, ws, event):
        self_uin = str(event.get("self_id", ""))
        if self.expected_self_uin and self_uin != self.expected_self_uin:
            return
        if not _private_message(event, self_uin):
            return
        sender = str(event["user_id"])
        text = _message_text(event)
        owner = self.owner_uin()
        if not owner:
            if self.pair_code and text == "绑定 " + self.pair_code:
                self.memory.set_setting(self.owner_chat_id, OWNER_KEY, sender)
                await self._send_text(ws, sender, "连上了。以后在这里说话，我也认得你。")
                print("QQ primary account paired", flush=True)
            return
        if sender != owner:
            return
        if text == "绑定 " + self.pair_code:
            await self._send_text(ws, sender, "已经连上了。")
            return
        if not text or len(text) > MAX_MESSAGE_CHARS:
            return
        message_id = event.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return
        try:
            response = await self._gateway(owner).process({
                "sender_id": sender, "message_id": message_id, "text": text,
            })
            await self._send_replies(ws, sender, response["replies"])
        except Exception as exc:
            print(f"QQ message handling failed: {type(exc).__name__}", flush=True)

    async def run(self, url, token):
        from websockets.asyncio.client import connect

        while True:
            try:
                async with connect(
                    url, additional_headers={"Authorization": "Bearer " + token},
                    max_size=2 * 1024 * 1024, ping_interval=25,
                ) as ws:
                    print("QQ OneBot private connection ready", flush=True)
                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                            if not isinstance(event, dict):
                                continue
                            echo = event.get("echo")
                            if echo in self.pending:
                                future = self.pending[echo]
                                if not future.done():
                                    future.set_result(event)
                            else:
                                asyncio.create_task(self.handle_event(ws, event))
                        except (ValueError, TypeError) as exc:
                            print(f"QQ OneBot event skipped: {type(exc).__name__}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"QQ OneBot reconnecting: {type(exc).__name__}", flush=True)
            finally:
                for future in self.pending.values():
                    if not future.done():
                        future.cancel()
                self.pending.clear()
            await asyncio.sleep(6)


async def start(application, *, handler, bot, memory):
    url = os.getenv("NAPCAT_WS_URL", "").strip()
    token = os.getenv("NAPCAT_ONEBOT_TOKEN", "").strip()
    pair_code = os.getenv("QQ_PAIR_CODE", "").strip()
    self_uin = os.getenv("NAPCAT_SELF_UIN", "").strip()
    if not (url.startswith("ws://napcat.railway.internal:") and len(token) >= 32
            and len(pair_code) >= 16 and self_uin.isdigit() and bot.TELEGRAM_OWNER_ID):
        print("QQ OneBot inactive: missing private URL, token, pair code, self UIN or Telegram owner", flush=True)
        return
    connector = QQConnector(
        owner_chat_id=bot.TELEGRAM_OWNER_ID, pair_code=pair_code,
        expected_self_uin=self_uin, handler=handler, bot=bot, memory=memory,
    )
    task = asyncio.create_task(connector.run(url, token), name="qq-onebot-connector")
    application.bot_data["qq_onebot_task"] = task


async def stop(application):
    task = application.bot_data.get("qq_onebot_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
