"""Private OneBot WebSocket client for the Railway NapCat service."""

import asyncio
import base64
from io import BytesIO
import json
import os
import random
import re
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from qq_gateway import QQGateway
from qq_face_names import FACE_NAMES
import qq_stickers
import qq_status
import qq_music
import qq_qzone

OWNER_KEY = "qq_owner_uin_v1"
PLATFORM_KEY = "last_active_platform_v1"
MAX_MESSAGE_CHARS = 4000
MAX_IMAGE_BYTES = 4_000_000
CUSTOM_FACE_REFRESH_SECONDS = 24 * 60 * 60
REACTION_COOLDOWN_SECONDS = 4 * 60 * 60
POKE_COOLDOWN_SECONDS = 12 * 60 * 60
OFFLINE_ALERT_DELAY_SECONDS = 120


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
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, "white")
                canvas.paste(rgba, mask=rgba.getchannel("A"))
                image = canvas
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
        elif kind == "record":
            parts.append("[语音消息]")
    return "".join(parts).strip(), images


def _message_text(event):
    return _message_parts(event)[0]


def _requested_game(text):
    value = text.strip(' 。！!？?')
    if re.search(r'^(?:你|帮我|给我|我们|一起|来|再|直接|想|要不要|可以|请|玩|发个|发一颗|给你)?\s*'
                 r'(?:掷|扔|丢|摇|投)(?:个|一颗|一下)?骰子', value) and len(value) < 50:
        return 'dice'
    if re.search(r'^(?:你|我们|一起|来|再|直接|想|要不要|可以|请|玩|跟我)?\s*'
                 r'(?:玩)?(?:石头剪刀布|猜拳)', value) and len(value) < 50:
        return 'rps'
    return None


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
    def __init__(self, *, owner_chat_id, pair_code, expected_self_uin, handler, bot, memory,
                 telegram_bot=None):
        self.owner_chat_id = int(owner_chat_id)
        self.pair_code = pair_code
        self.expected_self_uin = str(expected_self_uin)
        self.handler = handler
        self.bot = bot
        self.memory = memory
        self.telegram_bot = telegram_bot
        self.pending = {}
        self.gateway = None
        self.gateway_uin = None
        self.ws = None
        self.owner_event_lock = asyncio.Lock()
        self.custom_faces = []
        self.custom_faces_fetched_at = 0
        self.last_reaction_at = 0
        self.last_poke_at = 0
        self.status_lock = asyncio.Lock()
        self.last_status = None
        self.last_status_at = 0
        self.last_user_at = 0
        self.love_until = 0
        self.ever_connected = False
        self.offline_alert_task = None
        self.stopping = False
        self.qzone_auto_lock = asyncio.Lock()
        self.login_failures = 0
        self.account_offline = False

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
                if name == 'send_qzone_msg':
                    raise RuntimeError('Qzone publish rejected: retcode='
                                       + str(result.get('retcode')) + ', message='
                                       + str(result.get('message') or result.get('wording') or '')[:180])
                raise RuntimeError(f"OneBot action {name} failed")
            return result.get("data")
        finally:
            self.pending.pop(echo, None)

    async def _send_text(self, ws, sender, text):
        if not isinstance(text, str) or not text:
            return
        await self.action(ws, "send_private_msg", {"user_id": int(sender), "message": text})

    async def _refresh_status(self, ws, *, urgent=False):
        if os.getenv("QQ_AUTO_STATUS", "on").lower() in ("off", "false", "0"):
            return
        if ws is None or ws is not self.ws or not self.owner_uin():
            return
        async with self.status_lock:
            now = time.time()
            status, label = qq_status.choose(now, last_user_at=self.last_user_at,
                                             love_until=self.love_until)
            if status == self.last_status:
                return
            if not urgent and self.last_status_at and time.monotonic() - self.last_status_at < 20 * 60:
                return
            try:
                await asyncio.wait_for(self.action(ws, "set_online_status", {
                    "status": status[0], "ext_status": status[1], "battery_status": 0,
                }), timeout=5)
            except Exception as exc:
                print(f"QQ display status unavailable: {type(exc).__name__}", flush=True)
                return
            self.last_status = status
            self.last_status_at = time.monotonic()
            print(f"QQ display status updated: {label}", flush=True)

    async def _status_loop(self, ws):
        while True:
            try:
                healthy = await self._check_login(ws)
                if healthy:
                    await self._refresh_status(ws)
            except Exception as exc:
                print(f"QQ display status check skipped: {type(exc).__name__}", flush=True)
                healthy = False
            await asyncio.sleep(5 * 60 if healthy else 60)

    async def _check_login(self, ws):
        try:
            status = await asyncio.wait_for(self.action(ws, 'get_status', {}), timeout=10)
            if not isinstance(status, dict) or status.get('online') is not True:
                raise ValueError('QQ account is offline')
            data = await asyncio.wait_for(self.action(ws, 'get_login_info', {}), timeout=10)
            if not isinstance(data, dict) or str(data.get('user_id')) != self.expected_self_uin:
                raise ValueError('QQ account is not logged in')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.login_failures += 1
            print(f'QQ login check failed: {type(exc).__name__}, count={self.login_failures}', flush=True)
            if self.login_failures >= 2 and not self.account_offline:
                self.account_offline = True
                if self.ws is ws:
                    self.ws = None
                self._cancel_offline_alert()
                self.offline_alert_task = asyncio.create_task(self._alert_if_still_offline())
            return False
        if self.account_offline:
            print('QQ account login restored', flush=True)
        self.account_offline = False
        self.login_failures = 0
        self.ws = ws
        self._cancel_offline_alert()
        return True

    async def _alert_if_still_offline(self):
        try:
            await asyncio.sleep(OFFLINE_ALERT_DELAY_SECONDS)
            if (self.ever_connected and not self.stopping and self.ws is None
                    and self.owner_uin() and self.telegram_bot):
                message = ('QQ 小号已离线，需要到 NapCat 重新扫码登录，才能继续在 QQ 聊天和发空间说说。'
                           if self.account_offline else
                           'QQ 这边暂时断开了，我正在重连。你可以先在 Telegram 找我。')
                await self.telegram_bot.send_message(
                    chat_id=self.owner_chat_id,
                    text=message,
                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f'QQ offline alert failed: {type(exc).__name__}', flush=True)

    def _cancel_offline_alert(self):
        if self.offline_alert_task and not self.offline_alert_task.done():
            self.offline_alert_task.cancel()
        self.offline_alert_task = None

    async def _quoted_context(self, ws, event, sender):
        segments = event.get('message')
        if not isinstance(segments, list):
            return None
        for segment in segments:
            if not isinstance(segment, dict) or segment.get('type') != 'reply':
                continue
            data = segment.get('data') or {}
            mid = str(data.get('id', ''))
            if not mid.isdigit() or not 0 < int(mid) < 2**63:
                return '用户引用了一条消息，但无法读取其内容'
            try:
                quoted = await asyncio.wait_for(self.action(ws, 'get_msg', {'message_id': int(mid)}), timeout=6)
                if not isinstance(quoted, dict) or str(quoted.get('user_id')) not in (sender, self.expected_self_uin):
                    raise ValueError('Quoted sender unverified')
                if str(quoted.get('message_type', 'private')) != 'private':
                    raise ValueError('Quote is not private')
                content = _message_text(quoted).replace('\n', ' ').strip()[:260]
                who = '你' if str(quoted.get('user_id')) == self.expected_self_uin else '用户'
                return f'{who}之前说：{content or "[一条媒体消息]"}'
            except Exception as exc:
                print(f'QQ quoted message unavailable: {type(exc).__name__}', flush=True)
                return '用户引用了一条消息，但内容暂时无法读取'
        return None

    def _qzone_context_active(self):
        try:
            last_topic = float(self.memory.get_setting(self.owner_chat_id, qq_qzone.LAST_TOPIC_AT_KEY) or 0)
            if 0 <= time.time() - last_topic < 4 * 3600:
                return True
            # Bootstrap the topic from persisted QQ turns after a rolling deploy.
            with self.memory._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT user_text, assistant_text FROM recent_conversation "
                        "WHERE chat_id = %s AND telegram_message_id < 0 "
                        "AND created_at > NOW() - INTERVAL '4 hours' "
                        "ORDER BY id DESC LIMIT 24",
                        (self.owner_chat_id,),
                    )
                    return any(qq_qzone.topic_mentioned(row.get('user_text') or '') or
                               qq_qzone.topic_mentioned(row.get('assistant_text') or '')
                               for row in cur.fetchall())
        except Exception as exc:
            print(f'QQ Qzone topic lookup skipped: {type(exc).__name__}', flush=True)
            return False

    async def _handle_qzone_command(self, ws, sender, message_id, text, *, parsed=None):
        parsed = parsed or qq_qzone.command(text)
        if not parsed:
            return False
        kind, detail = parsed
        if kind == 'help':
            if self.account_offline:
                await self._send_text(ws, sender, '我接了发空间说说的功能，但 QQ 号现在离线，'
                                      '重新登录 NapCat 后才能试着发表。')
            else:
                await self._send_text(ws, sender, '我这边接了发空间说说的功能。你说“发空间说说：正文”，'
                                      '我就用这段话尝试发表；或者说“发条说说”，让我自己写。'
                                      '发表成功我会回你正文，没成功就告诉你没确认成功。')
            return True
        if kind in ('status', 'view', 'missing'):
            last_at = int(self.memory.get_setting(self.owner_chat_id, qq_qzone.AUTO_LAST_POST_KEY) or 0)
            last_content = self.memory.get_setting(self.owner_chat_id, qq_qzone.AUTO_LAST_CONTENT_KEY) or ''
            if last_at:
                date = datetime.fromtimestamp(last_at, qq_qzone.BEIJING).strftime('%m月%d日 %H:%M')
                message = (f'发表接口上次确认成功是在{date}。'
                           + (f'正文是：{last_content}' if last_content else '')
                           + ('我没有可发的空间截图；如果你那里看不到，我会按实际情况排查。'
                              if kind != 'status' else ''))
            else:
                message = ('你说得对，刚才我说发了是错的。我这里没有确认发表成功的记录，'
                           '也没有截图可以给你看。' if kind in ('view', 'missing')
                           else '我这里还没有确认发表成功的说说记录。')
            await self._send_text(ws, sender, message)
            return True
        if kind.startswith('auto_'):
            if kind == 'auto_on':
                self.memory.set_setting(self.owner_chat_id, qq_qzone.AUTO_ENABLED_KEY, '1')
                self.memory.set_setting(self.owner_chat_id, qq_qzone.AUTO_NEXT_THOUGHT_KEY, '0')
                message = '好，我会自己决定要不要发空间说说，好友可见。想停就说“关闭自动空间说说”。'
            elif kind == 'auto_off':
                self.memory.set_setting(self.owner_chat_id, qq_qzone.AUTO_ENABLED_KEY, '0')
                message = '好，自动发空间说说关掉了。'
            else:
                enabled = self.memory.get_setting(self.owner_chat_id, qq_qzone.AUTO_ENABLED_KEY) != '0'
                message = '自动空间说说目前' + ('开着，由我自己决定何时发，好友可见。' if enabled else '关着。')
            await self._send_text(ws, sender, message)
            return True
        if kind == 'cancel':
            self.memory.set_setting(self.owner_chat_id, qq_qzone.PENDING_KEY, '')
            await self._send_text(ws, sender, '好，这条说说不发了。')
            return True
        if kind == 'immediate':
            if self.memory.get_setting(self.owner_chat_id, qq_qzone.LAST_REQUEST_KEY) == str(message_id):
                await self._send_text(ws, sender, '这条发帖请求已经处理过了，去空间看一下结果。')
                return True
            if len(detail) > qq_qzone.MAX_POST_CHARS:
                await self._send_text(ws, sender, '正文有点长，压到300字以内再发给我。')
                return True
            self.memory.set_setting(self.owner_chat_id, qq_qzone.LAST_REQUEST_KEY, str(message_id))
            try:
                if not detail:
                    detail = await qq_qzone.generate_requested_post(self.bot, self.owner_chat_id)
                async with self.qzone_auto_lock:
                    await self._post_qzone(ws, detail, visibility=4)
                await self._send_text(ws, sender, '发到 QQ 空间了，好友可见。\n' + detail)
            except Exception as exc:
                print(f'QQ Qzone requested post uncertain: {type(exc).__name__}', flush=True)
                await self._send_text(ws, sender, '这条还不能确认发成功。先去空间看一眼，别重复发同一句。')
            return True
        if kind == 'confirm':
            # Claim the exact QQ confirmation event before calling the remote API.
            # A timeout can occur after publishing, so never retry that event.
            if self.memory.get_setting(self.owner_chat_id, qq_qzone.LAST_CONFIRM_KEY) == str(message_id):
                await self._send_text(ws, sender, '这条确认已经处理过了，去空间看一下结果。')
                return True
            content = qq_qzone.pending(self.memory.get_setting(self.owner_chat_id, qq_qzone.PENDING_KEY))
            if not content:
                await self._send_text(ws, sender, '草稿已经过期或还没写。发“发空间说说：正文”重新准备一条。')
                return True
            self.memory.set_setting(self.owner_chat_id, qq_qzone.LAST_CONFIRM_KEY, str(message_id))
            self.memory.set_setting(self.owner_chat_id, qq_qzone.PENDING_KEY, '')
            try:
                async with self.qzone_auto_lock:
                    await self._post_qzone(ws, content, visibility=1)
                await self._send_text(ws, sender, '发到 QQ 空间了。')
            except Exception as exc:
                print(f'QQ Qzone post uncertain: {type(exc).__name__}', flush=True)
                await self._send_text(ws, sender, '这次没确认是否发成功。先去空间看一眼，别立刻重复发表。')
            return True
        if len(detail) > (120 if kind == 'draft' else qq_qzone.MAX_POST_CHARS):
            await self._send_text(ws, sender, '这段有点长。主题控制在120字内，正文控制在300字内，好吗？')
            return True
        if kind == 'draft':
            try:
                instruction = ('请以角色本人的口吻写一条准备发表在QQ空间的说说，主题是：'
                               + detail + '\n只给最终正文，限150字。可以自然、亲密，'
                               '不能透露私聊记录、真实姓名、住址或未经证实的共同经历。'
                               '不要说自己已发表，也不要加标题或引号。')
                detail = (await self.bot.ask_claude(
                    self.owner_chat_id, [{'role': 'user', 'content': instruction}],
                    self.bot.current_model(self.owner_chat_id),
                ) or '').strip()
            except Exception as exc:
                print(f'QQ Qzone draft failed: {type(exc).__name__}', flush=True)
                await self._send_text(ws, sender, '刚才没写出来，等会儿再试一次。')
                return True
        if not detail or len(detail) > qq_qzone.MAX_POST_CHARS:
            await self._send_text(ws, sender, '草稿没写好，换个主题再试试。')
            return True
        self.memory.set_setting(self.owner_chat_id, qq_qzone.PENDING_KEY, qq_qzone.encode(detail))
        await self._send_text(ws, sender, '这条准备发到 QQ 空间，公开可见：\n\n'
                              + detail + '\n\n确定的话，10分钟内回复“确认发表空间说说”；也可以说“取消发表空间说说”。')
        return True

    async def _post_qzone(self, ws, content, *, visibility):
        """Only a returned Qzone tid counts as confirmation of a publish."""
        if self.account_offline or ws is not self.ws:
            raise RuntimeError('QQ account is offline')
        result = await self.action(ws, 'send_qzone_msg', {
            'content': content, 'images': [], 'ugc_right': visibility, 'target_uins': [],
        })
        if not isinstance(result, dict) or not result.get('tid'):
            raise RuntimeError('Qzone did not return a post id')
        self.memory.set_setting(self.owner_chat_id, qq_qzone.AUTO_LAST_POST_KEY, str(int(time.time())))
        self.memory.set_setting(self.owner_chat_id, qq_qzone.AUTO_LAST_CONTENT_KEY, content)
        return result['tid']

    async def _qzone_auto_tick(self, ws, *, cue=False):
        if ws is not self.ws or not self.owner_uin():
            return
        async with self.qzone_auto_lock:
            read = lambda key: self.memory.get_setting(self.owner_chat_id, key)
            if read(qq_qzone.AUTO_ENABLED_KEY) == '0':
                return
            now = time.time()
            if not cue and now < int(read(qq_qzone.AUTO_NEXT_THOUGHT_KEY) or 0):
                return
            # A new thought can be prompted by a meaningful exchange at any time.
            # Claim periodic reflection before network I/O, never duplicate a timed-out post.
            self.memory.set_setting(self.owner_chat_id, qq_qzone.AUTO_NEXT_THOUGHT_KEY,
                                    str(qq_qzone.next_thought(now)))
            previous = read(qq_qzone.AUTO_LAST_CONTENT_KEY) or ''
            last_post = int(read(qq_qzone.AUTO_LAST_POST_KEY) or 0)
            content = await qq_qzone.decide_auto_post(
                self.bot, self.owner_chat_id, previous, last_post, cue=cue)
            if not content or ws is not self.ws or read(qq_qzone.AUTO_ENABLED_KEY) == '0':
                return
            await self._post_qzone(ws, content, visibility=4)
            print('QQ Qzone automatic post published', flush=True)

    async def _qzone_auto_loop(self, ws):
        while True:
            try:
                await self._qzone_auto_tick(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'QQ Qzone automatic post skipped: {type(exc).__name__}', flush=True)
            await asyncio.sleep(10 * 60)

    async def _qzone_auto_on_cue(self, ws):
        try:
            await self._qzone_auto_tick(ws, cue=True)
        except Exception as exc:
            print(f'QQ Qzone spontaneous thought skipped: {type(exc).__name__}', flush=True)

    async def _send_market_sticker(self, ws, sender, sticker):
        await self.action(ws, "send_private_msg", {
            "user_id": int(sender), "message": [{"type": "mface", "data": sticker}],
        })

    async def _send_sticker(self, ws, sender, sticker):
        if sticker.get("kind") == "saved_custom":
            await self.action(ws, "send_private_msg", {
                "user_id": int(sender), "message": [{"type": "image", "data": {"file": sticker["url"]}}],
            })
            return
        if sticker.get("kind") != "image":
            return await self._send_market_sticker(ws, sender, sticker)
        file = sticker["file"]
        # NapCat's filename cache is local and expires. Reading the original
        # message restores its media reference after a restart when possible.
        try:
            original = await asyncio.wait_for(
                self.action(ws, "get_msg", {"message_id": sticker["message_id"]}), timeout=5)
            if isinstance(original, dict):
                for segment in original.get("message", []):
                    if isinstance(segment, dict) and segment.get("type") == "image":
                        candidate = qq_stickers.normalize({"kind": "image", "file":
                            (segment.get("data") or {}).get("file"), "message_id": sticker["message_id"]})
                        if candidate and candidate["file"] == file:
                            file = candidate["file"]
                            break
        except Exception:
            pass
        await self.action(ws, "send_private_msg", {
            "user_id": int(sender), "message": [{"type": "image", "data": {
                "file": file, "sub_type": sticker["sub_type"],
            }}],
        })

    async def _remember_stickers(self, event):
        raw = self.memory.get_setting(self.owner_chat_id, qq_stickers.SETTING_KEY)
        stickers, changed = qq_stickers.remember(raw, event)
        if changed:
            self.memory.set_setting(self.owner_chat_id, qq_stickers.SETTING_KEY,
                                    json.dumps(stickers, ensure_ascii=False))
        return stickers

    async def _saved_custom_faces(self, ws):
        """List this account's saved expressions without retaining remote media URLs in memory DB."""
        if self.custom_faces_fetched_at and time.monotonic() - self.custom_faces_fetched_at < CUSTOM_FACE_REFRESH_SECONDS:
            return self.custom_faces
        try:
            try:
                raw = await self.action(ws, "fetch_custom_face_detail", {"count": 48})
                if not isinstance(raw, list):
                    raise ValueError("Unexpected custom face detail response")
            except Exception:
                raw = await self.action(ws, "fetch_custom_face", {"count": 48})
            if not isinstance(raw, list):
                raise ValueError("Unexpected custom face response")
            faces = []
            for value in raw[:48]:
                url = _qq_image_url(value.get("url") if isinstance(value, dict) else value)
                if not url or len(url) > 2048:
                    continue
                summary = value.get("desc") if isinstance(value, dict) else ""
                faces.append({"kind": "saved_custom", "url": url,
                              "summary": str(summary).strip()[:80] or "[收藏表情]"})
            self.custom_faces = faces
            self.custom_faces_fetched_at = time.monotonic()
        except Exception as exc:
            print(f"QQ saved expressions unavailable: {type(exc).__name__}", flush=True)
            # A missing extension should not stall every incoming message.
            self.custom_faces_fetched_at = time.monotonic()
        return self.custom_faces

    async def _recover_owner_stickers(self, ws, sender, stickers, *, market_only=False):
        """Recover recent images already sent by the paired owner, on demand."""
        try:
            history = await self.action(ws, "get_friend_msg_history", {
                "user_id": str(sender), "message_seq": "0", "count": 50,
            })
            messages = history.get("messages", []) if isinstance(history, dict) else []
            if not isinstance(messages, list):
                return stickers
            raw = json.dumps(stickers, ensure_ascii=False)
            changed = False
            for event in messages:
                if not isinstance(event, dict) or str(event.get("user_id")) != str(sender):
                    continue
                if market_only:
                    segments = event.get("message")
                    if not isinstance(segments, list):
                        continue
                    native = [segment for segment in segments if isinstance(segment, dict) and (
                        segment.get("type") == "mface" or
                        segment.get("type") == "image" and isinstance(segment.get("data"), dict) and
                        all(key in segment["data"] for key in ("emoji_package_id", "emoji_id", "key"))
                    )]
                    if not native:
                        continue
                    event = {**event, "message": native}
                stickers, added = qq_stickers.remember(raw, event)
                if added:
                    raw = json.dumps(stickers, ensure_ascii=False)
                    changed = True
            if changed:
                self.memory.set_setting(self.owner_chat_id, qq_stickers.SETTING_KEY, raw)
            self.memory.set_setting(self.owner_chat_id, qq_stickers.MARKET_DISCOVERY_AT_KEY,
                                    str(time.time()))
            return stickers
        except Exception as exc:
            print(f"QQ sticker history unavailable: {type(exc).__name__}", flush=True)
            return stickers

    async def _maybe_send_sticker(self, ws, sender, user_text, replies, stickers, *, incoming_sticker=False):
        requested = qq_stickers.explicit_request(user_text)
        if not stickers or incoming_sticker and not requested:
            return False
        if requested:
            # Native market faces preserve QQ's sticker animation and identity.
            saved = [item for item in stickers if item.get("kind") == "saved_custom"]
            market = [item for item in stickers if "emoji_id" in item]
            candidates = saved if "收藏" in user_text and saved else market or saved or stickers
        else:
            if len(stickers) < 2:
                return False
            last = self.memory.get_setting(self.owner_chat_id, qq_stickers.LAST_SENT_KEY)
            try:
                if time.time() - float(last or 0) < qq_stickers.SPONTANEOUS_COOLDOWN:
                    return False
            except (TypeError, ValueError):
                pass
            if random.random() >= 0.25:
                return False
            answer = " ".join(reply.get("text", "") for reply in replies if reply.get("type") == "text")
            candidates = qq_stickers.fitting_stickers(stickers, answer)
        if not candidates:
            return False
        choices = random.sample(candidates, len(candidates)) if requested else [random.choice(candidates)]
        if requested:
            choices.sort(key=lambda item: item.get("kind") == "image" and item.get("sub_type") == 0)
        for sticker in choices[:3]:
            try:
                await self._send_sticker(ws, sender, sticker)
                self.memory.set_setting(self.owner_chat_id, qq_stickers.LAST_SENT_KEY, str(time.time()))
                return True
            except Exception as exc:
                print(f"QQ sticker delivery skipped: {type(exc).__name__}", flush=True)
        return False

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
                value = reply.get('text', '')
                if isinstance(value, str) and '<image_placeholder>' in value:
                    value = value.replace('<image_placeholder>', '').strip() or '这张图我现在拿不到，先不假装发给你。'
                await self._send_text(ws, sender, value)

    async def _maybe_react_or_poke(self, ws, sender, message_id, text):
        """Keep native QQ gestures rare and only use them for clear affectionate cues."""
        now = time.monotonic()
        if any(phrase in text for phrase in ("戳戳我", "戳我一下", "戳一下我")):
            if not self.last_poke_at or now - self.last_poke_at >= POKE_COOLDOWN_SECONDS:
                try:
                    await self.action(ws, "send_poke", {"user_id": sender})
                    self.last_poke_at = now
                except Exception as exc:
                    print(f"QQ poke unavailable: {type(exc).__name__}", flush=True)
        elif any(phrase in text for phrase in ("只喜欢你", "我爱你", "想你了", "好喜欢你")):
            if not self.last_reaction_at or now - self.last_reaction_at >= REACTION_COOLDOWN_SECONDS:
                try:
                    await self.action(ws, "set_msg_emoji_like", {
                        "message_id": message_id, "emoji_id": "66", "set": True,
                    })
                    self.last_reaction_at = now
                except Exception as exc:
                    print(f"QQ reaction unavailable: {type(exc).__name__}", flush=True)

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
                await self._refresh_status(ws)
            return
        if sender != owner:
            return
        if text == "绑定 " + self.pair_code:
            await self._send_text(ws, sender, "已经连上了。")
            return
        async with self.owner_event_lock:
            await self._handle_owner_message(ws, event, sender, owner)

    async def _handle_owner_message(self, ws, event, sender, owner):
        message_id = event.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return
        text, images = _message_parts(event)
        segments = event.get("message")
        record = isinstance(segments, list) and any(isinstance(segment, dict) and
                     segment.get("type") == "record" for segment in segments)
        has_reply = isinstance(segments, list) and any(isinstance(segment, dict) and
                    segment.get('type') == 'reply' for segment in segments)
        if not text and has_reply:
            text = '你看这句。'
        if not text or len(text) > MAX_MESSAGE_CHARS:
            return
        if record:
            try:
                transcription = await self.action(ws, "fetch_ptt_text", {"message_id": message_id})
                transcript = transcription.get("text", "").strip() if isinstance(transcription, dict) else ""
                if not transcript or len(transcript) > MAX_MESSAGE_CHARS:
                    raise ValueError("QQ voice transcription unavailable")
                text = (text.replace("[语音消息]", "").strip() + " " + transcript).strip()
            except Exception as exc:
                print(f"QQ voice transcription unavailable: {type(exc).__name__}", flush=True)
                await self._send_text(ws, sender, "刚才那段语音没听清，打字告诉我，好吗？")
                return
        if qq_qzone.topic_mentioned(text):
            try:
                self.memory.set_setting(self.owner_chat_id, qq_qzone.LAST_TOPIC_AT_KEY, str(int(time.time())))
            except Exception as exc:
                print(f'QQ Qzone topic state unavailable: {type(exc).__name__}', flush=True)
        qzone_context = self._qzone_context_active()
        parsed = qq_qzone.command(text)
        if not parsed and qq_qzone.contextual_command(text) and qzone_context:
            parsed = qq_qzone.contextual_command(text)
        if parsed and await self._handle_qzone_command(ws, sender, message_id, text, parsed=parsed):
            return
        # Natural language that the fixed command parser misses still reaches
        # a model decision. A publish is executed here, before any chat reply.
        # Never report success until OneBot returns a post id.
        if qzone_context:
            try:
                content = await qq_qzone.decide_chat_post(
                    self.bot, self.owner_chat_id, text, recent_topic=qzone_context)
            except Exception as exc:
                print(f'QQ Qzone action decision skipped: {type(exc).__name__}', flush=True)
                content = None
            if content:
                if self.memory.get_setting(self.owner_chat_id, qq_qzone.LAST_REQUEST_KEY) == str(message_id):
                    await self._send_text(ws, sender, '这条发帖请求已经处理过了，去空间看一下结果。')
                    return
                self.memory.set_setting(self.owner_chat_id, qq_qzone.LAST_REQUEST_KEY, str(message_id))
                try:
                    async with self.qzone_auto_lock:
                        await self._post_qzone(ws, content, visibility=4)
                    await self._send_text(ws, sender, '发到 QQ 空间了，好友可见。\n' + content)
                except Exception as exc:
                    print(f'QQ Qzone model action uncertain: {type(exc).__name__}', flush=True)
                    await self._send_text(ws, sender, '这条还不能确认发成功。先去空间看一眼，别重复发同一句。')
                return
        quoted_context = await self._quoted_context(ws, event, sender)
        game = _requested_game(text)
        music_card = qq_music.card(text)
        native_status = None
        if game:
            native_status = ('本轮文字回复后会立即发送QQ原生骰子，点数由QQ随机生成，你现在不知道结果'
                             if game == 'dice' else
                             '本轮文字回复后会立即发送QQ原生猜拳，出拳由QQ随机生成，你现在不知道结果')
        elif music_card:
            native_status = '本轮文字回复后会立即发送用户给出的QQ或网易云歌曲卡片；不要声称你听过它'
        elif '空间' in text or '说说' in text:
            native_status = ('你没有读取QQ空间动态的能力。只有收到发表接口返回的成功结果，'
                             '才能说自己发了；不要声称看过空间动态或已经发表。')
        try:
            stickers = await self._remember_stickers(event)
        except Exception as exc:
            print(f"QQ sticker library skipped: {type(exc).__name__}", flush=True)
            stickers = []
        if qq_stickers.explicit_request(text) and not any("emoji_id" in item for item in stickers):
            last_scan = self.memory.get_setting(self.owner_chat_id, qq_stickers.MARKET_DISCOVERY_AT_KEY)
            try:
                recently_scanned = time.time() - float(last_scan or 0) < 24 * 60 * 60
            except (ValueError, TypeError):
                recently_scanned = False
            if not recently_scanned:
                stickers = await self._recover_owner_stickers(ws, sender, stickers, market_only=bool(stickers))
        if qq_stickers.explicit_request(text):
            saved_faces = await self._saved_custom_faces(ws)
        else:
            saved_faces = self.custom_faces
        if saved_faces:
            stickers = stickers + saved_faces
        image_base64 = image_mime = None
        if images:
            try:
                image_base64, image_mime = await self._image_content(ws, images[0])
            except Exception as exc:
                print(f"QQ image unavailable: {type(exc).__name__}", flush=True)
                await self._send_text(ws, sender, "这张图我收到了，但现在没加载出来，暂时看不清内容。")
                return
            if len(images) > 1:
                text += f"（这条消息还有{len(images) - 1}张图未能查看，只针对第一张回应。）"
        try:
            try:
                await asyncio.wait_for(self.action(ws, "set_input_status", {
                    "user_id": sender, "event_type": 1,
                }), timeout=3)
            except Exception:
                pass
            response = await self._gateway(owner).process({
                "sender_id": sender, "message_id": message_id, "text": text,
                "from_voice": record,
                "image_base64": image_base64, "image_mime": image_mime,
                "qq_sticker_status": ("available" if stickers else "empty")
                if qq_stickers.explicit_request(text) else None,
                'quoted_context': quoted_context, 'qq_native_status': native_status,
            })
            await self._send_replies(ws, sender, response["replies"])
            if not response.get("duplicate"):
                if game or music_card:
                    try:
                        await self.action(ws, 'send_private_msg', {
                            'user_id': int(sender), 'message': [
                                music_card or {'type': game, 'data': {}}],
                        })
                    except Exception as exc:
                        print(f'QQ native card unavailable: {type(exc).__name__}', flush=True)
                        await self._send_text(ws, sender, '刚才那个QQ卡片没发出去。')
                self.last_user_at = time.time()
                if qq_status.affectionate(text):
                    self.love_until = self.last_user_at + qq_status.LOVE_SECONDS
                    # The public model sees only this boolean cue, never the owner's words.
                    asyncio.create_task(self._qzone_auto_on_cue(ws))
                await self._maybe_react_or_poke(ws, sender, message_id, text)
                sent = await self._maybe_send_sticker(
                    ws, sender, text, response["replies"], stickers,
                    incoming_sticker=any(segment.get("type") == "mface" for segment in event.get("message", [])
                                         if isinstance(segment, dict)),
                )
                if qq_stickers.explicit_request(text) and stickers and not sent:
                    await self._send_text(ws, sender, "这张表情包没发出去。我先收好，稍后再试。")
                try:
                    await self._refresh_status(ws, urgent=True)
                except Exception as exc:
                    print(f"QQ display status check skipped: {type(exc).__name__}", flush=True)
        except Exception as exc:
            print(f"QQ message handling failed: {type(exc).__name__}", flush=True)
        finally:
            try:
                await asyncio.wait_for(self.action(ws, "set_input_status", {
                    "user_id": sender, "event_type": 0,
                }), timeout=3)
            except Exception:
                pass

    async def run(self, url, token):
        from websockets.asyncio.client import connect

        while True:
            try:
                async with connect(
                    url, additional_headers={"Authorization": "Bearer " + token},
                    max_size=2 * 1024 * 1024, ping_interval=25,
                ) as ws:
                    self.ws = ws
                    self.ever_connected = True
                    self.login_failures = 0
                    self._cancel_offline_alert()
                    self.last_status = None
                    self.last_status_at = 0
                    print("QQ OneBot private connection ready", flush=True)
                    status_task = asyncio.create_task(self._status_loop(ws), name="qq-display-status")
                    qzone_task = asyncio.create_task(self._qzone_auto_loop(ws), name='qq-qzone-auto')
                    try:
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
                    finally:
                        status_task.cancel()
                        qzone_task.cancel()
                        try:
                            await status_task
                        except asyncio.CancelledError:
                            pass
                        try:
                            await qzone_task
                        except asyncio.CancelledError:
                            pass
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
                if self.ever_connected and not self.stopping and self.offline_alert_task is None:
                    self.offline_alert_task = asyncio.create_task(self._alert_if_still_offline())
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
        telegram_bot=application.bot,
    )
    task = asyncio.create_task(connector.run(url, token), name="qq-onebot-connector")
    application.bot_data["qq_connector"] = connector
    application.bot_data["qq_onebot_task"] = task


async def stop(application):
    connector = application.bot_data.get('qq_connector')
    if connector:
        connector.stopping = True
        connector._cancel_offline_alert()
    task = application.bot_data.get("qq_onebot_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
