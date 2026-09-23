"""Optional Telegram voice replies using CosyVoice or Edge TTS."""
import asyncio
import io
import os
import re

MODE_KEY = "voice_reply_mode_v1"
PROFILE_KEY = "voice_profile_v1"
BACKEND_KEY = "voice_backend_v1"
DEFAULT_MODE = "auto"
COSYVOICE_API_KEY = os.getenv("COSYVOICE_API_KEY", "").strip()
COSYVOICE_API_URL = "https://api.siliconflow.cn/v1/audio/speech"
COSYVOICE_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
# Keep the original timbre. Shifting pitch down by 12 Hz made the former
# Yunxi default sound processed, particularly on short chat messages.
PROFILES = {
    "natural": ("zh-CN-YunfengNeural", "-2%", "+0Hz", "自然沉稳"),
    "deep": ("zh-CN-YunjianNeural", "-3%", "+0Hz", "低沉克制"),
    "soft": ("zh-CN-YunxiNeural", "-2%", "+0Hz", "温柔年轻"),
}
DEFAULT_PROFILE = "natural"
COSYVOICE_VOICES = {
    "natural": "charles",   # magnetic male voice
    "deep": "benjamin",    # deep male voice
    "soft": "alex",        # steady male voice
}
MAX_SPEECH_CHARS = 450
MAX_AUDIO_BYTES = 3_000_000


def mode(get_setting, chat_id):
    value = get_setting(chat_id, MODE_KEY)
    return value if value in ("on", "off", "auto") else DEFAULT_MODE


def profile(get_setting, chat_id):
    value = get_setting(chat_id, PROFILE_KEY)
    return value if value in PROFILES else DEFAULT_PROFILE


def backend(get_setting, chat_id):
    selected = get_setting(chat_id, BACKEND_KEY)
    if selected == "edge":
        return "edge"
    if COSYVOICE_API_KEY and selected in (None, "cosyvoice"):
        return "cosyvoice"
    return "edge"


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
    # A line break in a chat reply should sound like a short pause, not a
    # run-on sentence. Avoid inserting punctuation after an existing stop.
    value = re.sub(r"(?<![。！？!?，,；;：:])\s*\n+\s*", "。", value)
    return re.sub(r"\s+", " ", value).strip()


async def synthesize_cosyvoice(text, voice_profile=DEFAULT_PROFILE):
    import httpx

    if not COSYVOICE_API_KEY:
        raise ValueError("CosyVoice API key is missing")
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            COSYVOICE_API_URL,
            headers={"Authorization": f"Bearer {COSYVOICE_API_KEY}"},
            json={
                "model": COSYVOICE_MODEL,
                "input": text,
                "voice": f"{COSYVOICE_MODEL}:{COSYVOICE_VOICES[voice_profile]}",
                "response_format": "mp3",
                "speed": 1.0,
            },
        )
        response.raise_for_status()
        audio = response.content
        if not audio or len(audio) > MAX_AUDIO_BYTES or "audio" not in response.headers.get("content-type", ""):
            raise ValueError("Invalid CosyVoice audio response")
        return audio


async def synthesize(text, voice_profile=DEFAULT_PROFILE, provider="edge"):
    if provider == "cosyvoice":
        return await synthesize_cosyvoice(text, voice_profile)
    # Import at use time so a TTS outage cannot prevent startup or text chat.
    import edge_tts

    async def collect():
        audio = bytearray()
        voice, rate, pitch, _ = PROFILES[voice_profile]
        speaker = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
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


async def send_voice(bot, chat_id, text, *, reply_to_message_id=None,
                     get_setting=None, voice_profile=None, caption=None,
                     provider=None):
    speech = clean_speech(text)
    if not speech:
        return False
    try:
        from telegram import InputFile
        selected = voice_profile or (profile(get_setting, chat_id) if get_setting else DEFAULT_PROFILE)
        selected_backend = provider or (backend(get_setting, chat_id) if get_setting else "edge")
        audio = await synthesize(speech, selected, selected_backend)
        await bot.send_voice(
            chat_id=chat_id,
            voice=InputFile(io.BytesIO(audio), filename="reply.mp3"),
            caption=(caption or text)[:1024],
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
    if option == "provider":
        selected = args[1].lower() if len(args) > 1 else ""
        if selected == "cosyvoice" and not COSYVOICE_API_KEY:
            await update.message.reply_text("CosyVoice 还没有配置接口密钥。需要先在 Railway 设置 COSYVOICE_API_KEY。")
            return
        if selected in ("edge", "cosyvoice"):
            set_setting(chat_id, BACKEND_KEY, selected)
        active = backend(get_setting, chat_id)
        await update.message.reply_text(
            f"当前语音服务：{'CosyVoice' if active == 'cosyvoice' else 'Edge TTS'}。"
            "\n用 /voice provider cosyvoice 或 /voice provider edge 切换。"
        )
        return
    if option == "tone":
        chosen = args[1].lower() if len(args) > 1 else ""
        if chosen in PROFILES:
            set_setting(chat_id, PROFILE_KEY, chosen)
            await update.message.reply_text(
                f"音色已选：{PROFILES[chosen][3]}。发 /voice sample 听一下。"
            )
        else:
            selected = profile(get_setting, chat_id)
            await update.message.reply_text(
                f"当前音色：{PROFILES[selected][3]}。可选：natural 自然沉稳、deep 低沉克制、soft 温柔年轻。"
                "\n例如 /voice tone deep；发 /voice sample 试听。"
            )
        return
    if option == "sample":
        chosen = args[1].lower() if len(args) > 1 else profile(get_setting, chat_id)
        if chosen not in PROFILES:
            await update.message.reply_text("音色可选：natural、deep、soft。例如 /voice sample deep。")
            return
        sample = "刚刚听见你的声音了。别急，我在这儿，慢慢跟我说。"
        success = await send_voice(
            context.bot, chat_id, sample, get_setting=get_setting,
            voice_profile=chosen, caption=f"{PROFILES[chosen][3]}：{sample}",
        )
        if not success:
            await update.message.reply_text("这次试听没合成成功，稍后再试。文字聊天不受影响。")
        return
    if option in ("on", "off", "auto"):
        set_setting(chat_id, MODE_KEY, option)
    current = mode(get_setting, chat_id)
    await update.message.reply_text(
        {"off": "语音回复已关闭，继续用文字聊天。",
         "auto": "语音已设为自动：你发语音或明确要语音时，我回语音；其他时候回文字。",
         "on": "语音回复已开启：适合的短回复会发语音；长回复或合成失败时回文字。"}[current]
        + "\n用 /voice on、/voice auto、/voice off 切换。"
        + "\n/voice tone 查看音色，/voice sample 试听，/voice provider 查看服务。"
    )
