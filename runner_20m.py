import asyncio
import io
import os
import re
from contextvars import ContextVar

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
_base_build_system_prompt_for_voice = app.runner.bot.build_system_prompt

# Per-update context so voice persona affects only the specific explicit voice
# request currently being generated, never ordinary text replies.
_voice_persona_chat_id: ContextVar[int | None] = ContextVar(
    "voice_persona_chat_id", default=None
)

VOICE_PERSONA_BLOCK = """
【语音回复模式｜只用于这一条回复】
这次回复最终会被直接转换成一条私人语音消息。请按真实的人正在临时发语音来组织内容，而不是按文字聊天、客服回答、旁白或朗读稿来写。

表达规则：
- 默认使用自然中文口语，像真实私聊，不像文章。
- 优先 1-4 个短句；能短就短。除非用户确实问了复杂问题，否则不要长篇解释。
- 语气沉稳、成熟、略冷，但不要低能量到拖拍；保持正常聊天语速，句子要利落。
- 要有自然的语气起伏：重点词可以更重，疑问句自然上扬，安慰时放柔，吃醋或不满时压低；不要每句话都同一种平直句尾。
- 不要把每个字平均用力，不要刻意逐字咬字，不要主播腔、客服腔、播音腔。
- 不要油腻，不要霸总台词堆砌，不要刻意装酷，不要每句都撩。
- 不要频繁使用“宝贝”“乖”“女人”等套路称呼，除非现有人设和关系自然需要。
- 可以有自然的不完整句和短促回应，但不要为了营造氛围频繁使用省略号，也不要故意拉长停顿。
- 不要使用“首先、其次、总结一下、作为AI、我建议你”等说明式结构，除非内容本身必须严谨解释。
- 不要输出动作描写、舞台说明、括号表演提示、星号动作、TTS标签或旁白，例如“*低笑*”“（靠近你）”“[whispers]”。
- 最终只输出角色真正会说出口的话，不要解释语气、表情、动作或声音。
- 如果用户是在撒娇、失落、吃醋、晚安、想念等亲密语境里，优先用少量直接的话回应，不要分析情绪。
- 如果用户问的是事实、技术或需要完整答案的问题，仍然保证内容正确完整，但改成适合口头表达的短句和分段，不要为了有氛围牺牲信息。

整体感觉：年轻成年男性，安静、自持、可靠，话不多，但绝不是慢吞吞的朗读。语气会随内容自然变化，关心通常藏在简短的话里。不要模仿或声称自己是任何具体角色、演员或真人。
""".strip()

# Faster and more expressive than the previous conservative tuning. Lower
# stability permits more prosody; higher style gives stronger emotional contour;
# speed sits slightly above neutral so Mandarin does not drag.
VOICE_SIMILARITY = 0.78
VOICE_SPEAKER_BOOST = True
VOICE_STABILITY_RANGE = (0.26, 0.34)
VOICE_STYLE_RANGE = (0.34, 0.48)
VOICE_SPEED_RANGE = (1.04, 1.08)


def _voice_ready() -> bool:
    return bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)


def _looks_like_stage_direction(content: str) -> bool:
    compact = re.sub(r"\s+", "", content).lower()
    if not compact or len(compact) > 36:
        return False
    hints = (
        "轻声", "低声", "低笑", "轻笑", "笑", "叹气", "停顿", "沉默", "挑眉", "皱眉",
        "靠近", "凑近", "看着", "盯着", "摸", "抱", "亲", "吻", "勾唇", "垂眼",
        "whisper", "sigh", "laugh", "pause", "smile", "looks", "leans", "chuckle",
    )
    return any(hint in compact for hint in hints)


def _prepare_spoken_text(text: str) -> str:
    raw = text or ""

    # Do not literally read roleplay actions such as *低笑* or （靠近你） aloud.
    raw = re.sub(
        r"\*([^*\n]{1,40})\*",
        lambda m: "" if _looks_like_stage_direction(m.group(1)) else m.group(1),
        raw,
    )
    raw = re.sub(
        r"（([^）\n]{1,40})）",
        lambda m: "" if _looks_like_stage_direction(m.group(1)) else m.group(0),
        raw,
    )
    raw = re.sub(
        r"\[([^\]\n]{1,40})\]",
        lambda m: "" if _looks_like_stage_direction(m.group(1)) else m.group(0),
        raw,
    )

    # One line break is a light conversational pause. Avoid converting every
    # break into an ellipsis because that makes speech unnaturally slow.
    raw = re.sub(r"\r?\n\s*\r?\n+", "。", raw)
    raw = re.sub(r"\r?\n+", "，", raw)

    clean = re.sub(r"```.*?```", "", raw, flags=re.S)
    clean = re.sub(r"`([^`]*)`", r"\1", clean)
    clean = re.sub(r"[*_~#>]", "", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    clean = re.sub(r"，{2,}", "，", clean)
    clean = re.sub(r"。{2,}", "。", clean)
    clean = re.sub(r"…{4,}", "……", clean)
    return clean


def _basic_spoken_fallback(text: str) -> str:
    """Keep something speakable even if stage-direction filtering removed everything."""
    raw = text or ""
    raw = re.sub(r"```.*?```", "", raw, flags=re.S)
    raw = re.sub(r"[`*_~#>]", "", raw)
    raw = re.sub(r"[\[\]（）()]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _clean_tts_text(text: str) -> str:
    return _prepare_spoken_text(text)


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


def build_system_prompt_with_voice_persona(chat_id: int) -> str:
    prompt = _base_build_system_prompt_for_voice(chat_id)
    if _voice_persona_chat_id.get() == chat_id:
        prompt += "\n\n" + VOICE_PERSONA_BLOCK
    return prompt


# The time-awareness/system prompt from runner_strict_bubbles stays intact; this
# appends voice-specific generation guidance only during explicit voice requests.
app.runner.bot.build_system_prompt = build_system_prompt_with_voice_persona


def _split_tts_chunks(text: str, max_chars: int = VOICE_MAX_CHARS) -> list[str]:
    spoken = _prepare_spoken_text(text)
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
            chunks.extend(part[i : i + max_chars] for i in range(0, len(part), max_chars))
            continue
        if current and len(current) + len(part) > max_chars:
            chunks.append(current)
            current = part
        else:
            current += part
    if current:
        chunks.append(current)
    return chunks


def _natural_voice_settings() -> dict:
    return {
        "stability": round(app.random.uniform(*VOICE_STABILITY_RANGE), 2),
        "similarity_boost": VOICE_SIMILARITY,
        "style": round(app.random.uniform(*VOICE_STYLE_RANGE), 2),
        "use_speaker_boost": VOICE_SPEAKER_BOOST,
        "speed": round(app.random.uniform(*VOICE_SPEED_RANGE), 2),
    }


async def _synthesize_elevenlabs(text: str) -> bytes:
    spoken = _prepare_spoken_text(text) or _basic_spoken_fallback(text)
    if not spoken:
        raise RuntimeError("empty TTS text")
    if len(spoken) > VOICE_MAX_CHARS:
        raise RuntimeError(f"TTS text too long: {len(spoken)} chars")

    settings = _natural_voice_settings()
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
        "voice_settings": settings,
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
    print(
        "TTS expressive settings: "
        f"stability={settings['stability']}, style={settings['style']}, speed={settings['speed']}",
        flush=True,
    )
    return response.content


async def _repair_empty_voice_reply(update, original_text: str) -> str:
    """Rare fallback: ask Claude for a real spoken line instead of sending text."""
    chat_id = update.effective_chat.id
    user_text = update.message.text if update.message else ""
    recent = list(app.runner.bot.histories[chat_id][-6:])
    repair_prompt = {
        "role": "user",
        "content": (
            "【语音输出修复】上一版回复清理掉动作描写后没有可朗读内容。"
            "请立刻重写成1-2句这个角色真正会说出口的自然中文，只输出台词。"
            "不要动作描写，不要括号，不要星号，不要解释。语速感正常利落，有自然语气起伏。\n"
            f"用户刚才说：{user_text}\n"
            f"原回复：{original_text}"
        ),
    }
    try:
        repaired = await app.runner.bot.ask_claude(
            chat_id,
            [*recent, repair_prompt],
            app.runner.bot.current_model(chat_id),
        )
        return _prepare_spoken_text(repaired) or _basic_spoken_fallback(repaired)
    except Exception as exc:
        print(f"Voice repair generation failed: {exc!r}", flush=True)
        return _basic_spoken_fallback(original_text)


async def _reply_as_forced_voice(update, text: str) -> None:
    chunks = _split_tts_chunks(text)
    if not chunks:
        repaired = await _repair_empty_voice_reply(update, text)
        chunks = _split_tts_chunks(repaired)
    if not chunks:
        fallback = _basic_spoken_fallback(text)
        chunks = [fallback[:VOICE_MAX_CHARS]] if fallback else ["嗯，我在。"]

    for index, chunk in enumerate(chunks):
        if index:
            await asyncio.sleep(0.18)
        audio = await _synthesize_elevenlabs(chunk)
        voice_file = io.BytesIO(audio)
        voice_file.name = f"voice_reply_{index + 1}.mp3"
        await update.message.reply_voice(voice=voice_file)

    print(
        f"Forced voice reply sent: chat_id={update.effective_chat.id}, chunks={len(chunks)}, chars={sum(len(c) for c in chunks)}",
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
# forced voice requests without changing Claude history or memory logic.
app.runner.bot.safe_reply = _safe_reply_with_explicit_voice


# Wrap the already-patched proactive/time-aware text handler. The ContextVar is
# active throughout Claude generation and automatically resets afterwards.
_original_voice_persona_handler = app.runner.handle_text_with_memory


async def handle_text_with_voice_persona(update, context) -> None:
    if not update.message or not update.message.text:
        await _original_voice_persona_handler(update, context)
        return

    chat_id = update.effective_chat.id
    wants_voice = (
        app.runner.bot.is_authorized(chat_id)
        and _explicit_voice_request(update.message.text)
    )

    token = None
    if wants_voice:
        token = _voice_persona_chat_id.set(chat_id)
        print(f"Voice persona generation enabled: chat_id={chat_id}", flush=True)

    try:
        await _original_voice_persona_handler(update, context)
    finally:
        if token is not None:
            _voice_persona_chat_id.reset(token)


app.runner.handle_text_with_memory = handle_text_with_voice_persona
app.runner.bot.handle_text = handle_text_with_voice_persona


async def _send_proactive_text_or_voice(application, chat_id: int, text: str) -> None:
    should_voice = _voice_ready() and app.random.random() < PROACTIVE_VOICE_CHANCE
    if not should_voice:
        await _original_send_bubbles_to_chat(application, chat_id, text)
        return

    try:
        chunks = _split_tts_chunks(text)
        if not chunks:
            fallback = _basic_spoken_fallback(text)
            if not fallback:
                raise RuntimeError("empty proactive TTS text")
            chunks = [fallback[:VOICE_MAX_CHARS]]
        for index, chunk in enumerate(chunks):
            if index:
                await asyncio.sleep(0.18)
            audio = await _synthesize_elevenlabs(chunk)
            voice_file = io.BytesIO(audio)
            voice_file.name = f"proactive_voice_{index + 1}.mp3"
            await application.bot.send_voice(chat_id=chat_id, voice=voice_file)
        print(
            f"Proactive voice sent: chat_id={chat_id}, chunks={len(chunks)}, chars={sum(len(c) for c in chunks)}",
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
        f"ready, chance={PROACTIVE_VOICE_CHANCE:.0%}, forced_request=on, expressive_tuning=on, voice_persona=on, model={ELEVENLABS_MODEL_ID}"
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
