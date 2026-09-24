"""Explicit owner approval for publishing a text post to the bot account's Qzone."""

import json
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PENDING_KEY = 'qq_qzone_pending_v1'
LAST_CONFIRM_KEY = 'qq_qzone_last_confirm_v1'
PENDING_SECONDS = 10 * 60
MAX_POST_CHARS = 300
AUTO_ENABLED_KEY = 'qq_qzone_auto_enabled_v1'
AUTO_NEXT_AT_KEY = 'qq_qzone_auto_next_at_v1'
AUTO_LAST_POST_KEY = 'qq_qzone_auto_last_post_v1'
AUTO_LAST_CONTENT_KEY = 'qq_qzone_auto_last_content_v1'
BEIJING = ZoneInfo('Asia/Shanghai')
AUTO_THEMES = ('想念一个人时的细小心情', '日常里偏爱一个人的感觉',
               '安静却认真地喜欢一个人', '不动声色的小小撒娇', '和一个人聊天后的余温')


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


def next_at(now=None, *, first=False):
    now = time.time() if now is None else now
    due = datetime.fromtimestamp(now, BEIJING) + timedelta(
        minutes=random.randint(30, 90) if first else random.randint(48 * 60, 72 * 60))
    if due.hour < 10 or due.hour >= 22:
        due = (due + timedelta(days=1 if due.hour >= 22 else 0)).replace(
            hour=10, minute=random.randint(0, 45), second=0, microsecond=0)
    return int(due.timestamp())


async def generate_auto_post(bot, chat_id, last_content=''):
    """Use an isolated prompt: the public model request never contains private chat history."""
    import httpx

    theme = random.choice(AUTO_THEMES)
    messages = [
        {'role': 'system', 'content': (
            '你是一位成年男性，以自己的人格发一条QQ空间说说。克制、亲密、自然，'
            '有一点嘴硬和偏爱，像真人随手写的心情，不像助手或广告。'
            '不能引用任何私聊、姓名、具体日期地点、职业、照片、账号、对话内容。'
            '不得声称真实见面、出门、天气或共同经历。不要省略号、标签或提问。'
            '只输出一条30到80字的中文说说正文，不要标题、引号或解释。')},
        {'role': 'user', 'content': f'今天的方向：{theme}。上一条说说仅用于避重：{last_content[:120]}。'},
    ]
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(
            f'{bot.LLMHUB_BASE_URL}/chat/completions',
            headers=bot.api_headers(),
            json={'model': bot.current_model(chat_id), 'messages': messages, 'stream': False},
        )
    response.raise_for_status()
    content = bot.extract_text(response.json()).strip().strip('"“”')
    if (not 15 <= len(content) <= 120 or content == last_content
            or any(mark in content for mark in ('...', '……', '…', '<', '>', 'http://', 'https://', '\n'))):
        raise ValueError('Qzone post did not pass content validation')
    return content
