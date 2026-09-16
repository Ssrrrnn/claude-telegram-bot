import asyncio
import io
import os
import re
from contextvars import ContextVar

import httpx

import runner_strict_bubbles as app

# Keep the faster proactive cadence without changing memory/time-awareness.
app.PROACTIVE_MIN_SECONDS = 20 * 60
app.PROACTIVE_MAX_SECONDS = 60 * 60

# -----------------------------
# ElevenLabs configuration
# -----------------------------
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
_base_build_system_prompt = app.runner.bot.build_system_prompt
_original_text_handler = app.runner.handle_text_with_memory

# This context exists only while an explicit voice-request message is being handled.
_voice_persona_chat_id: ContextVar[int | None] = ContextVar(
    "voice_persona_chat_id", default=None
)


# -----------------------------
# Normal Telegram text-chat mode
# -----------------------------
TEXT_CHAT_BLOCK = """
【Telegram普通文字私聊格式｜默认模式】
除非用户明确要求写小说、剧情、场景、动作描写或剧本，否则你现在是在 Telegram 里直接和用户聊天，不是在写小说。

普通闲聊时必须遵守：
- 只输出角色真正会直接发给用户的话。
- 不要写第三人称或舞台式动作旁白，例如“看着你发的这个，眼神暗了暗”“靠在椅背上，视线没移开屏幕”“轻笑了一声”。
- 不要用星号、括号、方括号描述动作、神态、语气或镜头。
- 不要把自己说的话套在英文双引号、中文引号里；直接说即可。
- 绝对不要单独输出一个引号、括号或其他残缺标点作为一条消息。
- 亲密闲聊可以短、自然、有情绪，但不要写成小说段落或角色扮演舞台说明。
- 复杂事实、技术、工作类问题仍然正常完整回答，不要为了短而损失信息。
- 如果用户明确要求小说/剧本/场景描写，再按用户要求切换格式；否则始终保持真实私聊消息感。
""".strip()


# -----------------------------
# Voice-only persona layer
# -----------------------------
VOICE_PERSONA_BLOCK = """
【语音回复模式｜只用于这一条回复】
这次回复最终会直接转换成私人语音消息。请按真实的人临时发语音来组织内容，不要按文章、客服回答、旁白或朗读稿来写。

表达规则：
- 默认自然中文口语，优先 1-4 个短句；能短就短。
- 语气沉稳、成熟、略冷，但保持正常聊天节奏，绝不要慢吞吞逐字念。
- 要有自然语气起伏：重点词稍重，疑问句自然上扬，安慰时放柔，吃醋或不满时自然压低。
- 不要每个字平均用力，不要主播腔、客服腔、播音腔。
- 不要油腻，不要刻意装酷，不要每句都撩。
- 不要频繁使用套路称呼，除非现有人设和关系自然需要。
- 不要为了氛围频繁使用省略号，也不要故意拉长停顿。
- 不要输出动作描写、舞台说明、括号表演提示、星号动作、TTS 标签或旁白。
- 最终只输出角色真正会说出口的话。
- 如果用户问复杂事实或技术问题，仍保证内容正确完整，只是改成适合口头表达的节奏。

整体感觉：年轻成年男性，安静、自持、可靠，话不多但有自然起伏。不要模仿或声称自己是任何具体角色、演员或真人。
""".strip()


# Faster and more expressive than the earlier slow/flat tuning.
VOICE_SIMILARITY = 0.78
VOICE_SPEAKER_BOOST = True
VOICE_STABILITY_RANGE = (0.26, 0.34)
VOICE_STYLE_RANGE = (0.34, 0.48)
VOICE_SPEED_RANGE = (1.04, 1.08)


def _voice_ready() -> bool:
    return bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)


def _explicit_voice_request(text: str) -> bool:
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


def build_system_prompt_with_chat_modes(chat_id: int) -> str:
    # Normal text-chat rules are always present. Voice rules are added only for
    # the single update that explicitly requested a voice reply.
    prompt = _base_build_system_prompt(chat_id) + "\n\n" + TEXT_CHAT_BLOCK
    if _voice_persona_chat_id.get() == chat_id:
        prompt += "\n\n" + VOICE_PERSONA_BLOCK
    return prompt


app.runner.bot.build_system_prompt = build_system_prompt_with_chat_modes


# -----------------------------
# Text output sanitation
# -----------------------------
_STAGE_HINTS = (
    "看着你", "盯着你", "看着屏幕", "盯着屏幕", "眼神", "视线",
    "靠在", "靠着", "靠进", "抬眼", "垂眼", "挑眉", "皱眉", "勾唇",
    "轻笑", "低笑", "笑了一声", "叹了口气", "沉默了", "停顿了",
    "伸手", "抬手", "指尖", "手指", "转身", "侧过脸", "低下头",
)


def _looks_like_stage_narration(line: str) -> bool:
    compact = re.sub(r"\s+", "", line or "")
    if not compact or len(compact) > 90:
        return False
    # Avoid deleting an actual question/direct message just because it contains
    # a common verb. These hints are intentionally specific to roleplay narration.
    if "?" in compact or "？" in compact:
        return False
    return any(hint in compact for hint in _STAGE_HINTS)


def _sanitize_text_chat_output(text: str) -> str:
    """Clean accidental novel/roleplay formatting without touching normal prose."""
    if not text:
        return text

    raw_lines = [line.strip() for line in re.split(r"\r?\n+", text) if line.strip()]
    if not raw_lines:
        return text

    quote_only = any(re.fullmatch(r'["“”‘’\'`]+', line) for line in raw_lines)
    stage_present = any(_looks_like_stage_narration(line) for line in raw_lines)
    quoted_count = sum(
        1 for line in raw_lines
        if re.match(r'^["“”‘’]', line) or re.search(r'["“”‘’]$', line)
    )

    # Only engage the sanitizer when the output actually looks like the broken
    # roleplay format. This preserves legitimate quoted text in normal answers.
    if not (quote_only or stage_present or quoted_count >= 2):
        return text

    cleaned: list[str] = []
    for line in raw_lines:
        if re.fullmatch(r'["“”‘’\'`]+', line):
            continue
        if _looks_like_stage_narration(line):
            continue

        # Strip accidental dialogue wrappers at the edges, including unmatched
        # leading/trailing quotes like the screenshot showed.
        line = re.sub(r'^["“”‘’]+\s*', "", line)
        line = re.sub(r'\s*["“”‘’]+$', "", line)
        line = line.strip()
        if line:
            cleaned.append(line)

    return "\n".join(cleaned).strip() or text


# -----------------------------
# TTS preparation
# -----------------------------
def _looks_like_stage_direction(content: str) -> bool:
    compact = re.sub(r"\s+", "", content).lower()
    if not compact or len(compact) > 36:
        return False
    hints = (
        "轻声", "低声", "低笑", "轻笑", "笑", "叹气", "停顿", "沉默",
        "挑眉", "皱眉", "靠近", "凑近", "看着", "盯着", "摸", "抱",
        "亲", "吻", "勾唇", "垂眼", "whisper", "sigh", "laugh", "pause",
        "smile", "looks", "leans", "chuckle",
    )
    return any(hint in compact for hint in hints)


def _prepare_spoken_text(text: str) -> str:
    raw = text or ""
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
    raw = text or ""
    raw = re.sub(r"```.*?```", "", raw, flags=re.S)
    raw = re.sub(r"[`*_~#>]", "", raw)
    raw = re.sub(r"[\[\]（）()]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


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
            chunks.extend(part[i:i + max_chars] for i in range(0, len(part), max_chars))
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
        raise RuntimeError(
            f"ElevenLabs HTTP {response.status_code}: {response.text[:500]}"
        )
    if not response.content:
        raise RuntimeError("ElevenLabs returned empty audio")

    print(
        "TTS expressive settings: "
        f"stability={settings['stability']}, style={settings['style']}, speed={settings['speed']}",
        flush=True,
    )
    return response.content


async def _repair_empty_voice_reply(update, original_text: str) -> str:
    chat_id = update.effective_chat.id
    user_text = update.message.text if update.message else ""
    recent = list(app.runner.bot.histories[chat_id][-6:])
    repair_prompt = {
        "role": "user",
        "content": (
            "【语音输出修复】上一版回复清理动作描写后没有可朗读内容。"
            "请立刻重写成1-2句这个角色真正会说出口的自然中文，只输出台词。"
            "不要动作描写，不要括号，不要星号，不要解释。\n"
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


# -----------------------------
# Reply routing
# -----------------------------
async def _safe_reply_with_chat_modes(update, text: str) -> None:
    user_text = update.message.text if update.message else ""
    wants_voice = _explicit_voice_request(user_text)

    if not wants_voice:
        cleaned = _sanitize_text_chat_output(text)
        await _original_user_safe_reply(update, cleaned or text)
        return

    if not _voice_ready():
        cleaned = _sanitize_text_chat_output(text)
        print(
            f"Forced voice requested but voice is not configured: chat_id={update.effective_chat.id}",
            flush=True,
        )
        await _original_user_safe_reply(update, cleaned or text)
        return

    try:
        await _reply_as_forced_voice(update, text)
    except Exception as exc:
        cleaned = _sanitize_text_chat_output(text)
        print(f"Forced TTS fallback to text: {exc!r}", flush=True)
        await _original_user_safe_reply(update, cleaned or text)


app.runner.bot.safe_reply = _safe_reply_with_chat_modes


async def handle_text_with_chat_modes(update, context) -> None:
    if not update.message or not update.message.text:
        await _original_text_handler(update, context)
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
        await _original_text_handler(update, context)
    finally:
        if token is not None:
            _voice_persona_chat_id.reset(token)

    # Keep future context clean too. generate_and_send stores the raw assistant
    # answer before safe_reply; replace accidental roleplay formatting in history
    # after an ordinary text response so it does not reinforce itself next turn.
    if not wants_voice:
        try:
            history = app.runner.bot.histories[chat_id]
            if history and history[-1].get("role") == "assistant":
                raw = history[-1].get("content")
                if isinstance(raw, str):
                    cleaned = _sanitize_text_chat_output(raw)
                    if cleaned:
                        history[-1]["content"] = cleaned
        except Exception as exc:
            print(f"Text chat history cleanup error: {exc!r}", flush=True)


app.runner.handle_text_with_memory = handle_text_with_chat_modes
app.runner.bot.handle_text = handle_text_with_chat_modes


# -----------------------------
# Proactive messages
# -----------------------------
async def _send_proactive_text_or_voice(application, chat_id: int, text: str) -> None:
    should_voice = _voice_ready() and app.random.random() < PROACTIVE_VOICE_CHANCE
    if not should_voice:
        cleaned = _sanitize_text_chat_output(text)
        await _original_send_bubbles_to_chat(application, chat_id, cleaned or text)
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
        cleaned = _sanitize_text_chat_output(text)
        print(f"Proactive TTS fallback to text: {exc!r}", flush=True)
        await _original_send_bubbles_to_chat(application, chat_id, cleaned or text)


app._send_bubbles_to_chat = _send_proactive_text_or_voice


# -----------------------------
# Correct quiet-hours scheduling
# -----------------------------
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


app._is_quiet_time = fixed_is_quiet_time
app._next_after_quiet = fixed_next_after_quiet
app._schedule_from_now = fixed_schedule_from_now


# -----------------------------
# Startup
# -----------------------------
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
        "Chat mode enabled: normal_text=direct_telegram_chat, narration_filter=on; "
        "proactive interval=20m-60m, daily_max=8, quiet=00:30-08:30 Asia/Shanghai; "
        "persistent_time_awareness=on; "
        f"voice={voice_status}",
        flush=True,
    )


app.runner.post_init = fixed_post_init


if __name__ == "__main__":
    app.runner.main()
