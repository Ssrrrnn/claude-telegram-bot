"""Small, deterministic policy for the companion account's QQ display status."""

from datetime import datetime
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
ROMANTIC = (10, 1051)  # 恋爱中
LOVE = (10, 2006)      # 爱你
LATE = (10, 1032)      # 熬夜中
LOVE_SECONDS = 75 * 60
RECENT_CHAT_SECONDS = 45 * 60


def affectionate(text):
    if not isinstance(text, str) or any(word in text for word in (
        "不爱你", "不喜欢你", "别爱我", "讨厌你", "不想你", "谁爱你",
    )):
        return False
    return any(word in text for word in ("我爱你", "只喜欢你", "好喜欢你", "想你了"))


def choose(now, *, last_user_at=0, love_until=0):
    """Return a QQ online state and its label, using Beijing time only."""
    if now < love_until:
        return LOVE, "爱你"
    hour = datetime.fromtimestamp(now, BEIJING).hour
    if hour < 6 and 0 <= now - last_user_at <= RECENT_CHAT_SECONDS:
        return LATE, "熬夜中"
    return ROMANTIC, "恋爱中"
