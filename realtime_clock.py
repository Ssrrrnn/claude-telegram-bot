"""Request-scoped Beijing clock; factual clock replies never depend on an LLM."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
WEEKDAYS = "一二三四五六日"


def now_local() -> datetime:
    return datetime.now(BEIJING)


def latest_user_text(messages) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str) and content.startswith("【生成校正，不是用户的新消息】"):
                continue
            return content if isinstance(content, str) else ""
    return ""


CLOCK_TOKEN = "[[LIVE_CLOCK]]"


def clock_reply_instruction(kind: str | None) -> str:
    if kind is None:
        return ""
    return (
        "\n【本轮时间回答的表达】\n"
        f"用户在问当前{ {'time': '钟点', 'date': '日期', 'weekday': '星期', 'period': '时段'}[kind] }。"
        f"把唯一的时间事实写为 {CLOCK_TOKEN}，后台会在发送前填入真实时间。"
        "围绕这个占位符，结合最近聊天和原有恋人人设，自然说一两句。"
        "不使用‘现在是北京时间’‘按北京时间’等播报开头，不解释系统或占位符。"
        "不另写具体钟点、日期或星期，不重复报时，不编造安排或催睡。"
        "可以接上刚才的话题，允许轻微逗她或亲近，但不要每次强行撩、反问或加固定情话。"
        "占位符必须完整且只出现一次。"
    )


def _spoken_number(value: int) -> str:
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    tens, units = divmod(value, 10)
    return (digits[tens] if tens > 1 else "") + "十" + (digits[units] if units else "")


def render_clock_reply(answer: str, kind: str) -> str:
    # Sample AFTER generation so slow requests or a minute/day rollover do
    # not leave the actual fact stale. Only the surrounding prose is generated.
    now = now_local()
    hour = now.hour % 12 or 12
    minutes = "整" if not now.minute else ("零" if now.minute < 10 else "") + _spoken_number(now.minute) + "分"
    hour_text = "两" if hour == 2 else _spoken_number(hour)
    value = f"{part_of_day(now)}{hour_text}点{minutes}"
    if kind == "date":
        value = f"{now.month}月{now.day}日"
    elif kind == "weekday":
        value = f"星期{WEEKDAYS[now.weekday()]}"
    elif kind == "period":
        value = part_of_day(now)
    rest = answer.replace(CLOCK_TOKEN, "")
    other_fact = re.search(
        r"[0-9零〇一二两三四五六七八九十]{1,4}(?:[:：][0-9]{1,2}|点|时|年|月|日|号)|"
        r"(?:星期|礼拜|周)[一二三四五六日天]|北京时间|UTC|Asia/Shanghai|"
        r"无法.{0,6}(?:时间|时钟)|不知道.{0,6}(?:几点|时间)", rest,
    )
    if answer.count(CLOCK_TOKEN) != 1 or other_fact or "[[" in rest or "]]" in rest:
        return value + "。"
    return answer.replace(CLOCK_TOKEN, value)


def clock_question(text: str) -> str | None:
    # Full-match only: appointments, quotations, foreign zones, and mixed
    # requests must retain their meaning and go through normal generation.
    text = re.sub(r"[\s，,。.!！?？~～]", "", text).lower()
    prefix = r"(?:(?:你)?(?:知道|知不知道|还知道|看看)|告诉我|报一下|看一下|看下|那|所以|到底|请问)*"
    suffix = r"(?:了|吗|么|呢|啊|呀|吧|没|没有)*"
    queries = {
        "time": r"(?:(?:现在|此刻|当前)(?:的)?|北京时间|北京现在|现在北京时间)?(?:都|已经)?(?:是)?(?:几点(?:钟)?(?:几分)?|什么时间|时间(?:是多少)?)",
        "date": r"(?:今天|现在)(?:是)?(?:几月几[号日]|几[号日]|什么日期|哪一天|日期)",
        "weekday": r"(?:今天|现在)(?:是)?(?:星期几|周几|礼拜几)",
        "period": r"现在(?:是)?(?:白天还是晚上|晚上还是白天|上午还是下午|早上还是晚上)",
    }
    for kind, query in queries.items():
        if re.fullmatch(prefix + query + suffix, text):
            if kind == "time" and not re.search(r"现在|此刻|当前|北京|几点(?:钟)?了", text):
                return None
            return kind
    if re.fullmatch(prefix + r"北京时间" + suffix, text):
        return "time"
    if re.fullmatch(r"(?:what(?:'s|is)thetime(?:now)?|whattimeisit(?:now)?)", text):
        return "time"
    return None


def part_of_day(now: datetime) -> str:
    hour = now.hour
    if hour < 6:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    return "晚上"


def request_clock_block() -> str:
    now = now_local()
    return (
        "【本次请求的实时时钟｜时间事实以此为准】\n"
        f"北京时间（Asia/Shanghai，UTC+08:00）：{now:%Y-%m-%d %H:%M:%S}；"
        f"星期{WEEKDAYS[now.weekday()]}；{part_of_day(now)}。\n"
        "这是本次请求现场读取的时间，不是启动时间、历史消息时间或角色设定。"
        "历史回复即使报过别的时间也不代表现在；禁止沿用。"
        "用户问此刻时间时准确使用这里的时分，问日期或星期时使用这里的日期或星期。"
        "没有其他时区要求时按北京时间回答，不要声称不知道当前时间或无法获取时间。"
        "未来安排、过去事件和引用中的钟点保持原义。普通聊天不必主动报时。"
    )
