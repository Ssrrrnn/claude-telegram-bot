"""Private OneBot WebSocket client for the Railway NapCat service."""

import asyncio
import base64
from io import BytesIO
import json
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

from qq_gateway import QQGateway
from qq_face_names import FACE_NAMES

OWNER_KEY = "qq_owner_uin_v1"
PLATFORM_KEY = "last_active_platform_v1"
MAX_MESSAGE_CHARS = 4000
MAX_IMAGE_BYTES = 4_000_000


def _qq_image_url(value):
    """Accept only HTTPS media from QQ controlled hosts (never arbitrary event URLs)."""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    allowed = any(host == suffix or host.endswith("." + suffix)
                  for suffix in ("qpic.cn", "qq.com", "qq.com.cn", "gtimg.cn"))
    if parsed.scheme not in ("http", "https") or not allowed or parsed.port not in (None, 80, 443) or parsed.username:
        return None
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


async def _fetch_qq_image(url):
    """Send a bounded, normalized first frame to the existing image capable model."""
    import httpx
    from PIL import Image, ImageOps, UnidentifiedImageError

    async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("QQ image redirect unavailable")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_IMAGE_BYTES:
                    raise ValueError("QQ image too large")
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.width * image.height > 20_000_000:
                raise ValueError("QQ image dimensions too large")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((1536, 1536))
            target = BytesIO()
            image.convert("RGB").save(target, format="JPEG", quality=85)
            return base64.b64encode(target.getvalue()).decode("ascii"), "image/jpeg"
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("QQ image format unavailable") from exc


def _message_parts(event):
    segments = event.get("message")
    if not isinstance(segments, list):
        return str(event.get("raw_message", "") or "").strip(), []
    parts, images = [], []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            continue
        kind, data = segment.get("type"), segment["data"]
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "face":
            face_id = str(data.get("id", ""))
            label = FACE_NAMES.get(face_id)
            parts.append(f"[QQ表情：{label}]" if label else "[QQ表情]")
        elif kind == "mface":
            summary = str(data.get("summary") or "").strip()[:80]
            parts.append(f"[QQ表情包：{summary}]" if summary else "[QQ表情包]")
        elif kind == "image":
            images.append(data)
            parts.append("[用户发送了一张图片或表情包]")
    return "".join(parts).strip(), images


def _message_text(event):
    return _message_parts(event)[0]


def _private_message(event, self_uin):
    if event.get("post_type") != "message" or event.get("message_type") != "private":
        return False
    sender = event.get("user_id")
    return str(sender).isdigit() and str(sender) != str(self_uin)


def last_chat_platform(memory, chat_id):
    """Recover the channel of the latest persisted human turn on first upgrade."""
    with memory._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT telegram_message_id FROM recent_conversation "
                "WHERE chat_id = %s AND kind = 'chat' ORDER BY id DESC LIMIT 1",
                (chat_id,),
            )
            row = cur.fetchone()
    message_id = row["telegram_message_id"] if row else None
    return "qq" if isinstance(message_id, int) and message_id < 0 else "telegram"


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
        self.ws = None

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

    async def _image_content(self, ws, data):
        url = _qq_image_url(data.get("url"))
        if url:
            try:
                return await _fetch_qq_image(url)
            except Exception:
                if not data.get("file"):
                    raise
        if data.get("file"):
            info = await self.action(ws, "get_image", {"file": data["file"]})
            if isinstance(info, dict):
                url = _qq_image_url(info.get("url"))
        if not url:
            raise ValueError("QQ image URL unavailable")
        return await _fetch_qq_image(url)

    async def send_proactive(self, text, bubbles, *, as_voice=False):
        ws = self.ws
        owner = self.owner_uin()
        if ws is None or not owner:
            raise RuntimeError("QQ proactive destination unavailable")
        if as_voice:
            from qq_gateway import _Bot
            import voice_output

            outbox = []
            if await voice_output.send_voice(
                _Bot(outbox), self.owner_chat_id, text,
                get_setting=self.memory.get_setting,
            ):
                await self._send_replies(ws, owner, outbox)
                return True
        for index, bubble in enumerate(bubbles):
            if index:
                await asyncio.sleep(0.32)
            await self._send_text(ws, owner, bubble)
        return False

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
        message_id = event.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return
        text, images = _message_parts(event)
        if not text or len(text) > MAX_MESSAGE_CHARS:
            return
        image_base64 = image_mime = None
        if images:
            try:
                image_base64, image_mime = await self._image_content(ws, images[0])
            except Exception as exc:
                print(f"QQ image unavailable: {type(exc).__name__}", flush=True)
                await self._send_text(ws, sender, "这张图我暂时没看清，再发一次给我好吗？")
                return
            if len(images) > 1:
                text += f"（这条消息还有{len(images) - 1}张图未能查看，只针对第一张回应。）"
        try:
            response = await self._gateway(owner).process({
                "sender_id": sender, "message_id": message_id, "text": text,
                "image_base64": image_base64, "image_mime": image_mime,
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
                    self.ws = ws
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
                self.ws = None
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
    if memory.get_setting(bot.TELEGRAM_OWNER_ID, OWNER_KEY) and not memory.get_setting(
        bot.TELEGRAM_OWNER_ID, PLATFORM_KEY,
    ):
        try:
            memory.set_setting(
                bot.TELEGRAM_OWNER_ID, PLATFORM_KEY,
                last_chat_platform(memory, bot.TELEGRAM_OWNER_ID),
            )
        except Exception as exc:
            print(f"QQ platform bootstrap skipped: {type(exc).__name__}", flush=True)
    connector = QQConnector(
        owner_chat_id=bot.TELEGRAM_OWNER_ID, pair_code=pair_code,
        expected_self_uin=self_uin, handler=handler, bot=bot, memory=memory,
    )
    task = asyncio.create_task(connector.run(url, token), name="qq-onebot-connector")
    application.bot_data["qq_connector"] = connector
    application.bot_data["qq_onebot_task"] = task


async def stop(application):
    task = application.bot_data.get("qq_onebot_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
