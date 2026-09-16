import asyncio
import io
import os
import re

import httpx

import runner_strict_bubbles as app

# Keep the faster proactive cadence without changing the rest of the roleplay,
# memory, time-awareness, and strict-bubble behavior.
app.PROACTIVE_MIN_SECONDS = 20 * 60
app.PROACTIVE_MAX_SECONDS = 60 * 60

# Optional ElevenLabs voice notes for proactive messages. If the credentials
# are absent or TTS fails, the bot always falls back to the existing text bubbles.
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
ELEVENLABS_MODEL_ID = os.environ.get(
    "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"
).strip()
try:
    PROACTIVE_VOICE_CHANCE = max(
        0.0,
        min(1.0, float(os.environ.get("PROACTIVE_VOICE_CHANCE", "0.25"))),
    )
except ValueError:
    PROACTIVE_VOICE_CHANCE = 0.25

VOICE_MAX_CHARS = 600
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
_original_send_bubbles_to_chat = app._send_bubbles_to_chat
_original_user_safe_reply = app.runner.bot.safe_reply


def _voice_ready() -> bool:
    return bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)


def _clean_tts_text(text: str) -> str:
    clean = re.sub(r"```.*?```", "", text, flags=re.S)
    clean = re.sub(r"`([^`]*)`", r"\1", clean)
    clean = re.sub(r"[*_~#>]", "", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _explicit_voice_request(text: str) -> bool:
    """Return True only when the user is clearly asking for a voice reply."""
    compact = re.sub(r"\s+", "", text or "").lower()
    if not compact:
        return False

    negative_patterns = (
        r"(?:不要|别|不用|不需要|先别|别再|不是要|不是让你).{0,8}(?:语音|voice|声音)",
        r"(?:语音|voice|声音).{0,8}(?:不要|别|不用|不需要)",
    )
    if any(re.search(pattern, compact, flags=re.I) for pattern in negative_patterns):
        return False

    positive_patterns = (
        r"(?:给我|跟我|对我)?.{0,3}(?:发|来)(?:个|一条|条|一下)?语音",
        r"(?:用|发)(?:一下|个|一条|条)?语音(?:回|回复|说|讲)?",
        r"语音(?:回|回复|说|讲|发)(?:我|给我)?",
        r"(?:用语音|语音)(?:跟我|给我|对我)?说",
        r"(?:说|念|读)(?:一下)?给我听",
        r"别打字.{0,8}(?:发语音|说|回|回复)",
        r"不要文字.{0,8}(?:发语音|说|回|回复)",
        r"(?:可以|能不能|能否).{0,8}(?:用语音|发语音|语音回).{0,4}(?:吗|嘛|么)?",
        r"(?:voice).{0,8}(?:reply|message|please|back)",
        r"(?:reply|message|send).{0,8}(?:voice)",
    )
    return any(re.search(pattern, compact, flags=re.I) for pattern in positive_patterns)


def _split_tts_chunks(text: str, max_chars: int = VOICE_MAX_CHARS) -> list[str]:
    spoken = _clean_tts_text(text)
    if not spoken:
        return []
    if len(spoken) <= max_chars:
        return [spoken]

    parts = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;…])\s*", spoken)
        if part and part.strip()
    ]
    chunks: list[str] = []
    current = ""
    for part in parts:
        if len(part) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(
                part[i : i + max_chars]
                for i in range(0, len(part), max_chars)
            )
            continue
        if current and len(current) + len(part) > max_chars:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)
    return chunks


async def _synthesize_elevenlabs(text: str) -> bytes:
    spoken = _clean_tts_text(text)
    if not spoken:
        raise RuntimeError("empty TTS text")
    if len(spoken) > VOICE_MAX_CHARS:
        raise RuntimeError(f"TTS text too long: {len(spoken)} chars")

    timeout = httpx.Timeout(45.0)
    url = f"{ELEVENLABS_TTS_URL}/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": spoken,
        "model_id": ELEVENLABS_MODEL_ID,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            params={"output_format": "mp3_44100_128"},
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"ElevenLabs HTTP {response.status_code}: {detail}")
    if not response.content:
        raise RuntimeError("ElevenLabs returned empty audio")
    return response.content


async def _reply_as_forced_voice(update, text: str) -> None:
    chunks = _split_tts_chunks(text)
    if not chunks:
        raise RuntimeError("empty forced voice reply")

    for index, chunk in enumerate(chunks):
        if index:
            await asyncio.sleep(0.25)
        audio = await _synthesize_elevenlabs(chunk)
        voice_file = io.BytesIO(audio)
        voice_file.name = f"voice_reply_{index + 1}.mp3"
        await update.message.reply_voice(voice=voice_file)

    print(
        f"Forced voice reply sent: chat_id={update.effective_chat.id}, chunks={len(chunks)}, chars={len(_clean_tts_text(text))}",
        flush=True,
    )


async def _safe_reply_with_explicit_voice(update, text: str) -> None:
    user_text = update.message.text if update.message else ""
    wants_voice = _explicit_voice_request(user_text)

    if not wants_voice:
        await _original_user_safe_reply(update, text)
        return

    if not _voice_ready():
        print(
            f"Forced voice requested but voice is not configured: chat_id={update.effective_chat.id}",
            flush=True,
        )
        await _original_user_safe_reply(update, text)
        return

    try:
        await _reply_as_forced_voice(update, text)
    except Exception as exc:
        print(f"Forced TTS fallback to text: {exc!r}", flush=True)
        await _original_user_safe_reply(update, text)


# generate_and_send resolves bot.safe_reply at runtime, so this adds natural-language
# forced voice requests without changing Claude generation, history, or memory logic.
app.runner.bot.safe_reply = _safe_reply_with_explicit_voice


async def _send_proactive_text_or_voice(application, chat_id: int, text: str) -> None:
    should_voice = _voice_ready() and app.random.random() < PROACTIVE_VOICE_CHANCE
    if not should_voice:
        await _original_send_bubbles_to_chat(application, chat_id, text)
        return

    try:
        chunks = _split_tts_chunks(text)
        if not chunks:
            raise RuntimeError("empty proactive TTS text")
        for index, chunk in enumerate(chunks):
            if index:
                await asyncio.sleep(0.25)
            audio = await _synthesize_elevenlabs(chunk)
            voice_file = io.BytesIO(audio)
            voice_file.name = f"proactive_voice_{index + 1}.mp3"
            await application.bot.send_voice(chat_id=chat_id, voice=voice_file)
        print(
            f"Proactive voice sent: chat_id={chat_id}, chunks={len(chunks)}, chars={len(_clean_tts_text(text))}",
            flush=True,
        )
    except Exception as exc:
        print(f"Proactive TTS fallback to text: {exc!r}", flush=True)
        await _original_send_bubbles_to_chat(application, chat_id, text)


# Autonomous contact remains probabilistic; explicit user requests above are forced.
app._send_bubbles_to_chat = _send_proactive_text_or_voice

# Quiet hours are 00:30-08:30 Asia/Shanghai. The previous implementation
# compared minutes-since-midnight against 30, which accidentally made nearly
# the entire day quiet.
QUIET_START_MINUTE_OF_DAY = 30
QUIET_END_MINUTE_OF_DAY = 8 * 60 + 30


def fixed_is_quiet_time(dt=None) -> bool:
    dt = dt or app._now_local()
    minute_of_day = dt.hour * 60 + dt.minute
    return QUIET_START_MINUTE_OF_DAY <= minute_of_day < QUIET_END_MINUTE_OF_DAY


def fixed_next_after_quiet(now_local=None) -> float:
    now_local = now_local or app._now_local()
    target = now_local.replace(
        hour=app.QUIET_END_HOUR,
        minute=app.QUIET_END_MINUTE,
        second=0,
        microsecond=0,
    )
    if now_local >= target:
        target += app.timedelta(days=1)
    target += app.timedelta(minutes=app.random.randint(0, 25))
    return target.timestamp()


def fixed_schedule_from_now(chat_id: int) -> None:
    now = app.time.time()
    next_at = now + app._random_delay()
    local_due = app.datetime.fromtimestamp(next_at, app.PROACTIVE_TZ)
    if fixed_is_quiet_time(local_due):
        next_at = fixed_next_after_quiet(local_due)

    app._set_setting(chat_id, app.KEY_NEXT_AT, str(int(next_at)))
    scheduled = app.datetime.fromtimestamp(next_at, app.PROACTIVE_TZ)
    print(
        f"Next proactive message scheduled: chat_id={chat_id}, at={scheduled:%Y-%m-%d %H:%M:%S %Z}",
        flush=True,
    )


# Patch every caller in runner_strict_bubbles to use the corrected scheduling.
app._is_quiet_time = fixed_is_quiet_time
app._next_after_quiet = fixed_next_after_quiet
app._schedule_from_now = fixed_schedule_from_now


# Start the background loop directly on the running asyncio loop. This avoids
# python-telegram-bot warning that Application.create_task() was called before
# Application.running became true during post_init.
async def fixed_post_init(application) -> None:
    await app._original_post_init(application)

    task = asyncio.create_task(app.proactive_loop(application), name="proactive-message-loop")
    application.bot_data["proactive_message_task"] = task

    chat_id = app._owner_chat_id()
    if chat_id is not None and not app._setting(chat_id, app.KEY_NEXT_AT):
        fixed_schedule_from_now(chat_id)

    voice_status = (
        f"ready, chance={PROACTIVE_VOICE_CHANCE:.0%}, forced_request=on, model={ELEVENLABS_MODEL_ID}"
        if _voice_ready()
        else "disabled until ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are configured"
    )
    print(
        "Proactive messaging enabled: interval=20m-60m, daily_max=8, "
        "quiet=00:30-08:30 Asia/Shanghai; persistent_time_awareness=on; "
        f"voice={voice_status}",
        flush=True,
    )


app.runner.post_init = fixed_post_init


if __name__ == "__main__":
    app.runner.main()
