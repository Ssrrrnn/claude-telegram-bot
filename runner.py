import asyncio
import re
from collections import defaultdict

from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

import bot
import memory_pg

# Default to chat-bubble mode instead of live-edit streaming.
bot.stream_preferences = defaultdict(lambda: False)

# Replace the old ephemeral SQLite helpers with persistent PostgreSQL-backed ones.
bot.init_db = memory_pg.init_db
bot.set_setting = memory_pg.set_setting
bot.get_setting = memory_pg.get_setting
bot.add_memory = memory_pg.add_memory
bot.list_memories = memory_pg.list_memories
bot.delete_memory = memory_pg.delete_memory
bot.clear_memories = memory_pg.clear_memories

_active_query: dict[int, str] = {}
_original_safe_reply = bot.safe_reply


def build_system_prompt_pg(chat_id: int) -> str:
    blocks = [bot.SYSTEM_PROMPT]

    books = bot.enabled_worldbooks(chat_id)
    if books:
        blocks.append(
            "以下为当前启用的角色扮演世界书。它们用于补充角色表现与文风；若与角色卡冲突，以角色既有人设为准。\n"
            + bot.render_worldbooks(books)
        )

    relationship = memory_pg.get_relationship(chat_id)
    if relationship:
        blocks.append(
            "以下是长期保存的关系状态与共同经历摘要。自然延续这些关系，不要机械复述，也不要说明这是数据库内容：\n"
            + relationship
        )

    query = _active_query.get(chat_id, "")
    memories = memory_pg.relevant_memories(chat_id, query, 14) if query else memory_pg.list_memories(chat_id, 12)
    if memories:
        memory_text = "\n".join(
            f"- [{row.get('kind', 'memory')}] {row['text']}" for row in memories
        )
        blocks.append(
            "以下是与当前聊天相关的长期记忆。只在相关时自然使用，不要逐条复述，不要声称你在读取记忆库：\n"
            + memory_text
        )

    return "\n\n".join(blocks)


bot.build_system_prompt = build_system_prompt_pg


def _split_bubbles(text: str) -> list[str]:
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

    normalized = re.sub(r"\s*\n+\s*", " ", text).strip()
    normalized = re.sub(r"[ \t]{2,}", " ", normalized)
    if not normalized:
        return ["…"]

    sentences = re.split(r"(?<=[。！？!?])\s*|(?<=[.!?])\s+", normalized)
    sentences = [s.strip() for s in sentences if s and s.strip()]
    if not sentences:
        sentences = [normalized]

    bubbles: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        sentence_len = len(sentence)
        if current and (len(current) >= 2 or current_len + sentence_len > 260):
            bubbles.append(" ".join(current).strip())
            current = []
            current_len = 0

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
    if len(bubbles) > 8:
        bubbles = bubbles[:7] + [" ".join(bubbles[7:])]
    return bubbles or [normalized]


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


bot.safe_reply = bubble_safe_reply


async def handle_text_with_memory(update, context) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    if not bot.is_authorized(chat_id):
        await update.message.reply_text("请先使用 /auth 访问密码 认证。")
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    _active_query[chat_id] = user_text
    entry = {"role": "user", "content": user_text}
    try:
        await bot.generate_and_send(update, context, entry, entry)
    finally:
        _active_query.pop(chat_id, None)

    assistant_text = ""
    if bot.histories[chat_id]:
        last = bot.histories[chat_id][-1]
        if last.get("role") == "assistant" and isinstance(last.get("content"), str):
            assistant_text = last["content"]

    # Memory analysis happens after the visible reply so it does not slow the chat.
    recent = list(bot.histories[chat_id][-12:])

    async def background_store():
        try:
            await memory_pg.analyze_and_store(
                chat_id=chat_id,
                telegram_message_id=update.message.message_id,
                user_text=user_text,
                assistant_text=assistant_text,
                recent_history=recent,
            )
        except Exception as exc:
            print(f"Background memory error: {exc!r}", flush=True)

    asyncio.create_task(background_store())


bot.handle_text = handle_text_with_memory


async def relationship_command(update, context) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not bot.is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    summary = memory_pg.get_relationship(chat_id)
    if not summary:
        await update.message.reply_text("目前还没有形成稳定的关系记忆。多聊一会儿后会自动生成。")
        return
    await bot.safe_reply(update, "关系状态：\n" + summary)


async def favorites_command(update, context) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not bot.is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    query = " ".join(context.args).strip()
    rows = memory_pg.list_favorites(chat_id, 20, query=query)
    if not rows:
        await update.message.reply_text("目前没有符合条件的收藏。")
        return
    lines = ["收藏："]
    for row in rows:
        text = str(row["text"]).replace("\n", " ")
        if len(text) > 160:
            text = text[:157] + "..."
        note = str(row.get("note") or "").strip()
        suffix = f"\n   ↳ {note}" if note else ""
        lines.append(f"#{row['id']}  {text}{suffix}")
    await bot.safe_reply(update, "\n".join(lines))


async def unfavorite_command(update, context) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not bot.is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    if not context.args:
        await update.message.reply_text("用法：/unfavorite 收藏编号，例如 /unfavorite 3")
        return
    try:
        favorite_id = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("收藏编号必须是数字。")
        return
    if memory_pg.delete_favorite(chat_id, favorite_id):
        await update.message.reply_text(f"已取消收藏 #{favorite_id}。")
    else:
        await update.message.reply_text("没有找到这条收藏。")


async def memory_status_command(update, context) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not bot.is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    memories = memory_pg.list_memories(chat_id, 200)
    favorites = memory_pg.list_favorites(chat_id, 200)
    relationship = memory_pg.get_relationship(chat_id)
    await update.message.reply_text(
        "长期记忆系统 ✅ PostgreSQL 持久化\n"
        f"记忆条目：{len(memories)}\n"
        f"关系摘要：{'有' if relationship else '暂无'}\n"
        f"收藏条目：{len(favorites)}\n"
        "重新部署 Bot 不会清空这些数据。"
    )


async def post_init(application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "启动机器人"),
            BotCommand("reset", "清空短期上下文"),
            BotCommand("model", "查看或切换模型"),
            BotCommand("models", "查看可用模型"),
            BotCommand("worldbooks", "查看已安装世界书"),
            BotCommand("world", "开关世界书"),
            BotCommand("remember", "手动添加长期记忆"),
            BotCommand("memories", "查看长期记忆"),
            BotCommand("relationship", "查看关系记忆摘要"),
            BotCommand("favorites", "查看/搜索收藏"),
            BotCommand("unfavorite", "删除一条收藏"),
            BotCommand("memorystatus", "查看记忆系统状态"),
            BotCommand("forget", "删除一条长期记忆"),
            BotCommand("forgetall", "清空长期记忆"),
            BotCommand("stream", "开关流式输出"),
            BotCommand("whoami", "查看 Telegram ID"),
            BotCommand("help", "查看帮助"),
        ]
    )


def main() -> None:
    bot.require_config()
    memory_pg.init_db()

    app = (
        ApplicationBuilder()
        .token(bot.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help", bot.help_command))
    app.add_handler(CommandHandler("whoami", bot.whoami))
    app.add_handler(CommandHandler("auth", bot.auth))
    app.add_handler(CommandHandler("reset", bot.reset))
    app.add_handler(CommandHandler("model", bot.model_command))
    app.add_handler(CommandHandler("models", bot.models_command))
    app.add_handler(CommandHandler("worldbooks", bot.worldbooks_command))
    app.add_handler(CommandHandler("world", bot.world_command))
    app.add_handler(CommandHandler("remember", bot.remember_command))
    app.add_handler(CommandHandler("memories", bot.memories_command))
    app.add_handler(CommandHandler("relationship", relationship_command))
    app.add_handler(CommandHandler("favorites", favorites_command))
    app.add_handler(CommandHandler("unfavorite", unfavorite_command))
    app.add_handler(CommandHandler("memorystatus", memory_status_command))
    app.add_handler(CommandHandler("forget", bot.forget_command))
    app.add_handler(CommandHandler("forgetall", bot.forgetall_command))
    app.add_handler(CommandHandler("stream", bot.stream_command))
    app.add_handler(MessageHandler(filters.PHOTO, bot.handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot.handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_with_memory))
    app.add_error_handler(bot.error_handler)

    print(
        f"Claude Telegram Bot starting with default model={bot.CLAUDE_MODEL}, "
        "persistent_memory=PostgreSQL, auto_memory=on, favorites=on",
        flush=True,
    )
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
