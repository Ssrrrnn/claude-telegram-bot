"""Adapt a QQ private message to the existing Telegram conversation pipeline."""

import asyncio
import base64
from types import SimpleNamespace


class _Reply:
    def __init__(self, outbox, index):
        self.outbox, self.index = outbox, index

    async def edit_text(self, text, **kwargs):
        self.outbox[self.index] = {"type": "text", "text": text}


class _Message:
    def __init__(self, text, message_id, outbox, from_voice=False, image_base64=None, image_mime=None):
        self.text, self.message_id, self.from_voice = text, message_id, from_voice
        self.image_base64, self.image_mime = image_base64, image_mime
        self.source_platform = "qq"
        self.outbox = outbox

    async def reply_text(self, text, **kwargs):
        self.outbox.append({"type": "text", "text": text})
        return _Reply(self.outbox, len(self.outbox) - 1)


class _Bot:
    def __init__(self, outbox):
        self.outbox = outbox

    async def send_chat_action(self, **kwargs):
        pass

    async def send_voice(self, *, voice, caption=None, **kwargs):
        content = voice.input_file_content
        audio = content.read() if hasattr(content, "read") else content
        if not isinstance(audio, bytes) or len(audio) > 3_000_000:
            raise ValueError("QQ voice data unavailable")
        self.outbox.append({
            "type": "record", "audio_base64": base64.b64encode(audio).decode("ascii"),
            "fallback_text": caption or "",
        })


class QQGateway:
    def __init__(self, *, owner_uin, shared_chat_id, handler, bot, memory):
        self.owner_uin = str(owner_uin)
        self.shared_chat_id = int(shared_chat_id)
        self.handler = handler
        self.bot = bot
        self.memory = memory
        self.request_lock = asyncio.Lock()

    def _existing_reply(self, message_id):
        # QQ ids are stored as negative values so they cannot overwrite Telegram turns.
        with self.memory._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT assistant_text FROM recent_conversation "
                    "WHERE chat_id = %s AND telegram_message_id = %s",
                    (self.shared_chat_id, -message_id),
                )
                row = cur.fetchone()
        return row["assistant_text"] if row else None

    async def process(self, data):
        if not isinstance(data, dict) or str(data.get("sender_id")) != self.owner_uin:
            raise ValueError("Unrecognized QQ sender")
        message_id = data.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or not 0 < message_id < 2**63:
            raise ValueError("Invalid QQ message id")
        text = data.get("text")
        audio = data.get("voice_base64")
        image = data.get("image_base64")
        image_mime = data.get("image_mime")
        if not isinstance(text, str) or len(text) > 4000 or (audio is not None and not isinstance(audio, str)):
            raise ValueError("Invalid QQ voice contents")
        if image is not None and (not isinstance(image, str) or len(image) > 5_500_000
                                  or image_mime not in ("image/jpeg", "image/png", "image/webp")):
            raise ValueError("Invalid QQ image contents")
        if audio is not None and len(audio) > 8_000_000:
            raise ValueError("QQ voice is too large")
        if not text.strip() and not audio and not image:
            raise ValueError("Empty QQ message")

        async with self.request_lock:
            cached = self._existing_reply(message_id)
            if cached:
                return {"replies": [{"type": "text", "text": cached}], "duplicate": True}
            if audio:
                try:
                    raw = base64.b64decode(audio, validate=True)
                    if len(raw) > 5_000_000:
                        raise ValueError("Voice file is too large")
                    text = await self.bot.transcribe_audio(raw, "qq_voice.mp3", "audio/mpeg")
                except Exception as exc:
                    print(f"QQ voice transcription skipped: {type(exc).__name__}", flush=True)
                    return {"replies": [{"type": "text", "text": "刚才那段语音没听清，打字告诉我，好吗？"}]}
            outbox = []
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=self.shared_chat_id),
                message=_Message(text.strip(), -message_id, outbox, from_voice=bool(audio),
                                 image_base64=image, image_mime=image_mime),
            )
            context = SimpleNamespace(bot=_Bot(outbox))
            await self.handler(update, context)
            return {"replies": outbox}
