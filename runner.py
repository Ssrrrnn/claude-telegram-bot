import asyncio
import re
from collections import defaultdict

from telegram.constants import ParseMode

import bot

# Default to chat-bubble mode instead of live-edit streaming.
# Users can still run /stream on if they want the old streaming style.
bot.stream_preferences = defaultdict(lambda: False)

_original_safe_reply = bot.safe_reply


def _split_bubbles(text: str) -> list[str]:
    """Turn a prose reply into natural Telegram chat bubbles."""
    if not text:
        return ["…"]

    # Keep technical / structured output intact.
    structured_prefixes = (
        "API 请求失败",
        "读取模型列表失败",
        "可用模型：",
        "当前启用的世界书",
        "世界书不存在",
    )
    if text.startswith(structured_prefixes) or "```" in text:
        return [text]

    # The model often uses hard line breaks for prose. In Telegram those look
    # like a wall of text, so flatten prose first and let sentence punctuation
    # determine bubble boundaries.
    normalized = re.sub(r"\s*\n+\s*", " ", text).strip()
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    if not normalized:
        return ["…"]

    # Chinese punctuation splits without spaces; English punctuation normally
    # splits when followed by whitespace.
    sentences = re.split(r"(?<=[。！？!?])\s*|(?<=[.!?])\s+", normalized)
    sentences = [s.strip() for s in sentences if s and s.strip()]
    if not sentences:
        sentences = [normalized]

    bubbles: list[str] = []
    current: list[str] = []
    current_len = 0

    # Usually 1-2 sentences per bubble, with a soft character cap. This feels
    # much more like normal messaging than one giant paragraph.
    for sentence in sentences:
        sentence_len = len(sentence)
        if current and (len(current) >= 2 or current_len + sentence_len > 260):
            bubbles.append(" ".join(current).strip())
            current = []
            current_len = 0

        # Very long unpunctuated sentence: split gently by commas if possible.
        if sentence_len > 520 and not current:
            pieces = re.split(r"(?<=[，,；;])\s*", sentence)
            piece_group: list[str] = []
            piece_len = 0
            for piece in pieces:
                if not piece:
                    continue
                if piece_group and piece_len + len(piece) > 300:
                    bubbles.append("".join(piece_group).strip())
                    piece_group = []
                    piece_len = 0
                piece_group.append(piece)
                piece_len += len(piece)
            if piece_group:
                bubbles.append("".join(piece_group).strip())
            continue

        current.append(sentence)
        current_len += sentence_len

    if current:
        bubbles.append(" ".join(current).strip())

    bubbles = [b for b in bubbles if b]
    if not bubbles:
        return [normalized]

    # Avoid Telegram spam on an unexpectedly long answer. Keep the first
    # bubbles conversational and merge any excess into the last bubble.
    if len(bubbles) > 8:
        bubbles = bubbles[:7] + [" ".join(bubbles[7:])]

    return bubbles


async def bubble_safe_reply(update, text: str) -> None:
    if not update.message:
        return

    bubbles = _split_bubbles(text)
    if len(bubbles) == 1 and bubbles[0] == text:
        await _original_safe_reply(update, text)
        return

    for index, bubble in enumerate(bubbles):
        if index:
            await asyncio.sleep(0.22)
        try:
            await update.message.reply_text(bubble, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(bubble)


# generate_and_send resolves safe_reply from bot's module globals at runtime,
# so replacing it here changes normal generated replies without rewriting bot.py.
bot.safe_reply = bubble_safe_reply


if __name__ == "__main__":
    bot.main()
