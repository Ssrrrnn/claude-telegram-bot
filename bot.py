import asyncio
import base64
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import realtime_clock
from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from worldbooks import DEFAULT_WORLD_BOOKS, WORLD_BOOKS, render_worldbooks

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
LLMHUB_API_KEY = os.environ.get("LLMHUB_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()
LLMHUB_BASE_URL = os.environ.get("LLMHUB_BASE_URL", "https://llmhub.uk/v1").rstrip("/")
BOT_ACCESS_CODE = os.environ.get("BOT_ACCESS_CODE", "").strip()
OWNER_ID_RAW = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "whisper-1").strip()
MEMORY_DB_PATH = os.environ.get("MEMORY_DB_PATH", "./claude_bot_memory.db").strip()

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "你是用户的私人 AI 助手。默认使用中文回答。回答准确、自然、有逻辑。",
)

MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "30"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "180"))
STREAM_EDIT_INTERVAL = float(os.environ.get("STREAM_EDIT_INTERVAL", "0.9"))

try:
    TELEGRAM_OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW else None
except ValueError:
    TELEGRAM_OWNER_ID = None

histories: dict[int, list[dict[str, Any]]] = defaultdict(list)
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
stream_preferences: dict[int, bool] = defaultdict(lambda: True)
authorized_chats: set[int] = set()
CLAIMED_OWNER_ID: int | None = TELEGRAM_OWNER_ID


def require_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not LLMHUB_API_KEY:
        missing.append("LLMHUB_API_KEY")
    if not CLAUDE_MODEL:
        missing.append("CLAUDE_MODEL")
    if not BOT_ACCESS_CODE and TELEGRAM_OWNER_ID is None:
        missing.append("BOT_ACCESS_CODE or TELEGRAM_OWNER_ID")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


def db_connect() -> sqlite3.Connection:
    path = Path(MEMORY_DB_PATH)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    global CLAIMED_OWNER_ID
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (chat_id, key)
            )
            """
        )
        conn.commit()
        if TELEGRAM_OWNER_ID is None:
            row = conn.execute(
                "SELECT value FROM settings WHERE chat_id = 0 AND key = 'owner_id'"
            ).fetchone()
            if row:
                try:
                    CLAIMED_OWNER_ID = int(row["value"])
                except (TypeError, ValueError):
                    CLAIMED_OWNER_ID = None


def set_setting(chat_id: int, key: str, value: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(chat_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value",
            (chat_id, key, value),
        )
        conn.commit()


def get_setting(chat_id: int, key: str) -> str | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        ).fetchone()
    return str(row["value"]) if row else None


def add_memory(chat_id: int, text: str) -> int:
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO memories(chat_id, text, created_at) VALUES (?, ?, ?)",
            (chat_id, text, int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_memories(chat_id: int, limit: int = 30) -> list[sqlite3.Row]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, text, created_at FROM memories WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return list(reversed(rows))


def delete_memory(chat_id: int, memory_id: int) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM memories WHERE chat_id = ? AND id = ?",
            (chat_id, memory_id),
        )
        conn.commit()
        return cur.rowcount > 0


def clear_memories(chat_id: int) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM memories WHERE chat_id = ?", (chat_id,))
        conn.commit()


def is_authorized(chat_id: int) -> bool:
    if TELEGRAM_OWNER_ID is not None:
        return chat_id == TELEGRAM_OWNER_ID
    if CLAIMED_OWNER_ID is not None:
        return chat_id == CLAIMED_OWNER_ID
    return chat_id in authorized_chats


def current_model(chat_id: int) -> str:
    return get_setting(chat_id, "model") or CLAUDE_MODEL


def enabled_worldbooks(chat_id: int) -> list[str]:
    raw = get_setting(chat_id, "worldbooks")
    if raw is None:
        return list(DEFAULT_WORLD_BOOKS)
    try:
        names = json.loads(raw)
    except json.JSONDecodeError:
        return list(DEFAULT_WORLD_BOOKS)
    if not isinstance(names, list):
        return list(DEFAULT_WORLD_BOOKS)
    return [name for name in names if name in WORLD_BOOKS]


def save_worldbooks(chat_id: int, names: list[str]) -> None:
    set_setting(chat_id, "worldbooks", json.dumps(names, ensure_ascii=False))


def build_system_prompt(chat_id: int) -> str:
    blocks = [SYSTEM_PROMPT]

    books = enabled_worldbooks(chat_id)
    if books:
        blocks.append(
            "以下为当前启用的角色扮演世界书。它们用于补充角色表现与文风；若与角色卡冲突，以角色既有人设为准。\n"
            + render_worldbooks(books)
        )

    memories = list_memories(chat_id, 30)
    if memories:
        memory_text = "\n".join(f"- {row['text']}" for row in memories)
        blocks.append(
            "以下是用户明确要求长期记住的信息。只在相关时自然使用，不要每次主动复述：\n"
            + memory_text
        )
    return "\n\n".join(blocks)


def split_text(text: str, max_len: int = 3900) -> list[str]:
    if not text:
        return ["(Claude returned an empty response.)"]
    return [text[i : i + max_len] for i in range(0, len(text), max_len)]


async def safe_reply(update: Update, text: str) -> None:
    if not update.message:
        return
    for chunk in split_text(text):
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await update.message.reply_text(chunk)


async def safe_edit(message: Any, text: str) -> None:
    try:
        await message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        try:
            await message.edit_text(text)
        except Exception:
            pass


def extract_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Unexpected API response: {data}")
    content = (choices[0].get("message") or {}).get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return str(content)


def extract_stream_delta(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return ""


def api_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {LLMHUB_API_KEY}",
        "Content-Type": "application/json",
    }


async def ask_claude(chat_id: int, messages: list[dict[str, Any]], model: str) -> str:
    clock_kind = realtime_clock.clock_question(realtime_clock.latest_user_text(messages))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_system_prompt(chat_id) + "\n\n" + realtime_clock.request_clock_block() + realtime_clock.clock_reply_instruction(clock_kind)},
            *messages,
        ],
        "stream": False,
    }
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{LLMHUB_BASE_URL}/chat/completions",
            headers=api_headers(),
            json=payload,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"LLM API HTTP {response.status_code}: {response.text[:1800]}")
    answer = extract_text(response.json())
    return realtime_clock.render_clock_reply(answer, clock_kind) if clock_kind else answer


async def stream_claude(chat_id: int, messages: list[dict[str, Any]], model: str):
    if realtime_clock.clock_question(realtime_clock.latest_user_text(messages)):
        # Buffer this small reply so the internal token can never reach Telegram.
        yield await ask_claude(chat_id, messages, model)
        return
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_system_prompt(chat_id) + "\n\n" + realtime_clock.request_clock_block()},
            *messages,
        ],
        "stream": True,
    }
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    yielded = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{LLMHUB_BASE_URL}/chat/completions",
            headers=api_headers(),
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")[:1800]
                raise RuntimeError(f"LLM API HTTP {response.status_code}: {body}")
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                delta = extract_stream_delta(data)
                if delta:
                    yielded = True
                    yield delta
    if not yielded:
        raise RuntimeError("Streaming is not supported by this gateway response.")


async def fetch_models() -> list[str]:
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            f"{LLMHUB_BASE_URL}/models",
            headers={"Authorization": f"Bearer {LLMHUB_API_KEY}"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Models API HTTP {response.status_code}: {response.text[:1000]}")
    data = response.json().get("data") or []
    model_ids = []
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            model_ids.append(str(item["id"]))
    return sorted(set(model_ids))


async def transcribe_audio(raw: bytes, filename: str, mime_type: str) -> str:
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    files = {"file": (filename, raw, mime_type)}
    data = {"model": TRANSCRIBE_MODEL}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{LLMHUB_BASE_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {LLMHUB_API_KEY}"},
            files=files,
            data=data,
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"语音转写接口 HTTP {response.status_code}: {response.text[:1200]}"
        )
    result = response.json()
    text = result.get("text") if isinstance(result, dict) else None
    if not text:
        raise RuntimeError(f"语音转写返回异常: {result}")
    return str(text).strip()


VOICE_REPLY_INSTRUCTION = (
    "【本轮回复方式】程序会把你这一条回复直接合成语音并发送。"
    "请直接说你现在想对用户说的话，像正在通话一样自然地接住上下文。"
    "不要说‘等一下’‘稍等’‘马上给你发’‘我录一下’，不要解释如何发送语音、请求用户等待或许诺下一条消息。"
    "这一条文字本身就是语音的内容。"
)


def voice_deferral(text: str) -> bool:
    """Catch short promises to send audio instead of an actual reply."""
    compact = re.sub(r"\s+", "", text)
    if len(compact) > 95:
        return False
    return bool(re.search(
        r"(?:等一下|稍等|等我|等会|待会|一会儿|马上|这就|稍后|先等|我去|我来).{0,24}"
        r"(?:发|录|做|生成|合成|弄|准备|回|说).{0,8}(?:语音|声音|音频)"
        r"|(?:语音|声音|音频).{0,14}(?:发给你|给你发|马上发|等一下|稍等)",
        compact,
    ))


async def generate_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request_user_entry: dict[str, Any],
    history_user_entry: dict[str, Any],
) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    async with chat_locks[chat_id]:
        import voice_output
        user_text = history_user_entry.get("content", "")
        voice_mode = voice_output.mode(get_setting, chat_id)
        use_voice = voice_output.wants_audio(
            voice_mode,
            from_voice=getattr(update.message, "from_voice", False),
            user_text=user_text if isinstance(user_text, str) else "",
        )
        request_messages = [*histories[chat_id], request_user_entry]
        if use_voice and isinstance(request_user_entry.get("content"), str):
            request_messages[-1] = {
                **request_user_entry,
                "content": request_user_entry["content"] + "\n\n" + VOICE_REPLY_INSTRUCTION,
            }
        model = current_model(chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        answer = ""
        placeholder = None
        try:
            if stream_preferences[chat_id] and not use_voice:
                placeholder = await update.message.reply_text("…")
                last_edit = 0.0
                async for delta in stream_claude(chat_id, request_messages, model):
                    answer += delta
                    now = time.monotonic()
                    if now - last_edit >= STREAM_EDIT_INTERVAL and len(answer) <= 3800:
                        preview = answer + "▌"
                        try:
                            await placeholder.edit_text(preview)
                        except Exception:
                            pass
                        last_edit = now
            else:
                answer = await ask_claude(chat_id, request_messages, model)
        except Exception as stream_exc:
            if stream_preferences[chat_id] and not use_voice and not answer:
                try:
                    answer = await ask_claude(chat_id, request_messages, model)
                except Exception as fallback_exc:
                    if placeholder:
                        await safe_edit(placeholder, f"API 请求失败：\n{str(fallback_exc)[:1800]}")
                    else:
                        await safe_reply(update, f"API 请求失败：\n{str(fallback_exc)[:1800]}")
                    return
            elif answer:
                answer += f"\n\n[流式输出中断：{str(stream_exc)[:300]}]"
            else:
                await safe_reply(update, f"API 请求失败：\n{str(stream_exc)[:1800]}")
                return

        if use_voice and voice_deferral(answer):
            try:
                retry_messages = [
                    *request_messages[:-1],
                    {**request_messages[-1], "content": request_messages[-1]["content"] + (
                        "\n\n【生成校正】上一版只答应稍后发语音，没有真正回应。"
                        "本条消息将直接作为语音发送。现在就说出完整、具体的回复；不要提发送流程或等待。"
                    )},
                ]
                answer = await ask_claude(chat_id, retry_messages, model)
            except Exception as exc:
                print(f"Voice reply retry failed: {type(exc).__name__}", flush=True)
            if voice_deferral(answer):
                # Never send an empty promise as a voice message.
                answer = "我在。刚才那句没说好，想听你说说现在的事。"

        chunks = split_text(answer)
        if placeholder:
            await safe_edit(placeholder, chunks[0])
            for chunk in chunks[1:]:
                try:
                    await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    await update.message.reply_text(chunk)
        else:
            sent_as_voice = use_voice and await voice_output.send_voice(
                context.bot, chat_id, answer,
                reply_to_message_id=update.message.message_id,
                get_setting=get_setting,
            )
            if not sent_as_voice:
                await safe_reply(update, answer)
            print(f"Reply delivered: chat_id={chat_id}, channel={'voice' if sent_as_voice else 'text'}", flush=True)

        histories[chat_id].append(history_user_entry)
        histories[chat_id].append({"role": "assistant", "content": answer})
        histories[chat_id] = histories[chat_id][-MAX_HISTORY_MESSAGES:]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if is_authorized(chat_id):
        await update.message.reply_text(
            "Claude 已连接 ✅\n\n"
            "直接发文字即可；也可以发图片让我分析。\n"
            "/reset 清空短期上下文\n"
            "/model 查看/切换模型\n"
            "/models 查看可用模型\n"
            "/worldbooks 查看世界书\n"
            "/world on|off 名称 开关世界书\n"
            "/remember 添加长期记忆\n"
            "/memories 查看记忆\n"
            "/stream on|off 控制流式输出\n"
            "/whoami 查看你的 Telegram ID"
        )
    else:
        await update.message.reply_text(
            "机器人已在线。请先使用 /auth 你的访问密码 完成认证。\n"
            "也可以先发送 /whoami 查看自己的 Telegram ID。"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"你的 Telegram user ID：{user_id}\n当前 chat ID：{chat_id}"
    )


async def auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CLAIMED_OWNER_ID
    if not update.message:
        return
    chat_id = update.effective_chat.id

    if TELEGRAM_OWNER_ID is not None and chat_id != TELEGRAM_OWNER_ID:
        await update.message.reply_text("这个机器人只允许指定的 Telegram 账号使用。")
        return
    if CLAIMED_OWNER_ID is not None and chat_id != CLAIMED_OWNER_ID:
        await update.message.reply_text("这个机器人已经绑定到其他 Telegram 账号。")
        return
    if is_authorized(chat_id):
        await update.message.reply_text("当前会话已经认证。")
        return

    supplied = " ".join(context.args).strip()
    if supplied and supplied == BOT_ACCESS_CODE:
        authorized_chats.add(chat_id)
        if CLAIMED_OWNER_ID is None:
            CLAIMED_OWNER_ID = chat_id
            set_setting(0, "owner_id", str(chat_id))
        await update.message.reply_text(
            "认证成功 ✅ 已将当前 Telegram 会话作为机器人所有者。"
        )
    else:
        await update.message.reply_text("访问密码不正确。")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    histories[chat_id].clear()
    await update.message.reply_text("当前短期对话上下文已清空；长期记忆和世界书设置没有删除。")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    if not context.args:
        await update.message.reply_text(
            f"当前模型：{current_model(chat_id)}\n"
            f"默认模型：{CLAUDE_MODEL}\n\n"
            "切换：/model 模型ID\n恢复默认：/model default"
        )
        return

    requested = " ".join(context.args).strip()
    if requested.lower() == "default":
        set_setting(chat_id, "model", CLAUDE_MODEL)
        await update.message.reply_text(f"已恢复默认模型：{CLAUDE_MODEL}")
        return

    try:
        models = await fetch_models()
        if models and requested not in models:
            await update.message.reply_text(
                "没有在 /v1/models 中找到这个模型。先发送 /models 查看可用模型。"
            )
            return
    except Exception:
        pass

    set_setting(chat_id, "model", requested)
    histories[chat_id].clear()
    await update.message.reply_text(f"已切换模型：{requested}\n并清空了短期上下文。")


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    try:
        models = await fetch_models()
    except Exception as exc:
        await update.message.reply_text(f"读取模型列表失败：{str(exc)[:1200]}")
        return
    preferred = [m for m in models if any(k in m.lower() for k in ("claude", "fable", "kiro"))]
    shown = preferred if preferred else models
    shown = shown[:60]
    if not shown:
        await update.message.reply_text("模型列表为空。")
        return
    await safe_reply(update, "可用模型：\n" + "\n".join(f"`{m}`" for m in shown))


async def worldbooks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    enabled = set(enabled_worldbooks(chat_id))
    lines = ["已安装世界书："]
    for name, book in WORLD_BOOKS.items():
        mark = "✅" if name in enabled else "⬜"
        lines.append(f"{mark} {name} — {book['title']}")
    lines.append("\n开关示例：/world off 珍藏欲  或  /world on 年上char")
    await safe_reply(update, "\n".join(lines))


async def world_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    if len(context.args) < 2:
        await update.message.reply_text("用法：/world on 世界书名 或 /world off 世界书名\n发送 /worldbooks 查看名称。")
        return
    action = context.args[0].lower()
    name = " ".join(context.args[1:]).strip()
    if name not in WORLD_BOOKS:
        await update.message.reply_text("没有找到这个世界书。发送 /worldbooks 查看已安装列表。")
        return
    books = enabled_worldbooks(chat_id)
    if action == "on":
        if name not in books:
            books.append(name)
        save_worldbooks(chat_id, books)
        histories[chat_id].clear()
        await update.message.reply_text(f"已启用世界书：{name} ✅")
    elif action == "off":
        books = [item for item in books if item != name]
        save_worldbooks(chat_id, books)
        histories[chat_id].clear()
        await update.message.reply_text(f"已关闭世界书：{name}")
    else:
        await update.message.reply_text("用法：/world on 世界书名 或 /world off 世界书名")


async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("用法：/remember 需要长期记住的内容")
        return
    if len(text) > 1500:
        await update.message.reply_text("单条记忆请控制在 1500 字以内。")
        return
    memory_id = add_memory(chat_id, text)
    await update.message.reply_text(f"已记住 ✅ 记忆编号 #{memory_id}")


async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    rows = list_memories(chat_id, 30)
    if not rows:
        await update.message.reply_text("目前没有长期记忆。")
        return
    text = "长期记忆：\n" + "\n".join(f"#{row['id']}  {row['text']}" for row in rows)
    await safe_reply(update, text)


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    if not context.args:
        await update.message.reply_text("用法：/forget 记忆编号，例如 /forget 3")
        return
    raw = context.args[0].lstrip("#")
    try:
        memory_id = int(raw)
    except ValueError:
        await update.message.reply_text("记忆编号必须是数字。")
        return
    if delete_memory(chat_id, memory_id):
        await update.message.reply_text(f"已删除记忆 #{memory_id}。")
    else:
        await update.message.reply_text("没有找到这条记忆。")


async def forgetall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    clear_memories(chat_id)
    await update.message.reply_text("长期记忆已全部清空。")


async def stream_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    if not context.args:
        state = "开启" if stream_preferences[chat_id] else "关闭"
        await update.message.reply_text(f"当前流式输出：{state}\n用法：/stream on 或 /stream off")
        return
    value = context.args[0].lower()
    if value in ("on", "1", "true", "开", "开启"):
        stream_preferences[chat_id] = True
        await update.message.reply_text("流式输出已开启。")
    elif value in ("off", "0", "false", "关", "关闭"):
        stream_preferences[chat_id] = False
        await update.message.reply_text("流式输出已关闭。")
    else:
        await update.message.reply_text("用法：/stream on 或 /stream off")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先使用 /auth 访问密码 认证。")
        return
    user_text = update.message.text.strip()
    if not user_text:
        return
    entry = {"role": "user", "content": user_text}
    await generate_and_send(update, context, entry, entry)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    raw = bytes(await tg_file.download_as_bytearray())
    encoded = base64.b64encode(raw).decode("ascii")
    prompt = (update.message.caption or "请分析这张图片。").strip()
    request_entry = {
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            },
        ],
    }
    history_entry = {"role": "user", "content": f"[用户发送了一张图片] {prompt}"}
    await generate_and_send(update, context, request_entry, history_entry)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("请先认证。")
        return

    media = update.message.voice or update.message.audio
    if media is None:
        return
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    tg_file = await media.get_file()
    raw = bytes(await tg_file.download_as_bytearray())
    mime = getattr(media, "mime_type", None) or "audio/ogg"
    filename = getattr(media, "file_name", None) or "voice.ogg"
    try:
        transcript = await transcribe_audio(raw, filename, mime)
    except Exception as exc:
        await update.message.reply_text(
            "语音转文字暂时不可用。中转站可能没有开放 /v1/audio/transcriptions。\n"
            f"错误：{str(exc)[:1200]}"
        )
        return
    await update.message.reply_text(f"🎙 识别到：{transcript}")
    entry = {"role": "user", "content": transcript}
    await generate_and_send(update, context, entry, entry)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f"Telegram handler error: {context.error!r}", flush=True)


async def post_init(application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "启动机器人"),
            BotCommand("auth", "输入访问密码认证"),
            BotCommand("reset", "清空短期上下文"),
            BotCommand("model", "查看或切换模型"),
            BotCommand("models", "查看可用模型"),
            BotCommand("worldbooks", "查看已安装世界书"),
            BotCommand("world", "开关世界书"),
            BotCommand("remember", "添加长期记忆"),
            BotCommand("memories", "查看长期记忆"),
            BotCommand("forget", "删除一条长期记忆"),
            BotCommand("forgetall", "清空长期记忆"),
            BotCommand("stream", "开关流式输出"),
            BotCommand("whoami", "查看 Telegram ID"),
            BotCommand("help", "查看帮助"),
        ]
    )


def main() -> None:
    require_config()
    init_db()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("auth", auth))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("models", models_command))
    app.add_handler(CommandHandler("worldbooks", worldbooks_command))
    app.add_handler(CommandHandler("world", world_command))
    app.add_handler(CommandHandler("remember", remember_command))
    app.add_handler(CommandHandler("memories", memories_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("forgetall", forgetall_command))
    app.add_handler(CommandHandler("stream", stream_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    print(
        f"Claude Telegram Bot starting with default model={CLAUDE_MODEL}, "
        f"memory_db={MEMORY_DB_PATH}, worldbooks={DEFAULT_WORLD_BOOKS}",
        flush=True,
    )
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
