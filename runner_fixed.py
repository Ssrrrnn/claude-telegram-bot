import asyncio

from telegram.constants import ParseMode

import runner_20m as base

# runner_20m already installs memory, proactive messaging, time-awareness,
# ElevenLabs, explicit voice requests, and text sanitization. Keep that entire
# stack, but bypass the layered safe_reply wrappers for ordinary text messages.
_previous_safe_reply = base.app.runner.bot.safe_reply


async def direct_safe_reply(update, text: str) -> None:
    if not update.message:
        return

    user_text = update.message.text or ""

    # Explicit voice requests must keep using the voice pipeline from runner_20m.
    if base._explicit_voice_request(user_text):
        await _previous_safe_reply(update, text)
        return

    cleaned = base._sanitize_text_chat_output(text)
    final_text = (cleaned or text or "…").strip() or "…"
    bubbles = base.app.strict_split_bubbles(final_text)
    bubbles = [bubble.strip() for bubble in bubbles if bubble and bubble.strip()]
    if not bubbles:
        bubbles = [final_text]

    sent = 0
    for index, bubble in enumerate(bubbles):
        if index:
            await asyncio.sleep(0.22)
        try:
            await update.message.reply_text(bubble, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(bubble)
        sent += 1

    print(
        f"Direct text reply sent: chat_id={update.effective_chat.id}, bubbles={sent}, chars={len(final_text)}",
        flush=True,
    )


# bot.generate_and_send resolves safe_reply from the bot module at runtime, so
# replacing this single function makes ordinary text delivery deterministic.
base.app.runner.bot.safe_reply = direct_safe_reply


if __name__ == "__main__":
    base.app.runner.main()
