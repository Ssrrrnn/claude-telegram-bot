import asyncio
import json
import os
import re
import time
from difflib import SequenceMatcher
from typing import Any

import httpx
import psycopg
import continuity
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
MEMORY_MODEL = os.environ.get("MEMORY_MODEL", "claude-sonnet-5").strip()
LLMHUB_BASE_URL = os.environ.get("LLMHUB_BASE_URL", "https://llmhub.uk/v1").rstrip("/")
LLMHUB_API_KEY = os.environ.get("LLMHUB_API_KEY", "").strip()
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "180"))
AUTO_MEMORY_COOLDOWN_SECONDS = int(os.environ.get("AUTO_MEMORY_COOLDOWN_SECONDS", "7200"))


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def mutate_continuity(chat_id, operation):
    """Serialize state changes with a row lock, including overlapping deploys."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO bot_settings(chat_id,key,value) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (chat_id, continuity.KEY, json.dumps(continuity.empty())))
            cur.execute("SELECT value FROM bot_settings WHERE chat_id=%s AND key=%s FOR UPDATE", (chat_id, continuity.KEY))
            state = operation(continuity.decode(cur.fetchone()["value"]))
            cur.execute("UPDATE bot_settings SET value=%s WHERE chat_id=%s AND key=%s",
                        (json.dumps(state, ensure_ascii=False), chat_id, continuity.KEY))
        conn.commit()
    return state


def correct_memory(chat_id, memory_id, replacement=None):
    """Update exactly one owned memory and record its superseded text atomically."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO bot_settings(chat_id,key,value) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (chat_id, continuity.KEY, json.dumps(continuity.empty())))
            cur.execute("SELECT value FROM bot_settings WHERE chat_id=%s AND key=%s FOR UPDATE", (chat_id, continuity.KEY))
            state = continuity.decode(cur.fetchone()["value"])
            cur.execute("SELECT text FROM memories WHERE chat_id=%s AND id=%s FOR UPDATE", (chat_id, memory_id))
            row = cur.fetchone()
            if not row:
                return False
            state["revision"] = -int(time.time() * 1000)
            state["corrections"] = [*state["corrections"], {"memory_id": memory_id, "old": row["text"],
                "replacement": replacement, "at": time.time()}][-100:]
            if replacement is None:
                cur.execute("DELETE FROM memories WHERE chat_id=%s AND id=%s", (chat_id, memory_id))
            else:
                cur.execute("UPDATE memories SET text=%s, kind='manual', source_message_id=NULL WHERE chat_id=%s AND id=%s",
                            (replacement, chat_id, memory_id))
            cur.execute("UPDATE bot_settings SET value=%s WHERE chat_id=%s AND key=%s",
                        (json.dumps(state, ensure_ascii=False), chat_id, continuity.KEY))
        conn.commit()
    return True


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
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS recent_conversation (
                            id BIGSERIAL PRIMARY KEY,
                            chat_id BIGINT NOT NULL,
                            telegram_message_id BIGINT,
                            user_text TEXT,
                            assistant_text TEXT NOT NULL,
                            kind TEXT NOT NULL DEFAULT 'chat',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            UNIQUE(chat_id, telegram_message_id)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_recent_conversation_chat_id
                        ON recent_conversation(chat_id, id DESC)
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


def save_recent_turn(
    chat_id: int,
    telegram_message_id: int,
    user_text: str,
    assistant_text: str,
) -> None:
    if not user_text.strip() or not assistant_text.strip():
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recent_conversation(
                    chat_id, telegram_message_id, user_text, assistant_text, kind
                ) VALUES (%s, %s, %s, %s, 'chat')
                ON CONFLICT(chat_id, telegram_message_id)
                DO UPDATE SET
                    user_text = EXCLUDED.user_text,
                    assistant_text = EXCLUDED.assistant_text
                """,
                (chat_id, telegram_message_id, user_text, assistant_text),
            )
            _trim_recent_conversation(cur, chat_id)
        conn.commit()


def save_recent_proactive(chat_id: int, assistant_text: str) -> None:
    if not assistant_text.strip():
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recent_conversation(
                    chat_id, telegram_message_id, user_text, assistant_text, kind
                ) VALUES (%s, NULL, NULL, %s, 'proactive')
                """,
                (chat_id, assistant_text),
            )
            _trim_recent_conversation(cur, chat_id)
        conn.commit()


def _trim_recent_conversation(cur, chat_id: int, keep: int = 80) -> None:
    cur.execute(
        """
        DELETE FROM recent_conversation
        WHERE chat_id = %s
          AND id NOT IN (
              SELECT id FROM recent_conversation
              WHERE chat_id = %s
              ORDER BY id DESC
              LIMIT %s
          )
        """,
        (chat_id, chat_id, keep),
    )


def load_recent_messages(chat_id: int, limit_messages: int = 40) -> list[dict[str, str]]:
    turn_limit = max(1, min(80, limit_messages))
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_text, assistant_text
                FROM recent_conversation
                WHERE chat_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (chat_id, turn_limit),
            )
            rows = list(reversed(cur.fetchall()))

    messages: list[dict[str, str]] = []
    for row in rows:
        user_text = row.get("user_text")
        if isinstance(user_text, str) and user_text.strip():
            messages.append({"role": "user", "content": user_text})
        assistant_text = row.get("assistant_text")
        if isinstance(assistant_text, str) and assistant_text.strip():
            messages.append({"role": "assistant", "content": assistant_text})
    return messages[-limit_messages:]


def clear_recent_messages(chat_id: int) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM recent_conversation WHERE chat_id = %s", (chat_id,))
        conn.commit()


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
    return correct_memory(chat_id, memory_id)


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


def _normalize_memory_text(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.lower())


def _is_duplicate_memory(chat_id: int, text: str, limit: int = 160) -> bool:
    candidate = _normalize_memory_text(text)
    if not candidate:
        return True
    candidate_tokens = _tokens(text)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT text FROM memories
                WHERE chat_id = %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (chat_id, limit),
            )
            rows = cur.fetchall()

    for row in rows:
        existing_text = str(row["text"])
        existing = _normalize_memory_text(existing_text)
        if not existing:
            continue
        if candidate == existing:
            return True
        shorter, longer = sorted((candidate, existing), key=len)
        if len(shorter) >= 8 and shorter in longer and len(shorter) / max(1, len(longer)) >= 0.72:
            return True
        if SequenceMatcher(None, candidate, existing).ratio() >= 0.82:
            return True
        existing_tokens = _tokens(existing_text)
        if candidate_tokens and existing_tokens:
            union = candidate_tokens | existing_tokens
            if union and len(candidate_tokens & existing_tokens) / len(union) >= 0.72:
                return True
    return False


def _seconds_since_last_auto_memory(chat_id: int) -> float | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_seconds
                FROM memories
                WHERE chat_id = %s AND kind <> 'manual'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (chat_id,),
            )
            row = cur.fetchone()
    if not row or row["age_seconds"] is None:
        return None
    return float(row["age_seconds"])


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
    allow_long_term: bool = True,
) -> None:
    user_text = (user_text or "").strip()
    if not user_text or user_text.startswith("/"):
        return
    current_relationship = get_relationship(chat_id) or "尚无稳定关系摘要。"
    current_continuity = continuity.decode(get_setting(chat_id, continuity.KEY))
    compact_history = []
    for item in recent_history[-10:]:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            compact_history.append({"role": role, "content": content[:1200]})

    system = """你是聊天应用后台的记忆整理器，不参与角色扮演正文。只返回严格 JSON，不要 Markdown，不要解释，不要推理过程。
目标：极其克制地保存真正值得跨天、跨会话甚至数周后继续使用的信息，并维护关系状态；同时判断用户当前消息是否值得由角色真实收藏。

长期记忆规则（必须严格）：
- 默认不要新增 memory。只有这条信息在几天或几周后仍会明显影响角色如何回应用户时，才考虑保存。
- 每轮最多 1 条 memory，绝大多数普通聊天应返回空数组 []。
- 只保存稳定且高价值的信息：明确且长期的用户偏好/雷点、稳定身份事实、固定称呼、重要且持续有效的约定、重大关系进展、重要共同事件、反复出现且已形成稳定模式的习惯。
- 不保存：普通寒暄、撒娇、调情、一次性的情绪、当下动作、临时计划、普通甜言蜜语、一次性问题、当天琐事、角色自己的即时感受、已经写进 relationship_state 的短期关系情绪、系统/命令/世界书/模型/测试等元信息。
- 不要因为“听起来有感情”就保存；不要推断用户没明确表达的事实。
- 如果和已有长期记忆只是同义改写、补充很小的细节或重复确认，返回空数组，不要新增。
- kind 仅允许 fact、preference、promise、event、relationship。
- importance 只使用 2 或 3：2=稳定且未来有用，3=长期关键/重大里程碑。不要输出 0 或 1。
- 拿不准就不记。

关系状态：relationship_state 返回“完整更新后的关系摘要”，不是只写本轮变化。它可以比长期 memory 更灵活地承接当前关系阶段、情绪余波和相处模式。若本轮没有值得改变关系摘要的内容，返回 null。不要为了更新而更新。

收藏规则：
- favorite 只针对用户当前这条原消息。只有它让角色真心喜欢、心软、心动、觉得可爱、感到被偏爱/信任、构成重要约定或纪念瞬间时才为 true。
- 不要机械收藏普通甜言蜜语；场外操作、测试、系统信息不收藏。
- note 是一句很短的角色私人心情，不写动作、不解释规则；favorite=false 时 note 必须为 null。

JSON 格式：
{"memories":[{"kind":"fact","text":"...","importance":2}],"relationship_state":"...或null","favorite":false,"note":null}"""

    system += """\n额外输出 continuity 对象，用于24小时内的对话续接，不属于长期记忆。
格式：{"topic":"当前正在延续的话题与情绪，简短且只写明确事实", "threads":[{"action":"open或done或cancelled", "id":"更新旧事项时使用已存在id，新事项留空", "title":"具体未完事项或明确承诺", "purpose":"晚点为什么值得回访", "category":"work或other", "subject":"user或assistant或shared", "realm":"real或roleplay", "evidence":"必须逐字引用当前用户消息或当前assistant_reply中的一段原话，至少4字", "delay_minutes":90}]}
只记录有明确依据且尚未完成的事项，不把寒暄、情话、泛泛的陪伴承诺当待办。
用户表示完成、取消、下班或不再需要时关闭相关旧事项；完成证据必须出自用户当前原话。
用户简短回复‘好了’‘解决了’时，可引用整条短消息作为完成证据，但必须能从上下文确定具体旧事项id。
相同事项沿用id，不创建同义重复。不会执行闹钟和工具任务，这些只是对话回访线索。
现实与角色剧情分开，不把角色自述当用户事实。已完成事项不要重新打开。
临时情绪写topic，不写长期memories。current_continuity中的纠错优先于历史内容，禁止重新存回作废内容。
allow_long_term=false时memories必须为[]、relationship_state为null，但仍可整理continuity与判断favorite。"""
    user_payload = {
        "current_continuity": current_continuity,
        "allow_long_term": allow_long_term,
        "current_relationship": current_relationship,
        "recent_history": compact_history,
        "current_user_message": user_text[:4000],
        "assistant_reply": (assistant_text or "")[:4000],
    }
    result = await asyncio.wait_for(_analyzer_call(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
    ), timeout=20)
    if not result:
        return

    applied = False
    def update_state(state):
        nonlocal applied
        if state["revision"] != telegram_message_id:
            return state
        applied = True
        return continuity.apply_analysis(state, result.get("continuity"), user_text, assistant_text,
                                         telegram_message_id, time.time())
    current_continuity = mutate_continuity(chat_id, update_state)
    if not applied:
        return  # A newer message/correction arrived while the analyzer ran.

    memories = result.get("memories")
    if allow_long_term and isinstance(memories, list):
        for item in memories[:1]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if continuity.blocked_memory(current_continuity, text):
                continue
            kind = str(item.get("kind") or "fact").strip().lower()
            if kind not in {"fact", "preference", "promise", "event", "relationship"}:
                kind = "fact"
            try:
                importance = int(item.get("importance", 2))
            except Exception:
                importance = 2

            if importance < 2 or not (6 <= len(text) <= 900):
                continue
            importance = min(importance, 3)

            if _is_duplicate_memory(chat_id, text):
                continue

            age = _seconds_since_last_auto_memory(chat_id)
            if importance < 3 and age is not None and age < AUTO_MEMORY_COOLDOWN_SECONDS:
                continue

            add_memory(
                chat_id,
                text,
                kind=kind,
                importance=importance,
                source_message_id=telegram_message_id,
            )

    relationship = result.get("relationship_state")
    if allow_long_term and isinstance(relationship, str) and relationship.strip().lower() not in {"null", "none"} and not continuity.blocked_memory(current_continuity, relationship):
        set_relationship(chat_id, relationship)

    if result.get("favorite") is True:
        note = result.get("note")
        if note is not None:
            note = str(note).strip()[:500]
        add_favorite(chat_id, telegram_message_id, user_text, note)
