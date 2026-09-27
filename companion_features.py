"""Owner controlled companion routines shared by Telegram and QQ."""

import json
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import continuity

LOCAL = ZoneInfo("Asia/Shanghai")
STYLE_KEY = "companion_style_v1"
AUTO_DIARY_KEY = "companion_auto_diary_v1"
AUTO_DIARY_DAY_KEY = "companion_auto_diary_day_v1"
STYLES = {"自然": "自然", "撒娇": "撒娇", "逗我": "调侃", "调侃": "调侃", "安慰": "安慰", "暧昧": "暧昧"}


def style_block(db, chat_id):
    try:
        value = json.loads(db.get_setting(chat_id, STYLE_KEY) or "{}")
        if value.get("until", 0) > time.time() and value.get("style") in STYLES.values():
            return ("【用户当前选择的聊天节奏】接下来偏向" + value["style"] +
                    "，自然地接当前话题，不重复固定句式；用户不舒服时立即放软并停止该节奏。")
    except Exception:
        pass
    return ("【聊天节奏】随着当前语境自然变化：有事先接事，疲惫先安慰，"
            "亲近时偶尔撒娇或调侃；不要每轮套同一种反应，不要凭空制造嫉妒或成人场景。")


def style_command(db, chat_id, text):
    value = text.strip().rstrip("。！! ")
    if value in ("别撒娇了", "别逗我了", "停下这个模式", "恢复自然模式"):
        db.set_setting(chat_id, STYLE_KEY, "{}")
        return "好，听你的。我就照现在这样陪着你。"
    match = re.fullmatch(r"(?:切到|换成|开启|用)(自然|撒娇|逗我|调侃|安慰|暧昧)模式", value)
    if not match:
        return None
    style = STYLES[match.group(1)]
    db.set_setting(chat_id, STYLE_KEY, json.dumps({"style": style,
                                                    "until": time.time() + 2 * 3600}))
    return "好，听你的。" if style != "自然" else "好，跟着我们现在的感觉聊。"


def diary_publish_id(text):
    value = text.strip().rstrip("。！! ")
    match = re.fullmatch(r"(?:发|发表)(?:我们的)?日记\s*#?(\d+)\s*到(?:QQ)?空间", value)
    if not match:
        match = re.fullmatch(r"(?:把)?日记\s*#?(\d+)\s*(?:发|发表|放)(?:到)?(?:QQ)?空间", value)
    return int(match.group(1)) if match else None


_NUMBERS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _hour(value):
    if value.isdigit():
        return int(value)
    if value in _NUMBERS:
        return _NUMBERS[value]
    if value.startswith("十") and len(value) == 2:
        return 10 + _NUMBERS.get(value[1], 100)
    if len(value) == 2 and value.endswith("十"):
        return _NUMBERS.get(value[0], 100) * 10
    return 100


def followup_command(db, chat_id, text, message_id, *, now=None):
    """Schedule a one-off question when the owner explicitly asks to be asked."""
    value = text.strip()
    if re.fullmatch(r"(?:查看|看看)(?:待|我的)?回访", value):
        state = continuity.load(db, chat_id)
        items = [x for x in state["threads"] if x.get("status") == "open"
                 and x.get("category") == "requested"]
        return ("还没有约定要问你的事。" if not items else "记着这些：\n" + "\n".join(
            f"#{x['id']} {datetime.fromtimestamp(x['due_at'], LOCAL):%m月%d日 %H:%M} {x['title']}"
            for x in items))
    cancel = re.fullmatch(r"(?:取消|删除)回访\s*#?([a-f0-9]{12})", value)
    if cancel:
        found = False
        def change(state):
            nonlocal found
            for item in state["threads"]:
                if item.get("id") == cancel.group(1) and item.get("category") == "requested":
                    item["status"] = "cancelled"
                    found = True
            return state
        db.mutate_continuity(chat_id, change)
        return "好，这件事不再问了。" if found else "没找到这条回访。"
    if not re.search(r"(?:问我|再问我|来问我|记得问我|到时候问我)", value):
        return None
    now = now or datetime.now(LOCAL)
    relative = re.search(r"(?:过|再过)(\d{1,3})(分钟|小时)(?:后)?", value)
    timed = re.search(r"(?:(今天|明天|后天)\s*)?(上午|早上|下午|晚上|中午|凌晨)?\s*"
                      r"([零一二两三四五六七八九十\d]{1,3})[点时:]"
                      r"(?:(半)|([0-5]?\d)分?)?", value)
    if relative:
        amount = int(relative.group(1)) * (60 if relative.group(2) == "小时" else 1)
        if not 20 <= amount <= 7 * 24 * 60:
            return "约个20分钟到7天内的时间，我才好记住。"
        due = now + timedelta(minutes=amount)
    elif timed:
        hour = _hour(timed.group(3))
        period = timed.group(2) or ""
        if period in ("下午", "晚上") and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        minute = 30 if timed.group(4) else int(timed.group(5) or 0)
        if hour > 23 or minute > 59:
            return "这个时间我没看明白，按‘明天15:00问我面试怎么样’说一次，好吗？"
        day = {"今天": 0, "明天": 1, "后天": 2}.get(timed.group(1), 0)
        due = (now + timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not timed.group(1) and due <= now:
            due += timedelta(days=1)
        # "面试下午三点，结束后问我" means after the event, not at its start.
        if "结束后" in value or "回来后" in value:
            due += timedelta(minutes=90)
    else:
        return "想让我到时问你，就说‘明天15:00问我面试怎么样’。"
    if not 20 * 60 <= (due - now).total_seconds() <= 7 * 86400:
        return "这个时间太近或已经过去了，给我一个20分钟到7天内的时间。"
    # Don't store a whole private conversation as the prompt for a future ping.
    title = re.sub(r"(?:今天|明天|后天|上午|早上|下午|晚上|中午|凌晨|结束后|回来后)", "", value)
    title = re.sub(r"(?:过|再过)?\d{1,3}(?:分钟|小时)(?:后)?|[零一二两三四五六七八九十\d]{1,3}[点时:](?:半|[0-5]?\d分?)?", "", title)
    title = re.sub(r"(?:记得|到时候|再|来)?问我", "", title).strip("，。 ？?，,")[:80]
    title = title or "今天那件事怎么样了"
    state = continuity.schedule_requested(db, chat_id, message_id, title, due.timestamp())
    if state is None:
        return "这条已经记下了。"
    return f"记住了。{due:%m月%d日 %H:%M}我来问你「{title}」。"


async def diary_command(db, bot, chat_id, text, message_id):
    """Private diary commands; public posting has a separate QQ-only path."""
    value = text.strip().rstrip("。！! ")
    if value in ("开启自动日记", "关闭自动日记"):
        enabled = value.startswith("开启")
        db.set_setting(chat_id, AUTO_DIARY_KEY, '1' if enabled else '0')
        return '好，值得记住的日子我会写成私密日记。' if enabled else '好，自动写日记关掉了。'
    if value in ("查看双人日记", "看看双人日记", "查看日记", "看看日记"):
        rows = db.list_diary(chat_id)
        return ("还没有写过双人日记。" if not rows else "我们的日记：\n" + "\n".join(
            f"#{row['id']} {row['created_at'].astimezone(LOCAL):%m月%d日} {row['text'][:35]}"
            for row in rows))
    match = re.fullmatch(r"(?:看|查看|读|打开)日记\s*#?(\d+)", value)
    if match:
        row = db.get_diary(chat_id, int(match.group(1)))
        return row["text"] if row else "没找到这篇日记。"
    match = re.fullmatch(r"(?:删除|忘掉)日记\s*#?(\d+)", value)
    if match:
        return "这篇日记删掉了。" if db.delete_diary(chat_id, int(match.group(1))) else "没找到这篇日记。"
    if diary_publish_id(value) is not None:
        return "这篇日记是私密的。想公开它，请在 QQ 私聊里明确说‘发日记 #编号 到空间’。"
    match = re.fullmatch(r"(?:写(?:一篇|篇|个)?|记(?:一篇|篇)?)?(?:我们的|双人)?日记(?:[：:]\s*(.{1,200}))?", value)
    if not match or not re.search(r"写|记|日记", value):
        return None
    hint = match.group(1) or ""
    recent = db.load_recent_messages(chat_id, 12)
    if not recent and not hint:
        return "先陪我聊聊今天，再把值得记住的写下来。"
    content = await _write_diary(bot, chat_id, recent, hint)
    if not content:
        return "这一篇没写好，等我们再聊一会儿。"
    diary_id = db.save_diary(chat_id, message_id, content)
    return f"写进我们的私密日记 #{diary_id} 了。\n{content}"


async def _write_diary(bot, chat_id, recent, hint=""):
    answer = await bot.ask_claude(chat_id, [
        *recent[-12:],
        {"role": "user", "content": (
            "【后台私密双人日记写作；不是公开空间说说】根据确实出现过的对话，"
            "用我们的口吻写80到220字的一小段日记。表达具体情绪和关系进展；"
            "不要编造现实见面、天气或共同经历，不要泄露用户实名、账号或精确位置。"
            "只输出正文，不说自己已经存档。" + ("今天的主题：" + hint if hint else ""))},
    ], bot.current_model(chat_id))
    content = (answer or "").strip()
    return content if 30 <= len(content) <= 300 and "<" not in content and "http" not in content.lower() else None


async def auto_diary_tick(db, bot, chat_id, *, now=None):
    """At most one private page for a day with a real exchange."""
    now = now or datetime.now(LOCAL)
    if now.hour < 21 or db.get_setting(chat_id, AUTO_DIARY_KEY) == '0':
        return
    day = now.date().isoformat()
    if db.get_setting(chat_id, AUTO_DIARY_DAY_KEY) == day:
        return
    recent = db.diary_context_since(chat_id, now.replace(hour=0, minute=0, second=0, microsecond=0))
    if sum(item['role'] == 'user' for item in recent) < 2:
        return
    # Claim before the model call so concurrent loops or restarts do not
    # generate and save two pages for the same day.
    db.set_setting(chat_id, AUTO_DIARY_DAY_KEY, day)
    content = await _write_diary(bot, chat_id, recent)
    if content:
        db.save_diary(chat_id, -int(now.strftime('%Y%m%d')), content)


def action_status(db, chat_id, text):
    if text.strip().rstrip("？?。！! ") not in ("查看动作记录", "刚才做了什么", "查看执行记录"):
        return None
    rows = db.recent_actions(chat_id)
    labels = {"success": "已确认成功", "attempting": "结果待确认", "uncertain": "未确认成功",
              "failed": "失败"}
    if not rows:
        return "还没有记录到实际操作。"
    return "最近的实际操作：\n" + "\n".join(
        f"{row['created_at'].astimezone(LOCAL):%m月%d日 %H:%M} {row['kind']}："
        f"{labels.get(row['status'], '结果待确认')}"
        + (f"（帖子 ID {row['remote_id']}）" if row['kind'] == "空间说说"
           and row['status'] == "success" and row.get('remote_id') else "")
        for row in rows)


def feature_help(text):
    if text.strip().rstrip("？?。！! ") not in ("查看新功能", "新功能说明"):
        return None
    return ("你可以这样跟我说：\n"
            "明天15:00问我面试怎么样｜查看回访｜取消回访 #编号\n"
            "记住：你的偏好｜查看记忆｜纠正记忆 #编号：正确内容｜忘掉记忆 #编号\n"
            "写一篇双人日记｜查看日记｜看日记 #编号｜删除日记 #编号｜关闭自动日记\n"
            "切到撒娇模式｜切到安慰模式｜恢复自然模式\n"
            "查看动作记录。QQ 里还能说‘换个性签名’‘关闭自动签名’；"
            "日记只有你明确说‘发日记 #编号 到空间’才会公开。")
