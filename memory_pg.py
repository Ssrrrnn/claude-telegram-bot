import asyncio
import json
import os
import re
import time
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
MEMORY_MODEL = os.environ.get("MEMORY_MODEL", "claude-sonnet-5").strip()
LLMHUB_BASE_URL = os.environ.get("LLMHUB_BASE_URL", "https://llmhub.uk/v1").rstrip("/")
LLMHUB_API_KEY = os.environ.get("LLMHUB_API_KEY", "").strip()
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "180"))


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db(retries: int = 12) -> None:
    last_exc = None
    for attempt in range(retries):
        try:
            with _connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS bot_settings (
                            chat_id BIGINT NOT NULL,
                            key TEXT NOT NULL,
                            value TEXT NOT NULL,
                            PRIMARY KEY (chat_id, key)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS memories (
                            id BIGSERIAL PRIMARY KEY,
                            chat_id BIGINT NOT NULL,
                            kind TEXT NOT NULL DEFAULT 'fact',
                            text TEXT NOT NULL,
                            importance SMALLINT NOT NULL DEFAULT 1,
                            source_message_id BIGINT,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memories_chat_created
                        ON memories(chat_id, created_at DESC)
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS relationship_state (
                            chat_id BIGINT PRIMARY KEY,
                            summary TEXT NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS favorites (
                            id BIGSERIAL PRIMARY KEY,
                            chat_id BIGINT NOT NULL,
                            telegram_message_id BIGINT NOT NULL,
                            text TEXT NOT NULL,
                            note TEXT,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            UNIQUE(chat_id, telegram_message_id)
                        )
                        """
                    )
                conn.commit()
            return
        except Exception as exc:
            last_exc = exc
            if attempt == retries - 1:
                break
            time.sleep(min(2 + attempt, 8))
    raise RuntimeError(f"Postgres initialization failed: {last_exc}")


def set_setting(chat_id: int, key: str, value: str) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_settings(chat_id, key, value)
                VALUES (%s, %s, %s)
                ON CONFLICT(chat_id, key)
                DO UPDATE SET value = EXCLUDED.value
                """,
                (chat_id, key, value),
            )
        conn.commit()


def get_setting(chat_id: int, key: str) -> str | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM bot_settings WHERE chat_id = %s AND key = %s",
                (chat_id, key),
            )
            row = cur.fetchone()
    return str(row["value"]) if row else None


def add_memory(
    chat_id: int,
    text: str,
    kind: str = "manual",
    importance: int = 3,
    source_message_id: int | None = None,
) -> int:
    text = text.strip()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories(chat_id, kind, text, importance, source_message_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (chat_id, kind, text, max(0, min(int(importance), 3)), source_message_id),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row["id"])


def list_memories(chat_id: int, limit: int = 30) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, kind, text, importance, source_message_id,
                       EXTRACT(EPOCH FROM created_at)::BIGINT AS created_at
                FROM memories
                WHERE chat_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (chat_id, limit),
            )
            rows = cur.fetchall()
    return list(reversed(rows))


def delete_memory(chat_id: int, memory_id: int) -> bool:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM memories WHERE chat_id = %s AND id = %s",
                (chat_id, memory_id),
            )
            changed = cur.rowcount > 0
        conn.commit()
    return changed


def clear_memories(chat_id: int) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE chat_id = %s", (chat_id,))
        conn.commit()


def get_relationship(chat_id: int) -> str | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT summary FROM relationship_state WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
    return str(row["summary"]) if row else None


def set_relationship(chat_id: int, summary: str) -> None:
    summary = summary.strip()[:3000]
    if not summary:
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO relationship_state(chat_id, summary, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT(chat_id)
                DO UPDATE SET summary = EXCLUDED.summary, updated_at = NOW()
                """,
                (chat_id, summary),
            )
        conn.commit()


def add_favorite(
    chat_id: int,
    telegram_message_id: int,
    text: str,
    note: str | None = None,
) -> int | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO favorites(chat_id, telegram_message_id, text, note)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(chat_id, telegram_message_id) DO NOTHING
                RETURNING id
                """,
                (chat_id, telegram_message_id, text[:5000], (note or "")[:500] or None),
            )
            row = cur.fetchone()
        conn.commit()
    return int(row["id"]) if row else None


def list_favorites(chat_id: int, limit: int = 20, query: str = "") -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cur:
            if query:
                like = f"%{query}%"
                cur.execute(
                    """
                    SELECT id, telegram_message_id, text, note,
                           EXTRACT(EPOCH FROM created_at)::BIGINT AS created_at
                    FROM favorites
                    WHERE chat_id = %s AND (text ILIKE %s OR COALESCE(note, '') ILIKE %s)
                    ORDER BY id DESC LIMIT %s
                    """,
                    (chat_id, like, like, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, telegram_message_id, text, note,
                           EXTRACT(EPOCH FROM created_at)::BIGINT AS created_at
                    FROM favorites
                    WHERE chat_id = %s
                    ORDER BY id DESC LIMIT %s
                    """,
                    (chat_id, limit),
                )
            rows = cur.fetchall()
    return rows


def delete_favorite(chat_id: int, favorite_id: int) -> bool:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM favorites WHERE chat_id = %s AND id = %s",
                (chat_id, favorite_id),
            )
            changed = cur.rowcount > 0
        conn.commit()
    return changed


def _tokens(text: str) -> set[str]:
    text = text.lower()
    ascii_words = set(re.findall(r"[a-z0-9_]{2,}", text))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    grams = {cjk[i : i + 2] for i in range(max(0, len(cjk) - 1))}
    chars = set(cjk) if len(cjk) < 6 else set()
    return ascii_words | grams | chars


def relevant_memories(chat_id: int, query: str, limit: int = 14) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, kind, text, importance,
                       EXTRACT(EPOCH FROM created_at)::BIGINT AS created_at
                FROM memories WHERE chat_id = %s
                ORDER BY id DESC LIMIT 250
                """,
                (chat_id,),
            )
            rows = cur.fetchall()
    if not rows:
        return []
    q = _tokens(query)
    now = time.time()
    scored = []
    for row in rows:
        t = _tokens(str(row["text"]))
        overlap = len(q & t)
        age_days = max(0.0, (now - int(row["created_at"])) / 86400)
        recency = max(0.0, 2.0 - age_days / 30.0)
        score = overlap * 4.0 + int(row["importance"]) * 2.5 + recency
        scored.append((score, row))
    scored.sort(key=lambda x: (x[0], x[1]["id"]), reverse=True)
    selected = [row for score, row in scored[:limit] if score >= 2.0]
    return list(reversed(selected))


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _analyzer_call(messages: list[dict[str, str]]) -> dict[str, Any] | None:
    if not LLMHUB_API_KEY:
        return None
    payload = {"model": MEMORY_MODEL, "messages": messages, "stream": False}
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{LLMHUB_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLMHUB_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code >= 400:
        return None
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content", "")
    if not isinstance(content, str):
        return None
    return _extract_json(content)


async def analyze_and_store(
    chat_id: int,
    telegram_message_id: int,
    user_text: str,
    assistant_text: str,
    recent_history: list[dict[str, Any]],
) -> None:
    user_text = (user_text or "").strip()
    if not user_text or user_text.startswith("/"):
        return
    current_relationship = get_relationship(chat_id) or "尚无稳定关系摘要。"
    compact_history = []
    for item in recent_history[-10:]:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            compact_history.append({"role": role, "content": content[:1200]})

    system = """你是聊天应用后台的记忆整理器，不参与角色扮演正文。只返回严格 JSON，不要 Markdown，不要解释，不要推理过程。
目标：从当前一轮聊天里提取值得跨天、跨会话保留的信息，并维护关系状态；同时判断用户当前消息是否值得由角色真实收藏。

记忆规则：
- 只保存未来有用、稳定或具有纪念意义的信息：用户偏好/雷点/身份信息、固定称呼、重要约定、关系进展、矛盾和和好、重要事件、反复出现的习惯。
- 不保存普通寒暄、一次性问题、系统/命令/世界书元信息。
- 每轮最多 3 条 memory；尽量写成独立、简洁、以后读得懂的事实。
- kind 仅允许 fact、preference、promise、event、relationship。
- importance 0-3，3 表示长期关键。

关系状态：relationship_state 返回“完整更新后的关系摘要”，不是只写本轮变化。应简洁记录关系阶段、当前情绪余波、重要共同经历、固定称呼/相处模式；如果本轮没有任何值得改变的内容，返回 null。

收藏规则：
- favorite 只针对用户当前这条原消息。只有它让角色真心喜欢、心软、心动、觉得可爱、感到被偏爱/信任、构成重要约定或纪念瞬间时才为 true。
- 不要机械收藏普通甜言蜜语；场外操作、测试、系统信息不收藏。
- note 是一句很短的角色私人心情，不写动作、不解释规则；favorite=false 时 note 必须为 null。

JSON 格式：
{"memories":[{"kind":"fact","text":"...","importance":2}],"relationship_state":"...或null","favorite":false,"note":null}"""

    user_payload = {
        "current_relationship": current_relationship,
        "recent_history": compact_history,
        "current_user_message": user_text[:4000],
        "assistant_reply": (assistant_text or "")[:4000],
    }
    result = await _analyzer_call(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
    )
    if not result:
        return

    memories = result.get("memories")
    if isinstance(memories, list):
        for item in memories[:3]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            kind = str(item.get("kind") or "fact").strip().lower()
            if kind not in {"fact", "preference", "promise", "event", "relationship"}:
                kind = "fact"
            try:
                importance = int(item.get("importance", 1))
            except Exception:
                importance = 1
            if 3 <= len(text) <= 1200:
                add_memory(
                    chat_id,
                    text,
                    kind=kind,
                    importance=max(0, min(importance, 3)),
                    source_message_id=telegram_message_id,
                )

    relationship = result.get("relationship_state")
    if isinstance(relationship, str) and relationship.strip().lower() not in {"null", "none"}:
        set_relationship(chat_id, relationship)

    if result.get("favorite") is True:
        note = result.get("note")
        if note is not None:
            note = str(note).strip()[:500]
        add_favorite(chat_id, telegram_message_id, user_text, note)
