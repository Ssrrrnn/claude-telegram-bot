"""Small, owner-curated QQ sticker library for private chat replies."""

import json

SETTING_KEY = "qq_market_stickers_v1"
LAST_SENT_KEY = "qq_market_sticker_last_sent_v1"
MARKET_DISCOVERY_AT_KEY = "qq_market_sticker_discovery_at_v1"
MAX_STICKERS = 16
SPONTANEOUS_COOLDOWN = 90 * 60


def normalize(data):
    if not isinstance(data, dict):
        return None
    if data.get("kind") == "image":
        file = data.get("file")
        # NapCat's received file name is an opaque media reference; never persist
        # an arbitrary URL, base64 payload, or filesystem path from a message.
        if not isinstance(file, str) or not 0 < len(file) <= 256 or any(
            token in file for token in ("/", "\\", ":", "..")
        ):
            return None
        message_id = data.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return None
        summary = data.get("summary")
        sub_type = data.get("sub_type")
        return {"kind": "image", "file": file, "message_id": message_id,
                "summary": str(summary)[:80] if summary else "[图片表情]",
                "sub_type": sub_type if type(sub_type) is int and 0 <= sub_type <= 100 else 0}
    package_id, emoji_id, key = data.get("emoji_package_id"), data.get("emoji_id"), data.get("key")
    if isinstance(package_id, str) and package_id.isascii() and package_id.isdecimal():
        package_id = int(package_id)
    if isinstance(package_id, bool) or not isinstance(package_id, int) or not 0 <= package_id < 2**31:
        return None
    if isinstance(emoji_id, int) and not isinstance(emoji_id, bool):
        emoji_id = str(emoji_id)
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
        if not isinstance(segment, dict) or segment.get("type") not in ("mface", "image"):
            continue
        data = segment.get("data")
        if segment["type"] == "image" and isinstance(data, dict):
            # A received market face can arrive as an image with market-face metadata.
            sticker = normalize(data) if all(k in data for k in ("emoji_package_id", "emoji_id", "key")) else None
            if not sticker:
                sticker = normalize({"kind": "image", "file": data.get("file"),
                                     "message_id": event.get("message_id"),
                                     "summary": data.get("summary"), "sub_type": data.get("sub_type")})
        else:
            sticker = normalize(data)
        if not sticker:
            continue
        identity = (sticker.get("emoji_package_id"), sticker.get("emoji_id")) if "emoji_id" in sticker else sticker["file"]
        previous = [item for item in items if (
            (item.get("emoji_package_id"), item.get("emoji_id")) if "emoji_id" in item else item["file"]
        ) != identity]
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
                                        "来张", "来一张", "给我发", "丢个", "丢一张",
                                        "发表情包", "表情包发", "丢表情包", "用表情包")) and not any(
                                            term in text for term in ("怎么还没", "为什么没", "能不能", "会不会"))


def fitting_stickers(items, reply):
    """Only accompany an answer when the saved sticker's label fits its mood."""
    themes = (
        (("抱", "贴贴", "心疼", "想你", "亲"), ("抱", "贴", "爱", "亲")),
        (("哈哈", "好笑", "逗", "开心"), ("笑", "乐", "开心", "哈哈")),
        (("喜欢", "脸红", "害羞"), ("爱", "害羞", "脸红", "喜欢")),
    )
    for words, labels in themes:
        if any(word in reply for word in words):
            matched = [item for item in items if item.get("kind") != "image" and
                       any(label in item["summary"] for label in labels)]
            if matched:
                return matched
            # PicSubType.KCUSTOM (1) is QQ's custom expression. Ordinary
            # photos (0) must never be sent as unsolicited stickers.
            return [item for item in items if item.get("kind") == "image" and
                    item.get("sub_type") == 1]
    return []
