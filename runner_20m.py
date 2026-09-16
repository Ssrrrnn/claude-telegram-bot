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

# Optional ElevenLabs voice notes for proactive messages.  If the credentials
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

VOICE_MAX_CHARS = 260
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
_original_send_bubbles_to_chat = app._send_bubbles_to_chat


def _voice_ready() -> bool:
    return bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)


def _clean_tts_text(text: str) -> str:
    clean = re.sub(r"```.*?```", "", text, flags=re.S)
    clean = re.sub(r"`([^`]*)`", r"\1", clean)
    clean = re.sub(r"[*_~#>]", "", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


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


async def _send_proactive_text_or_voice(application, chat_id: int, text: str) -> None:
    should_voice = _voice_ready() and app.random.random() < PROACTIVE_VOICE_CHANCE
    if not should_voice:
        await _original_send_bubbles_to_chat(application, chat_id, text)
        return

    try:
        audio = await _synthesize_elevenlabs(text)
        voice_file = io.BytesIO(audio)
        voice_file.name = "proactive_voice.mp3"
        await application.bot.send_voice(chat_id=chat_id, voice=voice_file)
        print(
            f"Proactive voice sent: chat_id={chat_id}, chars={len(_clean_tts_text(text))}",
            flush=True,
        )
    except Exception as exc:
        print(f"Proactive TTS fallback to text: {exc!r}", flush=True)
        await _original_send_bubbles_to_chat(application, chat_id, text)


# Use the optional voice path only for autonomous/proactive contact.  Normal
# user-triggered replies remain text so voice notes feel occasional rather than constant.
app._send_bubbles_to_chat = _send_proactive_text_or_voice

# Quiet hours are 00:30-08:30 Asia/Shanghai.  The previous implementation
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


# Start the background loop directly on the running asyncio loop.  This avoids
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
        f"ready, chance={PROACTIVE_VOICE_CHANCE:.0%}, model={ELEVENLABS_MODEL_ID}"
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
