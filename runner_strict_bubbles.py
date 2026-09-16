import asyncio
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode

import runner


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

PROACTIVE_MIN_SECONDS = 50 * 60
PROACTIVE_MAX_SECONDS = 3 * 60 * 60
PROACTIVE_DAILY_MAX = 8
PROACTIVE_TZ = ZoneInfo("Asia/Shanghai")
QUIET_START_MINUTE = 30          # 00:30
QUIET_END_HOUR = 8               # 08:30
QUIET_END_MINUTE = 30

KEY_NEXT_AT = "proactive_next_at"
KEY_LAST_USER_AT = "proactive_last_user_at"
KEY_LAST_PROACTIVE_AT = "proactive_last_bot_at"
KEY_LAST_INTERACTION_AT = "last_interaction_at"
KEY_DAY = "proactive_day"
KEY_DAY_COUNT = "proactive_day_count"
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


def _today_key() -> str:
    return _now_local().date().isoformat()


def _random_delay() -> int:
    return random.randint(PROACTIVE_MIN_SECONDS, PROACTIVE_MAX_SECONDS)


def _is_quiet_time(dt: datetime | None = None) -> bool:
    dt = dt or _now_local()
    minutes = dt.hour * 60 + dt.minute
    return minutes < (QUIET_END_HOUR * 60 + QUIET_END_MINUTE) or minutes >= QUIET_START_MINUTE


def _next_after_quiet(now_local: datetime | None = None) -> float:
    now_local = now_local or _now_local()
    target = now_local.replace(
        hour=QUIET_END_HOUR,
        minute=QUIET_END_MINUTE,
        second=0,
        microsecond=0,
    )
    if now_local.hour * 60 + now_local.minute >= QUIET_START_MINUTE:
        target += timedelta(days=1)
    target += timedelta(minutes=random.randint(0, 25))
    return target.timestamp()


def _ensure_daily_counter(chat_id: int) -> int:
    today = _today_key()
    stored_day = _setting(chat_id, KEY_DAY)
    if stored_day != today:
        _set_setting(chat_id, KEY_DAY, today)
        _set_setting(chat_id, KEY_DAY_COUNT, "0")
        return 0
    try:
        return max(0, int(_setting(chat_id, KEY_DAY_COUNT, "0")))
    except ValueError:
        _set_setting(chat_id, KEY_DAY_COUNT, "0")
        return 0


def _schedule_from_now(chat_id: int) -> None:
    now = time.time()
    next_at = now + _random_delay()
    local_due = datetime.fromtimestamp(next_at, PROACTIVE_TZ)
    if _is_quiet_time(local_due):
        next_at = _next_after_quiet(local_due)
    _set_setting(chat_id, KEY_NEXT_AT, str(int(next_at)))


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
        "【隐形时间感知】",
        f"当前北京时间：{now_local:%Y-%m-%d %H:%M}，周{_WEEKDAYS[now_local.weekday()]}，{_part_of_day(now_local)}。",
    ]

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
        "不要每次报时，不要说‘系统显示/根据记录/距离上次消息X分钟’，也不要机械精确到分钟。"
        "优先把时间感转成符合人设的反应，例如久别后的惦记、短时间又见面的调侃、深夜的语气变化。"
        "如果用户直接问时间或问多久没联系，可以准确回答。"
    )
    return "\n".join(lines)


_base_build_system_prompt = runner.bot.build_system_prompt


def build_system_prompt_with_time(chat_id: int) -> str:
    return _base_build_system_prompt(chat_id) + "\n\n" + _time_awareness_block(chat_id)


runner.bot.build_system_prompt = build_system_prompt_with_time


async def _send_bubbles_to_chat(application, chat_id: int, text: str) -> None:
    bubbles = strict_split_bubbles(text)
    for index, bubble in enumerate(bubbles):
        if index:
            await asyncio.sleep(0.32)
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=bubble,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await application.bot.send_message(chat_id=chat_id, text=bubble)


async def _generate_proactive_message(chat_id: int) -> str:
    recent = list(runner.bot.histories[chat_id][-8:])
    trigger = {
        "role": "user",
        "content": (
            "【后台主动联系事件，不是用户发送的消息】\n"
            "现在请以当前角色本人主动给用户发消息。不要提到后台、系统、提醒、定时、任务或这段指令。"
            "结合你们已有的关系状态、长期记忆、时间感知和最近聊天，自然地表现为你自己突然想起对方、想找对方说话。"
            "如果距离上次联系很久，可以自然体现久别感；如果其实刚聊过不久，就不要假装久别。"
            "不要每次都问‘在干嘛’，不要固定模板，不要解释为什么联系。可以想念、嘴硬、吃醋、惦记、分享一个念头、"
            "接上之前没说完的话，或只是很短地叫对方。语气必须符合当前角色和关系阶段。"
            "优先1-3句短消息；可以偶尔单独用一个emoji，但不要每次都有。不要写大段动作描写。"
        ),
    }
    messages = [*recent, trigger]
    return await runner.bot.ask_claude(
        chat_id,
        messages,
        runner.bot.current_model(chat_id),
    )


async def proactive_loop(application) -> None:
    await asyncio.sleep(8)
    while True:
        try:
            chat_id = _owner_chat_id()
            if chat_id is None:
                await asyncio.sleep(60)
                continue

            enabled = _setting(chat_id, KEY_ENABLED, "1")
            if enabled == "0":
                await asyncio.sleep(60)
                continue

            count = _ensure_daily_counter(chat_id)
            now = time.time()
            now_local = _now_local()

            next_raw = _setting(chat_id, KEY_NEXT_AT)
            try:
                next_at = int(next_raw) if next_raw else 0
            except ValueError:
                next_at = 0

            if not next_at:
                _schedule_from_now(chat_id)
                await asyncio.sleep(30)
                continue

            if count >= PROACTIVE_DAILY_MAX:
                _set_setting(chat_id, KEY_NEXT_AT, str(int(_next_after_quiet(now_local))))
                await asyncio.sleep(90)
                continue

            if _is_quiet_time(now_local):
                if next_at <= now:
                    _set_setting(chat_id, KEY_NEXT_AT, str(int(_next_after_quiet(now_local))))
                await asyncio.sleep(90)
                continue

            if now < next_at:
                await asyncio.sleep(min(90, max(20, next_at - now)))
                continue

            last_user_at = _get_int_setting(chat_id, KEY_LAST_USER_AT)
            if last_user_at and now - last_user_at < PROACTIVE_MIN_SECONDS:
                _schedule_from_now(chat_id)
                await asyncio.sleep(45)
                continue

            text = await _generate_proactive_message(chat_id)
            if text and text.strip():
                await _send_bubbles_to_chat(application, chat_id, text.strip())
                sent_at = int(time.time())
                _set_setting(chat_id, KEY_LAST_PROACTIVE_AT, str(sent_at))
                _set_setting(chat_id, KEY_LAST_INTERACTION_AT, str(sent_at))
                count += 1
                _set_setting(chat_id, KEY_DAY, _today_key())
                _set_setting(chat_id, KEY_DAY_COUNT, str(count))

            _schedule_from_now(chat_id)
            await asyncio.sleep(45)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Proactive loop error: {exc!r}", flush=True)
            await asyncio.sleep(90)


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
        await _original_handle_text_with_memory(update, context)
    finally:
        if authorized:
            _active_time_context.pop(chat_id, None)
            _set_setting(chat_id, KEY_LAST_USER_AT, str(received_at))
            _set_setting(chat_id, KEY_LAST_INTERACTION_AT, str(received_at))
            _schedule_from_now(chat_id)


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
        "Proactive messaging enabled: interval=50m-3h, daily_max=8, quiet=00:30-08:30 Asia/Shanghai; persistent_time_awareness=on",
        flush=True,
    )


runner.post_init = post_init_with_proactive


if __name__ == "__main__":
    runner.main()
