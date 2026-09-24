"""Explicit, recurring dates the owner asked the companion to remember."""

import json
import re
from datetime import date

KEY = "companion_milestones_v1"
LAST_SENT_KEY = "companion_milestone_last_sent_v1"
MAX_DATES = 20


def load(raw):
    try:
        items = json.loads(raw) if raw else []
        if not isinstance(items, list):
            return []
        return [x for x in items[:MAX_DATES] if isinstance(x, dict)
                and type(x.get("month")) is int and type(x.get("day")) is int
                and isinstance(x.get("name"), str) and len(x["name"]) <= 32
                and _valid(x["month"], x["day"])]
    except (ValueError, TypeError):
        return []


def _valid(month, day):
    try:
        date(2024, month, day)  # Leap day remains valid and fires in leap years.
        return True
    except ValueError:
        return False


def due(raw, today):
    return [x for x in load(raw) if x["month"] == today.month and x["day"] == today.day]


def handle(text, *, get_setting, set_setting, chat_id):
    """Return a user-facing acknowledgement for explicit date management only."""
    value = text.strip()
    if re.fullmatch(r"(?:查看|看看|列出)(?:我的|我们的)?(?:重要日子|纪念日|生日)(?:有哪些)?[？?]?", value):
        items = load(get_setting(chat_id, KEY))
        if not items:
            return "还没有记下重要日子。告诉我日期和名字，我会记着。"
        return "我记得这些：\n" + "\n".join(
            f"#{i}  {x['month']}月{x['day']}日  {x['name']}" for i, x in enumerate(items, 1))

    match = re.fullmatch(r"(?:删除|取消|忘掉)(?:重要日子|纪念日)?\s*#(\d+)[。.!！]?", value)
    if match:
        items = load(get_setting(chat_id, KEY))
        index = int(match.group(1)) - 1
        if index < 0 or index >= len(items):
            return "没找到这个编号。说‘查看重要日子’，我把记下的给你看。"
        removed = items.pop(index)
        set_setting(chat_id, KEY, json.dumps(items, ensure_ascii=False))
        return f"好，{removed['name']}这个日子我不再提醒。"

    birthday = re.search(r"(?:我的生日|我生日)\s*(?:是|在|：|:)\s*(\d{1,2})月(\d{1,2})日?", value)
    reminder = re.search(r"(?:记住|记下|别忘了)[，,:：\s]*(\d{1,2})月(\d{1,2})日\s*(?:是|：|:|，)?\s*(.{1,32})", value)
    if birthday:
        month, day, name = int(birthday.group(1)), int(birthday.group(2)), "你的生日"
    elif reminder:
        month, day = int(reminder.group(1)), int(reminder.group(2))
        name = reminder.group(3).strip(" 。！!，,：:")
    else:
        return None
    if not _valid(month, day) or not name:
        return "这个日期我没看明白。按‘记住9月24日是我们的纪念日’告诉我，好吗？"
    items = load(get_setting(chat_id, KEY))
    items = [x for x in items if x["name"] != name]
    if len(items) >= MAX_DATES:
        return "我已经记了20个日子。可以先说‘查看重要日子’，删掉一个再告诉我。"
    items.append({"month": month, "day": day, "name": name})
    set_setting(chat_id, KEY, json.dumps(items, ensure_ascii=False))
    return f"记住了。每年{month}月{day}日，{name}。"
