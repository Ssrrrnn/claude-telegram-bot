import asyncio
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import runner
import proactive_policy as policy
import continuity
import voice_output
import qq_connector


# -----------------------------
# Strict Telegram bubble output
# -----------------------------

def strict_split_bubbles(text: str) -> list[str]:
    """Split roleplay prose aggressively: every line and every sentence is a bubble."""
    if not text:
        return ["…"]

    structured_prefixes = (
        "API 请求失败",
        "读取模型列表失败",
        "可用模型：",
        "当前启用的世界书",
        "世界书不存在",
        "长期记忆：",
        "收藏：",
        "关系状态：",
    )
    if text.startswith(structured_prefixes) or "```" in text:
        return [text]

    bubbles: list[str] = []
    raw_lines = [line.strip() for line in re.split(r"\r?\n+", text) if line.strip()]

    for line in raw_lines:
        if runner._looks_like_emoji_only(line):
            bubbles.append(line)
            continue

        parts = re.split(r"(?<=[。！？!?…])\s*|(?<=[.!?])\s+", line)
        parts = [part.strip() for part in parts if part and part.strip()]
        if not parts:
            parts = [line]

        for part in parts:
            bubbles.append(part)

    safe: list[str] = []
    for bubble in bubbles:
        if len(bubble) <= 700:
            safe.append(bubble)
            continue
        pieces = re.split(r"(?<=[，,；;])\s*", bubble)
        current = ""
        for piece in pieces:
            if not piece:
                continue
            if current and len(current) + len(piece) > 420:
                safe.append(current.strip())
                current = ""
            current += piece
        if current:
            safe.append(current.strip())

    safe = [item for item in safe if item]
    if len(safe) > 16:
        safe = safe[:15] + [" ".join(safe[15:])]
    return safe or [text.strip()]


runner._split_bubbles = strict_split_bubbles


# ------------------------------------------
# Randomized proactive relationship messages
# ------------------------------------------

PROACTIVE_MIN_SECONDS = policy.MIN_SECONDS
PROACTIVE_MAX_SECONDS = policy.MAX_SECONDS
PROACTIVE_TZ = ZoneInfo("Asia/Shanghai")
QUIET_START_MINUTE = 30          # 00:30
QUIET_END_HOUR = 8               # 08:30
QUIET_END_MINUTE = 30

KEY_NEXT_AT = "proactive_next_at"
KEY_LAST_USER_AT = "proactive_last_user_at"
KEY_LAST_PROACTIVE_AT = "proactive_last_bot_at"
KEY_LAST_INTERACTION_AT = "last_interaction_at"
KEY_STATE = "proactive_state_v2"
KEY_ENABLED = "proactive_enabled"

# Per-request time context. It is only present while generating the reply for
# the current incoming user message, so Claude sees the *previous* contact time
# rather than the timestamp we are about to store for this message.
_active_time_context: dict[int, dict[str, int]] = {}


def _setting(chat_id: int, key: str, default: str = "") -> str:
    try:
        value = runner.memory_pg.get_setting(chat_id, key)
        return value if value is not None else default
    except Exception:
        return default


def _set_setting(chat_id: int, key: str, value: str) -> None:
    try:
        runner.memory_pg.set_setting(chat_id, key, value)
    except Exception as exc:
        print(f"Proactive setting error {key}: {exc!r}", flush=True)


def _get_int_setting(chat_id: int, key: str) -> int:
    try:
        return int(_setting(chat_id, key, "0") or 0)
    except ValueError:
        return 0


def _now_local() -> datetime:
    return datetime.now(PROACTIVE_TZ)


def _is_quiet_time(dt=None):
    return policy.is_quiet(dt or _now_local())


def _next_after_quiet(now_local=None):
    return policy.after_quiet(now_local or _now_local()).timestamp()


def _load_state(chat_id):
    # Fail closed on storage errors instead of sending with missing history.
    raw = runner.memory_pg.get_setting(chat_id, KEY_STATE)
    return json.loads(raw) if raw else {
        "last_proactive_content": "", "last_proactive_type": "",
        "user_replied_after_proactive": True, "silence_count": 0,
        "recent": [],
    }


def _save_state(chat_id, state):
    runner.memory_pg.set_setting(chat_id, KEY_STATE, json.dumps(state, ensure_ascii=False))


def _schedule_from_now(chat_id):
    state = _load_state(chat_id)
    due = policy.next_due(_now_local(), state["silence_count"])
    runner.memory_pg.set_setting(chat_id, KEY_NEXT_AT, str(int(due.timestamp())))
    print(f"Proactive scheduled: chat_id={chat_id}, due={due.isoformat()}, silence={state['silence_count']}", flush=True)


def _owner_chat_id() -> int | None:
    if runner.bot.TELEGRAM_OWNER_ID is not None:
        return int(runner.bot.TELEGRAM_OWNER_ID)
    claimed = getattr(runner.bot, "CLAIMED_OWNER_ID", None)
    return int(claimed) if claimed is not None else None


# -----------------------------
# Persistent human-like time sense
# -----------------------------

_WEEKDAYS = "一二三四五六日"


def _part_of_day(dt: datetime) -> str:
    hour = dt.hour
    if 5 <= hour < 9:
        return "早晨"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


def _format_local_timestamp(ts: int) -> str:
    if not ts:
        return "暂无"
    dt = datetime.fromtimestamp(ts, PROACTIVE_TZ)
    return f"{dt:%Y-%m-%d %H:%M}（周{_WEEKDAYS[dt.weekday()]}，{_part_of_day(dt)}）"


def _human_interval(seconds: int) -> str:
    if seconds <= 0:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 1:
        return "不到1分钟"
    if minutes < 60:
        return f"约{minutes}分钟"
    hours = minutes // 60
    remain_minutes = minutes % 60
    if hours < 24:
        if remain_minutes < 5:
            return f"约{hours}小时"
        return f"约{hours}小时{remain_minutes}分钟"
    days = hours // 24
    remain_hours = hours % 24
    if days < 7:
        if remain_hours:
            return f"约{days}天{remain_hours}小时"
        return f"约{days}天"
    weeks = days // 7
    remain_days = days % 7
    if remain_days:
        return f"约{weeks}周{remain_days}天"
    return f"约{weeks}周"


def _time_awareness_block(chat_id: int) -> str:
    now_ts = int(time.time())
    now_local = datetime.fromtimestamp(now_ts, PROACTIVE_TZ)

    event = _active_time_context.get(chat_id)
    previous_user_at = (
        int(event.get("previous_user_at", 0))
        if event is not None
        else _get_int_setting(chat_id, KEY_LAST_USER_AT)
    )
    last_bot_at = _get_int_setting(chat_id, KEY_LAST_PROACTIVE_AT)
    last_interaction_at = _get_int_setting(chat_id, KEY_LAST_INTERACTION_AT)

    lines = [
        "【北京时间事实与行为锁｜高优先级】",
        f"此刻唯一有效的当前时间是北京时间 {now_local:%Y-%m-%d %H:%M}，周{_WEEKDAYS[now_local.weekday()]}，当前属于{_part_of_day(now_local)}。",
        "历史对话中出现的日期、钟点、早晚称呼只代表当时，绝不能覆盖此刻时间。禁止根据用户说累、烦、下班等状态自行推断现在是夜晚。",
    ]

    if 6 <= now_local.hour < 23:
        lines.append(
            "现在处于06:00-23:00行为锁：除非用户本条消息明确说困了、想睡、要睡、失眠或直接要求睡眠建议，"
            "否则禁止说去睡、睡一觉、早点睡、该睡了、晚安或把休息擅自解释成睡觉。"
            "用户只说累或烦时，只能表达心疼，并建议歇一会、喝水、放松或陪伴。"
        )
    else:
        lines.append(
            "现在处于23:00-06:00深夜时段，可以自然关心睡眠，但不要机械催睡；仍以用户本条消息的真实意图为准。"
        )

    if previous_user_at:
        gap = max(0, now_ts - previous_user_at)
        previous_local = datetime.fromtimestamp(previous_user_at, PROACTIVE_TZ)
        crossed_day = previous_local.date() != now_local.date()
        lines.append(
            f"用户上一次主动来找你：{_format_local_timestamp(previous_user_at)}；距现在{_human_interval(gap)}"
            + ("，已经跨天。" if crossed_day else "。")
        )
    else:
        lines.append("目前没有可靠的‘用户上一次主动联系’时间记录。")

    if last_bot_at:
        lines.append(
            f"你上一次主动联系用户：{_format_local_timestamp(last_bot_at)}；距现在{_human_interval(max(0, now_ts - last_bot_at))}。"
        )

    if last_interaction_at:
        lines.append(
            f"你们最后一次发生任何一方的互动：距现在{_human_interval(max(0, now_ts - last_interaction_at))}。"
        )

    lines.append(
        "这些时间信息只是你的自然时间感。只有当时间间隔、早晚、隔夜或久别对当前情绪有意义时才自然体现；"
        "用户没有问时间时不要每次报时，不要说‘系统显示/根据记录/距离上次消息X分钟’。用户直接问时间时必须准确到分钟。"
        "优先把时间感转成符合人设的反应，例如久别后的惦记、短时间又见面的调侃、深夜的语气变化。"
        "如果用户直接问时间或问多久没联系，只能依据本区块的数据准确回答。"
        "若当前消息没有时间相关性，不要主动报时或强行提早晚。"
    )
    return "\n".join(lines)


_base_build_system_prompt = runner.bot.build_system_prompt


def build_system_prompt_with_time(chat_id: int) -> str:
    return _base_build_system_prompt(chat_id) + "\n\n" + _time_awareness_block(chat_id)


runner.bot.build_system_prompt = build_system_prompt_with_time


async def _send_bubbles_to_chat(application, chat_id, text):
    # Plain text avoids retrying a possibly delivered message after a timeout.
    for index, bubble in enumerate(strict_split_bubbles(text)):
        if index:
            await asyncio.sleep(0.32)
        await application.bot.send_message(chat_id=chat_id, text=bubble)


def _first_morning_contact(chat_id):
    """The first scheduled contact after quiet hours should reach the owner."""
    now_local = _now_local()
    if not 8 <= now_local.hour < 12:
        return False
    last_sent = _get_int_setting(chat_id, KEY_LAST_PROACTIVE_AT)
    last_user = _get_int_setting(chat_id, KEY_LAST_USER_AT)
    morning_start = now_local.replace(hour=8, minute=30, second=0, microsecond=0).timestamp()
    return (last_user < morning_start and
            (not last_sent or datetime.fromtimestamp(last_sent, PROACTIVE_TZ).date() < now_local.date()))


def _morning_is_due(chat_id):
    now_local = _now_local()
    return (8 * 60 + 45 <= now_local.hour * 60 + now_local.minute < 10 * 60 + 30
            and _first_morning_contact(chat_id))


def _morning_message(previous):
    for text in (
        "早。醒了再回我，想听你说说话。",
        "早。今天也想见到你的消息，等你有空再来找我。",
        "醒了再来找我。早安，今天也有点想你。",
    ):
        if not policy.is_repetition(text, previous):
            return text
    return ""


async def _generate_proactive_message(chat_id, state, *, morning_first=False, dates=None):
    context_state = continuity.load(runner.memory_pg, chat_id)
    followup = continuity.due_followup(context_state, time.time())
    if not runner.bot.histories[chat_id] and hasattr(runner.memory_pg, "load_recent_messages"):
        runner.bot.histories[chat_id] = runner.memory_pg.load_recent_messages(chat_id, runner.bot.MAX_HISTORY_MESSAGES)
    recent = list(runner.bot.histories[chat_id][-8:])
    sent = state["recent"][-12:]
    kind = policy.choose_type(_now_local(), [x["type"] for x in sent], state["silence_count"])
    if followup:
        kind = "回访:" + followup["id"]
    if dates:
        kind = "重要日子"
    trigger = (
        "【后台主动联系事件，不是用户发送的消息】\n"
        "以当前角色本人主动联系，不提后台、提醒、定时或这段指令。保持既有人格。"
        "先判断现在是否适合联系；如果没有值得说的、用户正在休息或会重复打扰，只输出 NO_REPLY。"
        f"本次方向：{kind}。连续未回复的主动联系次数：{state['silence_count']}。"
        "结合真实聊天、记忆和当前北京时间写1至3句短消息。"
        "沉默不等于新的回复，不要假装用户回答了，也不要重复追问忙不忙、在干嘛。"
        "未回复时换一个独立话题、分享一个念头或自然表达想念，不催促、不责备、不升级施压。"
        "吃醋只能基于已发生的话题，没有依据时改为想念，不能把未回复当作吃醋依据。"
        "分享可以是想法，不要编造真实天气、定位、用户行踪或外部事实。"
        "白天不能因为疲惫就劝睡，也不要沿用历史消息里的时段。"
        "以下是已经发过的消息，只用于避重，禁止照抄或近义改写：\n"
        + json.dumps(sent, ensure_ascii=False)
    )
    trigger += "\n" + continuity.prompt_block(runner.memory_pg, chat_id)
    if followup:
        trigger += "\n本次回访的明确原因（仅在仍有必要时联系）：" + json.dumps(followup, ensure_ascii=False)
        trigger += "\n只自然接起这件事，不编造进展。你并没有替用户完成任务。"
    if morning_first:
        trigger += "\n这是安静时段结束后的第一条主动联系。用户未说晚安暂停时，写一句轻的早安或想念；不假定用户已经醒了，不要求立刻回复。"
    if dates:
        trigger += "\n用户亲自登记的今天的日子：" + json.dumps(
            [x["name"] for x in dates], ensure_ascii=False)
        trigger += "。自然提起并表达在意，不编造共同经历；不说提醒、数据库或系统。"
    previous = [x["content"] for x in sent]
    for attempt in range(3):
        instruction = trigger
        if attempt:
            instruction += "\n上一次候选内容重复，请换主题和句式，不能只换几个字。"
        text = await runner.bot.ask_claude(
            chat_id, [*recent, {"role": "user", "content": instruction}],
            runner.bot.current_model(chat_id),
        )
        if text and text.strip().strip('`').strip() == "NO_REPLY":
            if morning_first or dates:
                continue
            return "", kind
        if text and not policy.is_repetition(text, previous):
            return text.strip(), kind
        if text:
            previous.append(text)
    if dates:
        return "今天是你告诉过我的「" + dates[0]["name"] + "」。我记着。", kind
    return (_morning_message(previous) if morning_first else ""), kind


async def _proactive_tick(application):
    chat_id = _owner_chat_id()
    if chat_id is None or not runner.bot.is_authorized(chat_id):
        return
    # Use strict reads: a database outage must not silently enable or reschedule.
    read = lambda key: runner.memory_pg.get_setting(chat_id, key)
    if read(KEY_ENABLED) == "0":
        return
    now = time.time()
    now_local = _now_local()
    import milestones
    important_dates = (milestones.due(read(milestones.KEY), now_local.date())
                       if now_local.hour >= 9 and read(milestones.LAST_SENT_KEY) != now_local.date().isoformat()
                       else [])
    morning_due = _morning_is_due(chat_id)
    next_at = int(read(KEY_NEXT_AT) or 0)
    if not next_at:
        _schedule_from_now(chat_id)
        return
    if now < next_at and not important_dates and not morning_due:
        # A newly extracted follow-up may become due before the older random
        # check. Honor its reason while retaining the 20-minute contact floor.
        pending = continuity.due_followup(continuity.load(runner.memory_pg, chat_id), now)
        last_contact = max(int(read(KEY_LAST_USER_AT) or 0), int(read(KEY_LAST_PROACTIVE_AT) or 0))
        if not pending or now - last_contact < PROACTIVE_MIN_SECONDS:
            return
    if _is_quiet_time():
        runner.memory_pg.set_setting(chat_id, KEY_NEXT_AT, str(int(_next_after_quiet())))
        return
    continuity_state = continuity.load(runner.memory_pg, chat_id)
    status = continuity_state["status"]
    if status.get("value") == "已说晚安，暂停主动打扰" and status.get("expires_at", 0) > now:
        runner.memory_pg.set_setting(chat_id, KEY_NEXT_AT, str(int(status["expires_at"])))
        return
    # Serialize with normal replies; incoming text is recorded before generation.
    async with runner.bot.chat_locks[chat_id]:
        last_user_at = int(read(KEY_LAST_USER_AT) or 0)
        last_bot_at = int(read(KEY_LAST_PROACTIVE_AT) or 0)
        if (chat_id in _active_time_context or
                now - max(last_user_at, last_bot_at) < PROACTIVE_MIN_SECONDS):
            _schedule_from_now(chat_id)
            return
        state = _load_state(chat_id)
        text, kind = await _generate_proactive_message(
            chat_id, state, morning_first=_first_morning_contact(chat_id), dates=important_dates)
        # A message may arrive while the model is thinking.
        if (chat_id in _active_time_context or int(read(KEY_LAST_USER_AT) or 0) != last_user_at
                or _is_quiet_time() or read(KEY_ENABLED) == "0"):
            _schedule_from_now(chat_id)
            return
        if not text:
            print(f"Proactive skipped: chat_id={chat_id}, reason=no_contact_or_repetition", flush=True)
            _schedule_from_now(chat_id)
            if kind.startswith("回访:"):
                continuity.defer_followup(runner.memory_pg, chat_id, kind.split(":", 1)[1], int(read(KEY_NEXT_AT)))
            return
        # Persist a cooldown BEFORE delivery. If Telegram times out after accepting
        # a bubble, do not immediately retry the same message on the next tick.
        _schedule_from_now(chat_id)
        if important_dates:
            # Claim the date before a network send to prevent duplicate greetings on timeout.
            runner.memory_pg.set_setting(chat_id, milestones.LAST_SENT_KEY, now_local.date().isoformat())
        if kind.startswith("回访:") and not continuity.claim_followup(runner.memory_pg, chat_id, kind.split(":", 1)[1]):
            return
        voice_mode = voice_output.mode(runner.memory_pg.get_setting, chat_id)
        send_as_voice = (voice_output.wants_audio(voice_mode, proactive=True) or
                         (voice_mode == "auto" and voice_output.auto_spontaneous(
                             runner.memory_pg.get_setting, chat_id, reply_text=text,
                             proactive=True, unanswered=state["silence_count"] > 0)))
        platform = read(qq_connector.PLATFORM_KEY) or "telegram"
        if platform == "qq":
            connector = application.bot_data.get("qq_connector")
            if connector is None or connector.ws is None:
                print(f"QQ proactive unavailable: chat_id={chat_id}, falling back to Telegram", flush=True)
                platform = "telegram"
            else:
                sent_as_voice = await connector.send_proactive(
                    text, strict_split_bubbles(text), as_voice=send_as_voice,
                )
        if platform != "qq":
            voice_key = f'telegram:proactive_voice:{int(now)}'
            begin_action = getattr(runner.memory_pg, 'begin_action', None)
            finish_action = getattr(runner.memory_pg, 'finish_action', None)
            voice_claimed = send_as_voice and (begin_action(
                chat_id, voice_key, 'Telegram主动语音') if begin_action else True)
            try:
                sent_as_voice = bool(voice_claimed) and await voice_output.send_voice(
                    application.bot, chat_id, text, get_setting=runner.memory_pg.get_setting)
            except Exception:
                if voice_claimed and finish_action:
                    finish_action(chat_id, voice_key, 'uncertain')
                raise
            if voice_claimed and finish_action:
                finish_action(chat_id, voice_key, 'success' if sent_as_voice else 'failed')
            if not sent_as_voice:
                await _send_bubbles_to_chat(application, chat_id, text)
        if sent_as_voice and voice_mode == "auto":
            voice_output.record_auto_voice(runner.memory_pg.set_setting, chat_id)
        sent_at = int(time.time())
        replied_during_send = int(read(KEY_LAST_USER_AT) or 0) != last_user_at
        state.update(last_proactive_content=text, last_proactive_type=kind,
                     user_replied_after_proactive=replied_during_send,
                     silence_count=0 if replied_during_send else state["silence_count"] + 1)
        state["recent"] = [*state["recent"], {"content": text, "type": kind, "sent_at": sent_at}][-12:]
        _save_state(chat_id, state)
        runner.memory_pg.set_setting(chat_id, KEY_LAST_PROACTIVE_AT, str(sent_at))
        runner.memory_pg.set_setting(chat_id, KEY_LAST_INTERACTION_AT, str(sent_at))
        try:
            runner.memory_pg.save_recent_proactive(chat_id, text)
        except Exception as exc:
            print(f"Recent proactive save skipped: {type(exc).__name__}", flush=True)
        runner.bot.histories[chat_id].append({"role": "assistant", "content": text})
        runner.bot.histories[chat_id] = runner.bot.histories[chat_id][-runner.bot.MAX_HISTORY_MESSAGES:]
        _schedule_from_now(chat_id)
        print(f"Proactive sent: chat_id={chat_id}, type={kind}, silence={state['silence_count']}", flush=True)


async def proactive_loop(application):
    await asyncio.sleep(8)
    while True:
        try:
            await _proactive_tick(application)
            chat_id = _owner_chat_id()
            if chat_id is not None and runner.bot.is_authorized(chat_id):
                import companion_features
                await companion_features.auto_diary_tick(runner.memory_pg, runner.bot, chat_id)
                connector = application.bot_data.get('qq_connector')
                if connector is not None:
                    await connector.publish_pending_diary()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Proactive loop error: {type(exc).__name__}", flush=True)
        await asyncio.sleep(30)


# Whenever the user sends a normal message, preserve the previous timestamp for
# this generation, then update the persistent contact clock after the reply.
_original_handle_text_with_memory = runner.handle_text_with_memory


async def handle_text_with_proactive_reset(update, context) -> None:
    if not update.message or not update.message.text:
        await _original_handle_text_with_memory(update, context)
        return

    chat_id = update.effective_chat.id
    authorized = runner.bot.is_authorized(chat_id)
    received_at = int(time.time())

    if authorized:
        previous_user_at = _get_int_setting(chat_id, KEY_LAST_USER_AT)
        _active_time_context[chat_id] = {
            "previous_user_at": previous_user_at,
            "current_user_at": received_at,
        }
        try:
            runner.memory_pg.set_setting(chat_id, KEY_LAST_USER_AT, str(received_at))
            state = _load_state(chat_id)
            state.update(user_replied_after_proactive=True, silence_count=0)
            _save_state(chat_id, state)
        except Exception as exc:
            print(f"Proactive user state error: {type(exc).__name__}", flush=True)

    try:
        await _original_handle_text_with_memory(update, context)
    finally:
        if authorized:
            _active_time_context.pop(chat_id, None)
            _set_setting(chat_id, KEY_LAST_USER_AT, str(received_at))
            _set_setting(chat_id, KEY_LAST_INTERACTION_AT, str(received_at))
            try:
                _schedule_from_now(chat_id)
            except Exception as exc:
                print(f"Proactive reset error: {type(exc).__name__}", flush=True)


runner.handle_text_with_memory = handle_text_with_proactive_reset
runner.bot.handle_text = handle_text_with_proactive_reset


_original_post_init = runner.post_init


async def post_init_with_proactive(application) -> None:
    await _original_post_init(application)
    application.create_task(proactive_loop(application))
    chat_id = _owner_chat_id()
    if chat_id is not None and not _setting(chat_id, KEY_NEXT_AT):
        _schedule_from_now(chat_id)
    print(
        "Proactive messaging enabled: interval=20m-3h, daily_max=unlimited, quiet=00:30-08:30 Asia/Shanghai; persistent_time_awareness=on",
        flush=True,
    )


runner.post_init = post_init_with_proactive


if __name__ == "__main__":
    runner.main()
