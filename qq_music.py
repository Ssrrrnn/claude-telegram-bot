"""Recognize owner supplied song links for native OneBot music cards."""

import re
from urllib.parse import parse_qs, urlsplit


def card(text):
    if not re.search(r"(?:发|分享|来|给我|给你).{0,12}(?:歌|音乐|歌曲)|(?:歌|音乐|歌曲).{0,12}(?:卡片|分享|发)", text):
        return None
    for raw in re.findall(r"https?://[^\s<>，。！？]+", text):
        url = urlsplit(raw.rstrip(')]}'))
        host = (url.hostname or '').lower()
        if url.scheme != 'https' or url.username or url.port not in (None, 443):
            continue
        if host in ('music.163.com', 'y.music.163.com'):
            path = url.path + (url.fragment.split('?', 1)[0] if url.fragment else '')
            ids = parse_qs(url.query).get('id') or parse_qs(url.fragment.split('?', 1)[-1]).get('id')
            if ('song' in path or url.path == '/song') and ids and ids[0].isdigit() and len(ids[0]) <= 16:
                return {'type': 'music', 'data': {'type': '163', 'id': ids[0]}}
        if host == 'y.qq.com':
            match = re.fullmatch(r'/n/ryqq/songDetail/([A-Za-z0-9]{5,32})/?', url.path)
            if match:
                return {'type': 'music', 'data': {'type': 'qq', 'id': match.group(1)}}
    return None
