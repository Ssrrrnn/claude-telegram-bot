"""Public-safe QQ profile signatures chosen by the companion."""

import re

ENABLED_KEY = "qq_signature_auto_v1"
LAST_TEXT_KEY = "qq_signature_last_text_v1"
NEXT_AT_KEY = "qq_signature_next_at_v1"
LAST_AT_KEY = "qq_signature_last_at_v1"
MAX_CHARS = 45


def command(text):
    value = text.strip().rstrip("。！! ")
    if value in ("个性签名状态", "查看个性签名"):
        return ("status", "")
    if value in ("开启自动签名", "关闭自动签名"):
        return ("on" if value.startswith("开启") else "off", "")
    if value in ("换个性签名", "换个签名", "自己换个签名"):
        return ("generate", "")
    match = re.fullmatch(r"(?:设置|换|改)(?:QQ)?(?:个性)?签名[：:]\s*(.{1,80})", value)
    return ("set", match.group(1).strip()) if match else None


def valid(content):
    return (isinstance(content, str) and 2 <= len(content) <= MAX_CHARS
            and not any(mark in content for mark in ("\n", "\r", "<", ">", "...", "……", "…", "http")))


async def generate(bot, chat_id, previous=""):
    import httpx

    messages = [
        {"role": "system", "content": (
            "你要给自己的QQ账号写一条公开可见的个性签名。"
            "只写5到28字的中文短句，带一点克制的浪漫和个人心情，像真人写的。"
            "你不知道用户此刻在做什么；不得引用私聊原文、姓名、位置、职业、账号、"
            "私密关系细节，不得假装现实见过面。不要标签、链接、省略号、标题或解释。")},
        {"role": "user", "content": "写一条和旧签名不同的短句。旧签名仅用于避重：" + previous[:MAX_CHARS]},
    ]
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            f"{bot.LLMHUB_BASE_URL}/chat/completions", headers=bot.api_headers(),
            json={"model": bot.current_model(chat_id), "messages": messages, "stream": False})
    response.raise_for_status()
    content = bot.extract_text(response.json()).strip().strip('"“”')
    if not valid(content) or content == previous:
        raise ValueError("Invalid or repeated QQ signature")
    return content
