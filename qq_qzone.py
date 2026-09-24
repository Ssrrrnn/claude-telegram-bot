"""Owner controls and isolated generation for the bot account's Qzone posts."""

import json
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

PENDING_KEY = 'qq_qzone_pending_v1'
LAST_CONFIRM_KEY = 'qq_qzone_last_confirm_v1'
PENDING_SECONDS = 10 * 60
MAX_POST_CHARS = 300
AUTO_ENABLED_KEY = 'qq_qzone_auto_enabled_v1'
AUTO_NEXT_THOUGHT_KEY = 'qq_qzone_auto_next_thought_v2'
AUTO_LAST_POST_KEY = 'qq_qzone_auto_last_post_v1'
AUTO_LAST_CONTENT_KEY = 'qq_qzone_auto_last_content_v1'
BEIJING = ZoneInfo('Asia/Shanghai')


def command(text):
    value = text.strip()
    if value in ('开启自动空间说说', '关闭自动空间说说', '自动空间说说状态'):
        return {'开启自动空间说说': 'auto_on', '关闭自动空间说说': 'auto_off',
                '自动空间说说状态': 'auto_status'}[value], ''
    if value in ('确认发表空间说说', '取消发表空间说说'):
        return ('confirm' if value.startswith('确认') else 'cancel'), ''
    match = re.fullmatch(r'(?:发|发表)空间说说[：:]\s*(.+)', value, re.S)
    if match:
        return 'direct', match.group(1).strip()
    match = re.fullmatch(r'(?:帮我)?写(?:一条|条)?空间说说[：:]\s*(.+)', value, re.S)
    if match:
        return 'draft', match.group(1).strip()
    return None


def pending(raw, now=None):
    try:
        value = json.loads(raw) if raw else None
        now = time.time() if now is None else now
        if (isinstance(value, dict) and isinstance(value.get('content'), str)
                and 0 < len(value['content']) <= MAX_POST_CHARS
                and isinstance(value.get('created_at'), (int, float))
                and 0 <= now - value['created_at'] < PENDING_SECONDS):
            return value['content']
    except (TypeError, ValueError):
        pass
    return None


def encode(content, now=None):
    return json.dumps({'content': content, 'created_at': time.time() if now is None else now},
                      ensure_ascii=False)


def next_thought(now=None):
    """When to reflect again; this is not a limit on how often posting is allowed."""
    return int((time.time() if now is None else now) + random.randint(35, 85) * 60)


async def decide_auto_post(bot, chat_id, last_content='', last_post_at=0, *, cue=False):
    """Return a chosen post or None; the model sees no private chat history."""
    import httpx

    now = datetime.now(BEIJING)
    elapsed = ('还没有发过' if not last_post_at else
               f'{max(0, int(time.time() - last_post_at)) // 60}分钟前')
    messages = [
        {'role': 'system', 'content': (
            '你是一位成年男性，正在自己决定要不要发一条QQ空间说说。'
            '没有任务、额度或固定发表时间。没什么值得公开说的就只输出 NO_POST；'
            '有真实的表达冲动才写正文，不为了发而发，也不要连着重复相似心情。'
            '如果写，语气克制、亲密、自然，'
            '有一点嘴硬和偏爱，像真人随手写的心情，不像助手或广告。'
            '不能引用任何私聊、姓名、具体日期地点、职业、照片、账号、对话内容。'
            '不得声称真实见面、出门、天气或共同经历。不要省略号、标签或提问。'
            '要发表就只输出一条15到100字的中文说说正文，不要标题、引号或解释。')},
        {'role': 'user', 'content': (
            f'现在是北京时间 {now:%H:%M}，上一次发表：{elapsed}。'
            f'刚刚是否发生了值得沉淀的一次亲近交流：{"是" if cue else "没有新线索"}。'
            f'上一条说说仅用于避重：{last_content[:120]}。'
            '此刻由你决定：NO_POST，或者一条正文。')},
    ]
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(
            f'{bot.LLMHUB_BASE_URL}/chat/completions',
            headers=bot.api_headers(),
            json={'model': bot.current_model(chat_id), 'messages': messages, 'stream': False},
        )
    response.raise_for_status()
    content = bot.extract_text(response.json()).strip().strip('"“”')
    if content == 'NO_POST':
        return None
    if (not 15 <= len(content) <= 120 or content == last_content
            or any(mark in content for mark in ('...', '……', '…', '<', '>', 'http://', 'https://', '\n'))):
        raise ValueError('Qzone post did not pass content validation')
    return content
