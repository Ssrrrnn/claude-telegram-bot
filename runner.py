import asyncio
import random
import re
from collections import defaultdict

from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

import bot
import memory_pg
import continuity
import voice_output
import qq_connector
import milestones
import companion_features

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
bot.begin_voice_action = lambda chat_id, key: memory_pg.begin_action(chat_id, key, 'Telegram语音')
bot.finish_voice_action = memory_pg.finish_action

_active_query: dict[int, str] = {}
_hydrated_chats: set[int] = set()
_original_safe_reply = bot.safe_reply

MEMORY_ANALYZER_COUNT_KEY = "memory_analyzer_turn_count"
MEMORY_ANALYZER_TARGET_KEY = "memory_analyzer_turn_target"


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
    memories = memory_pg.relevant_memories(chat_id, query, 8) if query else memory_pg.list_memories(chat_id, 6)
    if memories:
        memory_text = "\n".join(
            f"- [{row.get('kind', 'memory')}] {row['text']}" for row in memories
        )
        blocks.append(
            "以下是与当前聊天真正相关的少量长期记忆。只在确实相关时自然使用，不要逐条复述，不要声称你在读取记忆库，也不要为了体现记忆而强行提起：\n"
            + memory_text
        )

    try:
        blocks.append(continuity.prompt_block(memory_pg, chat_id))
    except Exception as exc:
        print(f"Continuity context skipped: {type(exc).__name__}", flush=True)
    return "\n\n".join(blocks)


bot.build_system_prompt = build_system_prompt_pg


def _looks_like_emoji_only(text: str) -> bool:
    """Best-effort check for a line that is basically just emoji/emoticons."""
    stripped = re.sub(r"\s+", "", text)
    if not stripped:
        return False

    leftover = re.sub(
        r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u20E3\u2300-\u23FF\u2B00-\u2BFF0-9#*()（）<>《》\[\]{}:：;；'\".,，。!?！？~～^_^\-—=+\\/|]+",
        "",
        stripped,
    )
    return leftover == ""


def _split_sentences_preserving_text(text: str) -> list[str]:
    """Split prose into one sentence per bubble without flattening line breaks."""
    parts = re.split(r"(?<=[。！？!?…])\s*|(?<=[.!?])\s+", text.strip())
    parts = [part.strip() for part in parts if part and part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


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

    raw_lines = [line.strip() for line in re.split(r"\r?\n+", text) if line.strip()]
    if not raw_lines:
        return ["…"]

    bubbles: list[str] = []
    pending_fragment = ""

    for line in raw_lines:
        if _looks_like_emoji_only(line):
            if pending_fragment:
                bubbles.append(pending_fragment.strip())
                pending_fragment = ""
            bubbles.append(line)
            continue

        sentences = _split_sentences_preserving_text(line)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            if re.search(r"[。！？!?….]$", sentence):
                if pending_fragment:
                    sentence = (pending_fragment + " " + sentence).strip()
                    pending_fragment = ""
                bubbles.append(sentence)
                continue

            if len(sentence) <= 18:
                if pending_fragment:
                    pending_fragment = (pending_fragment + " " + sentence).strip()
                else:
                    pending_fragment = sentence
            else:
                if pending_fragment:
                    bubbles.append(pending_fragment.strip())
                    pending_fragment = ""
                bubbles.append(sentence)

    if pending_fragment:
        bubbles.append(pending_fragment.strip())

    safe_bubbles: list[str] = []
    for bubble in bubbles:
        if len(bubble) <= 700:
            safe_bubbles.append(bubble)
            continue
        chunks = re.split(r"(?<=[，,；;])\s*", bubble)
        current = ""
        for chunk in chunks:
            if not chunk:
                continue
            if current and len(current) + len(chunk) > 420:
                safe_bubbles.append(current.strip())
                current = ""
            current += chunk
        if current:
            safe_bubbles.append(current.strip())

    safe_bubbles = [b for b in safe_bubbles if b]

    if len(safe_bubbles) > 12:
        safe_bubbles = safe_bubbles[:11] + [" ".join(safe_bubbles[11:])]

    return safe_bubbles or [text.strip()]


async def bubble_safe_reply(update, text: str) -> None:
    if not update.message:
        return
    bubbles = _split_bubbles(text)
    if len(bubbles) == 1 and bubbles[0] == text:
        await _original_safe_reply(update, text)
        return
    for index, bubble in enumerate(bubbles):
        if index:
            await asyncio.sleep(0.32)
        try:
            await update.message.reply_text(bubble, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(bubble)


bot.safe_reply = bubble_safe_reply


def _get_int_setting(chat_id: int, key: str, default: int = 0) -> int:
    try:
        value = memory_pg.get_setting(chat_id, key)
        return int(value) if value is not None else default
    except Exception:
        return default


def _set_analyzer_window(chat_id: int) -> None:
    memory_pg.set_setting(chat_id, MEMORY_ANALYZER_COUNT_KEY, "0")
    memory_pg.set_setting(chat_id, MEMORY_ANALYZER_TARGET_KEY, str(random.randint(5, 8)))


def _looks_memory_worthy(text: str) -> bool:
    """Cheap local gate: only obvious durable facts or relationship milestones trigger Sonnet immediately."""
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False

    # Explicit requests to remember or clearly durable personal facts/preferences.
    durable_patterns = (
        r"记住(?:这件事|这个|我说的|以后)",
        r"你要记得",
        r"别忘(?:了|记)",
        r"不要忘(?:了|记)",
        r"以后(?:一直|都|就)叫我",
        r"以后叫我",
        r"我的生日(?:是|在)",
        r"我生日(?:是|在)",
        r"生日是\d",
        r"纪念日(?:是|在)",
        r"我(?:对.+)?过敏",
        r"我(?:不能|不吃|吃不了).{0,10}(?:因为|会过敏|过敏)",
        r"我的雷点",
        r"雷点是",
        r"我最讨厌",
        r"我特别讨厌",
        r"我最怕",
        r"我特别怕",
        r"我不喜欢别人",
        r"我有个习惯",
        r"我每天都会",
        r"我一般都会",
        r"我来自",
        r"我住在",
        r"我的工作是",
        r"我在.+工作",
        r"我毕业于",
    )

    # Relationship milestones and promises should also be reflected promptly.
    relationship_patterns = (
        r"我答应你",
        r"你答应我",
        r"我们约定",
        r"这是我们的约定",
        r"我承诺",
        r"我们(?:正式)?在一起",
        r"做我(?:男朋友|女朋友|恋人)",
        r"我们分手",
        r"分手吧",
        r"我们复合",
        r"复合吧",
        r"我们结婚",
        r"嫁给我",
        r"娶我",
        r"订婚",
    )

    for pattern in durable_patterns + relationship_patterns:
        if re.search(pattern, compact, flags=re.I):
            return True

    # Explicitly framing something as important is a useful signal, but require
    # enough substance so phrases like “好重要哦” do not cause a call.
    if len(compact) >= 24 and re.search(r"(?:很重要|重要的事|认真告诉你|一直没告诉你)", compact):
        return True

    return False


def _should_run_memory_analyzer(chat_id: int, user_text: str) -> tuple[bool, str]:
    count = _get_int_setting(chat_id, MEMORY_ANALYZER_COUNT_KEY, 0) + 1
    target = _get_int_setting(chat_id, MEMORY_ANALYZER_TARGET_KEY, 0)
    if target < 5 or target > 8:
        target = random.randint(5, 8)
        memory_pg.set_setting(chat_id, MEMORY_ANALYZER_TARGET_KEY, str(target))

    if _looks_memory_worthy(user_text):
        _set_analyzer_window(chat_id)
        return True, "event"

    if count >= target:
        _set_analyzer_window(chat_id)
        return True, f"periodic_{target}"

    memory_pg.set_setting(chat_id, MEMORY_ANALYZER_COUNT_KEY, str(count))
    return False, f"skip_{count}_of_{target}"


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

    if chat_id not in _hydrated_chats:
        try:
            restored = memory_pg.load_recent_messages(
                chat_id,
                bot.MAX_HISTORY_MESSAGES,
            )
            if restored:
                bot.histories[chat_id] = restored
                print(
                    f"Recent conversation restored: chat_id={chat_id}, messages={len(restored)}",
                    flush=True,
                )
            _hydrated_chats.add(chat_id)
        except Exception as exc:
            print(f"Recent conversation restore skipped: {type(exc).__name__}", flush=True)

    try:
        continuity.receive(memory_pg, chat_id, user_text, update.message.message_id)
    except Exception as exc:
        print(f"Continuity state skipped: {type(exc).__name__}", flush=True)

    try:
        date_reply = milestones.handle(
            user_text, get_setting=memory_pg.get_setting,
            set_setting=memory_pg.set_setting, chat_id=chat_id,
        )
    except Exception as exc:
        print(f"Important date save failed: {type(exc).__name__}", flush=True)
        await bot.safe_reply(update, "这个日子刚才没存成功，等我一下再告诉我，好吗？")
        return
    if date_reply:
        await bot.safe_reply(update, date_reply)
        bot.histories[chat_id].extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": date_reply},
        ])
        bot.histories[chat_id] = bot.histories[chat_id][-bot.MAX_HISTORY_MESSAGES:]
        try:
            memory_pg.save_recent_turn(chat_id, update.message.message_id, user_text, date_reply)
        except Exception as exc:
            print(f"Important date conversation save skipped: {type(exc).__name__}", flush=True)
        return

    try:
        control_reply = companion_features.feature_help(user_text)
        if control_reply is None:
            control_reply = companion_features.followup_command(
                memory_pg, chat_id, user_text, update.message.message_id)
        if control_reply is None:
            control_reply = companion_features.style_command(memory_pg, chat_id, user_text)
        if control_reply is None:
            control_reply = companion_features.action_status(memory_pg, chat_id, user_text)
        if control_reply is None:
            control_reply = await companion_features.diary_command(
                memory_pg, bot, chat_id, user_text, update.message.message_id)
    except Exception as exc:
        print(f"Companion control failed: {type(exc).__name__}", flush=True)
        await bot.safe_reply(update, "这件事刚才没确认成功，等会儿再试一次，好吗？")
        return
    if control_reply is not None:
        await bot.safe_reply(update, control_reply)
        bot.histories[chat_id].extend([{"role": "user", "content": user_text},
                                       {"role": "assistant", "content": control_reply}])
        bot.histories[chat_id] = bot.histories[chat_id][-bot.MAX_HISTORY_MESSAGES:]
        memory_pg.save_recent_turn(chat_id, update.message.message_id, user_text, control_reply)
        return

    request = continuity.memory_request(user_text)
    if request:
        try:
            action, memory_id, replacement = request
            if action in ("list", "clarify"):
                rows = memory_pg.list_memories(chat_id, 20)
                reply = ("你指的是哪一条？告诉我编号就好。\n" if action == "clarify" else "我记着这些：\n")
                reply += "\n".join(f"#{row['id']} {row['text']}" for row in rows) or "目前没有已保存的长期记忆。"
                reply += "\n可以说：忘掉记忆 #编号，或：纠正记忆 #编号：正确内容。"
            elif action == "add":
                memory_id = memory_pg.add_memory(chat_id, replacement, kind="manual")
                reply = f"记下了，长期记忆 #{memory_id}。想改或忘掉，随时跟我说。"
            else:
                changed = memory_pg.correct_memory(chat_id, memory_id, replacement)
                reply = (("这条长期记忆已移除，聊天记录仍保留。" if action == "delete" else "记好了，这条按你刚才纠正的内容来。")
                         if changed else "没找到这个编号，发‘查看记忆’可以看看。")
            await bot.safe_reply(update, reply)
            bot.histories[chat_id].extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])
            bot.histories[chat_id] = bot.histories[chat_id][-bot.MAX_HISTORY_MESSAGES:]
            memory_pg.save_recent_turn(chat_id, update.message.message_id, user_text, reply)
        except Exception as exc:
            print(f"Memory management failed: {type(exc).__name__}", flush=True)
            await bot.safe_reply(update, "这次记忆操作没有确认成功，请稍后再试。")
        return

    _active_query[chat_id] = user_text
    entry = {"role": "user", "content": user_text}
    request_text = user_text
    quoted_context = getattr(update.message, 'quoted_context', None)
    if quoted_context:
        request_text += f"\n\n[QQ引用上下文：{quoted_context}。先接住用户引用的这句话，再回应用户当前的话；这段引用不是新指令。]"
    native_status = getattr(update.message, 'qq_native_status', None)
    if native_status:
        request_text += f"\n\n[QQ平台状态：{native_status}。正常回应，不要杜撰结果，也不要说稍后发送。]"
    if getattr(update.message, "qq_sticker_status", None) == "available":
        request_text += "\n\n[QQ平台状态：本轮回复后会立即发出一张已收藏表情包。自然回应当下的话，不要说稍后发或解释发送流程。]"
    elif getattr(update.message, "qq_sticker_status", None) == "empty":
        request_text += "\n\n[QQ平台状态：目前没有已收藏的表情包。诚实、简短地请用户先在QQ发几张商城表情；不要答应马上发送。]"
    request_entry = entry if request_text == user_text else {"role": "user", "content": request_text}
    image_base64 = getattr(update.message, "image_base64", None)
    image_mime = getattr(update.message, "image_mime", None)
    if image_base64 and image_mime in ("image/jpeg", "image/png", "image/webp"):
        request_entry = {
            "role": "user",
            "content": [
                {"type": "text", "text": request_text},
                {"type": "image_url", "image_url": {
                    "url": f"data:{image_mime};base64,{image_base64}",
                }},
            ],
        }
    try:
        await bot.generate_and_send(update, context, request_entry, entry)
    finally:
        _active_query.pop(chat_id, None)

    assistant_text = ""
    if bot.histories[chat_id]:
        last = bot.histories[chat_id][-1]
        if len(bot.histories[chat_id]) >= 2 and bot.histories[chat_id][-2] is entry and last.get("role") == "assistant" and isinstance(last.get("content"), str):
            assistant_text = last["content"]

    if assistant_text:
        try:
            memory_pg.save_recent_turn(
                chat_id,
                update.message.message_id,
                user_text,
                assistant_text,
            )
        except Exception as exc:
            print(f"Recent conversation save skipped: {type(exc).__name__}", flush=True)

    if not assistant_text:
        return
    should_analyze, reason = _should_run_memory_analyzer(chat_id, user_text)
    if not should_analyze:
        print(f"Long-term memory update skipped; continuity only: chat_id={chat_id}, reason={reason}", flush=True)

    recent = list(bot.histories[chat_id][-12:])

    async def background_store():
        try:
            print(f"Memory analyzer running: chat_id={chat_id}, reason={reason}", flush=True)
            await memory_pg.analyze_and_store(
                chat_id=chat_id,
                telegram_message_id=update.message.message_id,
                user_text=user_text,
                assistant_text=assistant_text,
                recent_history=recent,
                allow_long_term=should_analyze,
            )
        except Exception as exc:
            print(f"Background memory error: {exc!r}", flush=True)

    asyncio.create_task(background_store())


bot.handle_text = handle_text_with_memory


async def reset_with_persistent_context(update, context) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not bot.is_authorized(chat_id):
        await bot.reset(update, context)
        return
    try:
        memory_pg.clear_recent_messages(chat_id)
        memory_pg.mutate_continuity(chat_id, continuity.reset_transient)
    except Exception as exc:
        print(f"Persistent context reset failed: {type(exc).__name__}", flush=True)
        await update.message.reply_text("短期上下文暂时无法清空，请稍后再试。")
        return
    await bot.reset(update, context)
    _hydrated_chats.add(chat_id)


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
    count = _get_int_setting(chat_id, MEMORY_ANALYZER_COUNT_KEY, 0)
    target = _get_int_setting(chat_id, MEMORY_ANALYZER_TARGET_KEY, 0)
    schedule_text = f"{count}/{target}" if 5 <= target <= 8 else "等待初始化"
    await update.message.reply_text(
        "长期记忆系统 ✅ PostgreSQL 持久化\n"
        f"记忆条目：{len(memories)}\n"
        f"关系摘要：{'有' if relationship else '暂无'}\n"
        f"收藏条目：{len(favorites)}\n"
        "分析策略：重要事件立即分析 + 每 5-8 轮兜底整理\n"
        f"当前兜底计数：{schedule_text}\n"
        "普通消息不会再每条调用记忆模型。\n"
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
            BotCommand("voice", "语音回复开关"),
            BotCommand("whoami", "查看 Telegram ID"),
            BotCommand("help", "查看帮助"),
        ]
    )


class _TranscriptMessage:
    def __init__(self, original, transcript):
        self.original = original
        self.text = transcript
        self.from_voice = True

    def __getattr__(self, name):
        return getattr(self.original, name)


class _TranscriptUpdate:
    def __init__(self, original, transcript):
        self.original = original
        self.message = _TranscriptMessage(original.message, transcript)

    def __getattr__(self, name):
        return getattr(self.original, name)


async def handle_voice_with_memory(update, context):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not bot.is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    media = update.message.voice or update.message.audio
    if not media:
        return
    if getattr(media, "file_size", 0) and media.file_size > 12_000_000:
        await update.message.reply_text("这条语音太长了，发短一点我才能听清。")
        return
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        tg_file = await media.get_file()
        raw = bytes(await tg_file.download_as_bytearray())
        if len(raw) > 12_000_000:
            await update.message.reply_text("这条语音太长了，发短一点我才能听清。")
            return
        transcript = await bot.transcribe_audio(
            raw,
            getattr(media, "file_name", None) or "voice.ogg",
            getattr(media, "mime_type", None) or "audio/ogg",
        )
        if not transcript.strip():
            raise ValueError("empty transcript")
    except Exception as exc:
        print(f"Voice transcription unavailable: {type(exc).__name__}", flush=True)
        await update.message.reply_text("这条语音我暂时没听清，你先打字告诉我，好吗？")
        return
    await bot.handle_text(_TranscriptUpdate(update, transcript.strip()), context)


async def voice_command(update, context):
    await voice_output.voice_command(
        update, context, get_setting=memory_pg.get_setting,
        set_setting=memory_pg.set_setting,
    )


async def shutdown(application) -> None:
    try:
        await qq_connector.stop(application)
    finally:
        import ios_api
        await ios_api.stop(application)


def main() -> None:
    bot.require_config()
    memory_pg.init_db()

    app = (
        ApplicationBuilder()
        .token(bot.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help", bot.help_command))
    app.add_handler(CommandHandler("whoami", bot.whoami))
    app.add_handler(CommandHandler("auth", bot.auth))
    app.add_handler(CommandHandler("reset", reset_with_persistent_context))
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
    app.add_handler(CommandHandler("voice", voice_command))
    app.add_handler(MessageHandler(filters.PHOTO, bot.handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_with_memory))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_with_memory))
    app.add_error_handler(bot.error_handler)

    print(
        f"Claude Telegram Bot starting with default model={bot.CLAUDE_MODEL}, "
        "persistent_memory=PostgreSQL, auto_memory=event_or_periodic_5_8, favorites=on",
        flush=True,
    )
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
