"""Optional Telegram voice replies using Edge's online TTS service."""
import asyncio
import io
import os
import re

MODE_KEY = "voice_reply_mode_v1"
DEFAULT_MODE = "auto"
VOICE_NAME = os.getenv("TTS_VOICE", "zh-CN-YunxiNeural").strip()
VOICE_RATE = os.getenv("TTS_RATE", "-8%").strip()
VOICE_PITCH = os.getenv("TTS_PITCH", "-12Hz").strip()
MAX_SPEECH_CHARS = 450
MAX_AUDIO_BYTES = 3_000_000


def mode(get_setting, chat_id):
    value = get_setting(chat_id, MODE_KEY)
    return value if value in ("on", "off", "auto") else DEFAULT_MODE


def wants_audio(current_mode, *, from_voice=False, user_text="", proactive=False):
    if current_mode == "off":
        return False
    if current_mode == "on":
        return True
    if proactive:
        return False
    return from_voice or bool(re.search(r"(?:发|来|回|说).{0,4}语音|语音(?:回|说|回复)", user_text))


def clean_speech(text):
    value = text.strip()
    if (not value or len(value) > MAX_SPEECH_CHARS or "```" in value
            or re.search(r"(?:API 请求失败|错误：|内部协议|<favorite_message>|NO_REPLY)", value)):
        return ""
    value = re.sub(r"\*\*|__|(?<!\w)[*_](?!\w)", "", value)
    return re.sub(r"\s+", " ", value).strip()


async def synthesize(text):
    # Import at use time so a TTS outage cannot prevent startup or text chat.
    import edge_tts

    async def collect():
        audio = bytearray()
        speaker = edge_tts.Communicate(text, voice=VOICE_NAME, rate=VOICE_RATE, pitch=VOICE_PITCH)
        async for chunk in speaker.stream():
            if chunk.get("type") != "audio":
                continue
            audio.extend(chunk.get("data") or b"")
            if len(audio) > MAX_AUDIO_BYTES:
                raise ValueError("Synthesized voice is too large")
        if not audio:
            raise ValueError("No voice data returned")
        return bytes(audio)

    return await asyncio.wait_for(collect(), timeout=45)


async def send_voice(bot, chat_id, text, *, reply_to_message_id=None):
    speech = clean_speech(text)
    if not speech:
        return False
    try:
        from telegram import InputFile
        audio = await synthesize(speech)
        await bot.send_voice(
            chat_id=chat_id,
            voice=InputFile(io.BytesIO(audio), filename="reply.mp3"),
            caption=text[:1024],
            reply_to_message_id=reply_to_message_id,
        )
        return True
    except Exception as exc:
        # The caller falls back to a normal text reply with the same words.
        print(f"Voice delivery skipped: {type(exc).__name__}", flush=True)
        return False


async def voice_command(update, context, *, get_setting, set_setting):
    if not update.message:
        return
    from bot import is_authorized
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    args = getattr(context, "args", [])
    option = args[0].lower() if args else ""
    if option in ("on", "off", "auto"):
        set_setting(chat_id, MODE_KEY, option)
    current = mode(get_setting, chat_id)
    await update.message.reply_text(
        {"off": "语音回复已关闭，继续用文字聊天。",
         "auto": "语音已设为自动：你发语音或明确要语音时，我回语音；其他时候回文字。",
         "on": "语音回复已开启：适合的短回复会发语音；长回复或合成失败时回文字。"}[current]
        + "\n用 /voice on、/voice auto、/voice off 切换。"
    )
