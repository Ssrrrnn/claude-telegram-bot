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
            return content if isinstance(content, str) else ""
    return ""


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


def direct_clock_reply(messages) -> str | None:
    kind = clock_question(latest_user_text(messages))
    if kind is None:
        return None
    now = now_local()  # Read on every request, never at import or startup.
    if kind == "time":
        return f"现在是北京时间 {now:%H:%M}。"
    if kind == "date":
        return f"按北京时间，今天是{now.year}年{now.month}月{now.day}日，星期{WEEKDAYS[now.weekday()]}。"
    if kind == "weekday":
        return f"按北京时间，今天星期{WEEKDAYS[now.weekday()]}，{now.month}月{now.day}日。"
    return f"现在是北京时间 {now:%H:%M}，{part_of_day(now)}。"


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
