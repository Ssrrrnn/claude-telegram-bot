"""Explicit owner approval for publishing a text post to the bot account's Qzone."""

import json
import re
import time

PENDING_KEY = 'qq_qzone_pending_v1'
LAST_CONFIRM_KEY = 'qq_qzone_last_confirm_v1'
PENDING_SECONDS = 10 * 60
MAX_POST_CHARS = 300


def command(text):
    value = text.strip()
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
