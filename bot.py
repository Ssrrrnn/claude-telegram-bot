import asyncio
import os
from collections import defaultdict

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
LLMHUB_API_KEY = os.environ.get("LLMHUB_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()
LLMHUB_BASE_URL = os.environ.get("LLMHUB_BASE_URL", "https://llmhub.uk/v1").rstrip("/")
BOT_ACCESS_CODE = os.environ.get("BOT_ACCESS_CODE", "").strip()

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "你是用户的私人 AI 助手。默认使用中文回答。回答准确、自然、有逻辑。",
)

MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "30"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "180"))

histories: dict[int, list[dict[str, str]]] = defaultdict(list)
authorized_chats: set[int] = set()
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def require_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not LLMHUB_API_KEY:
        missing.append("LLMHUB_API_KEY")
    if not CLAUDE_MODEL:
        missing.append("CLAUDE_MODEL")
    if not BOT_ACCESS_CODE:
        missing.append("BOT_ACCESS_CODE")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


def is_authorized(chat_id: int) -> bool:
    return chat_id in authorized_chats


async def send_long_message(update: Update, text: str) -> None:
    if not update.message:
        return
    max_len = 3900
    if not text:
        text = "(Claude returned an empty response.)"
    for i in range(0, len(text), max_len):
        await update.message.reply_text(text[i : i + max_len])


def extract_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Unexpected API response: {data}")

    content = (choices[0].get("message") or {}).get("content", "")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)

    return str(content)


async def ask_claude(messages: list[dict[str, str]]) -> str:
    headers = {
        "Authorization": f"Bearer {LLMHUB_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ],
        "stream": False,
    }

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{LLMHUB_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )

    if response.status_code >= 400:
        body = response.text[:1800]
        raise RuntimeError(f"LLM API HTTP {response.status_code}: {body}")

    return extract_text(response.json())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if is_authorized(chat_id):
        await update.message.reply_text(
            "Claude 已连接。直接发消息即可。\n/reset 清空上下文\n/model 查看当前模型"
        )
    else:
        await update.message.reply_text(
            "机器人已在线。请先使用 /auth 你的访问密码 完成认证。"
        )


async def auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id

    if is_authorized(chat_id):
        await update.message.reply_text("当前会话已经认证。")
        return

    supplied = " ".join(context.args).strip()
    if supplied and supplied == BOT_ACCESS_CODE:
        authorized_chats.add(chat_id)
        await update.message.reply_text("认证成功。现在可以直接和 Claude 对话。")
    else:
        await update.message.reply_text("访问密码不正确。")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先使用 /auth 访问密码 认证。")
        return
    histories[chat_id].clear()
    await update.message.reply_text("当前对话上下文已清空。")


async def model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    await update.message.reply_text(f"当前模型：{CLAUDE_MODEL}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先使用 /auth 访问密码 认证。")
        return

    async with chat_locks[chat_id]:
        user_text = update.message.text.strip()
        if not user_text:
            return

        histories[chat_id].append({"role": "user", "content": user_text})
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        try:
            answer = await ask_claude(histories[chat_id])
        except Exception as exc:
            if histories[chat_id] and histories[chat_id][-1].get("role") == "user":
                histories[chat_id].pop()
            message = str(exc)
            if len(message) > 1900:
                message = message[:1900] + "..."
            await update.message.reply_text(f"API 请求失败：\n{message}")
            return

        histories[chat_id].append({"role": "assistant", "content": answer})
        histories[chat_id] = histories[chat_id][-MAX_HISTORY_MESSAGES:]
        await send_long_message(update, answer)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"Telegram handler error: {context.error!r}", flush=True)


def main() -> None:
    require_config()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("auth", auth))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("model", model))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    print(f"Claude Telegram Bot starting with model={CLAUDE_MODEL}", flush=True)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
