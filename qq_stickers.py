"""Small, owner-curated QQ sticker library for private chat replies."""

import json

SETTING_KEY = "qq_market_stickers_v1"
LAST_SENT_KEY = "qq_market_sticker_last_sent_v1"
MAX_STICKERS = 16
SPONTANEOUS_COOLDOWN = 4 * 60 * 60


def normalize(data):
    if not isinstance(data, dict):
        return None
    package_id, emoji_id, key = data.get("emoji_package_id"), data.get("emoji_id"), data.get("key")
    if isinstance(package_id, bool) or not isinstance(package_id, int) or not 0 <= package_id < 2**31:
        return None
    if not isinstance(emoji_id, str) or not 0 < len(emoji_id) <= 128:
        return None
    if not isinstance(key, str) or not 0 < len(key) <= 1024:
        return None
    summary = data.get("summary")
    return {"emoji_package_id": package_id, "emoji_id": emoji_id, "key": key,
            "summary": str(summary)[:80] if summary else "[商城表情]"}


def load(raw):
    try:
        value = json.loads(raw) if raw else []
        return [sticker for item in value[:MAX_STICKERS] if (sticker := normalize(item))] if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def remember(raw, event):
    items = load(raw)
    segments = event.get("message")
    if not isinstance(segments, list):
        return items, False
    changed = False
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("type") != "mface":
            continue
        sticker = normalize(segment.get("data"))
        if not sticker:
            continue
        previous = [item for item in items if (item["emoji_package_id"], item["emoji_id"]) !=
                    (sticker["emoji_package_id"], sticker["emoji_id"])]
        if len(previous) != len(items) or len(items) >= MAX_STICKERS:
            # Reusing a sticker updates recency, without growing the stored collection.
            items = previous + [sticker]
            items = items[-MAX_STICKERS:]
        else:
            items.append(sticker)
        changed = True
    return items, changed


def explicit_request(text):
    if not isinstance(text, str) or "表情包" not in text:
        return False
    if any(term in text for term in ("别发", "不要发", "不用发", "不许发")):
        return False
    return any(term in text for term in ("发个", "发一个", "发张", "发一张", "来个", "来一个",
                                        "来张", "来一张", "给我发", "丢个", "丢一张"))


def fitting_stickers(items, reply):
    """Only accompany an answer when the saved sticker's label fits its mood."""
    themes = (
        (("抱", "贴贴", "心疼", "想你", "亲"), ("抱", "贴", "爱", "亲")),
        (("哈哈", "好笑", "逗", "开心"), ("笑", "乐", "开心", "哈哈")),
        (("喜欢", "脸红", "害羞"), ("爱", "害羞", "脸红", "喜欢")),
    )
    for words, labels in themes:
        if any(word in reply for word in words):
            matched = [item for item in items if any(label in item["summary"] for label in labels)]
            if matched:
                return matched
    return []
