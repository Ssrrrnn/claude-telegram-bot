import re

import runner


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
        # Emoji-only lines remain a standalone bubble.
        if runner._looks_like_emoji_only(line):
            bubbles.append(line)
            continue

        # Split each line into complete sentences. Unlike the old behavior,
        # fragments are NEVER merged across lines.
        parts = re.split(r"(?<=[。！？!?…])\s*|(?<=[.!?])\s+", line)
        parts = [part.strip() for part in parts if part and part.strip()]
        if not parts:
            parts = [line]

        for part in parts:
            # If Claude intentionally put a short fragment on its own line,
            # respect that choice and send it as its own Telegram message.
            bubbles.append(part)

    # Safety-split unexpectedly long single bubbles, but do not merge normal ones.
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


# bubble_safe_reply looks up _split_bubbles from runner module globals at call time.
runner._split_bubbles = strict_split_bubbles


if __name__ == "__main__":
    runner.main()
