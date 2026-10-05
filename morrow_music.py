"""Public catalog lookup and real in-chat music shares, without account cookies."""
import json
import re
import aiohttp
from urllib.parse import urlencode
import phone_bridge as bridge


async def fetch_catalog(query):
    from morrow_services import PublicResolver
    url = 'https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg?' + urlencode({
        'key': query, 'format': 'json', 'platform': 'yqq.json', 'inCharset': 'utf8', 'outCharset': 'utf-8',
        'g_tk': '5381', 'loginUin': '0', 'hostUin': '0', 'notice': '0', 'needNewCode': '0'})
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver()),
            timeout=aiohttp.ClientTimeout(total=10), headers={'Referer': 'https://y.qq.com/',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15'}) as client:
        async with client.get(url, allow_redirects=False) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                data.extend(chunk)
                if len(data) > 1_000_000:
                    raise bridge.BridgeError('音乐目录返回过大。')
            return json.loads(data)


def catalog_items(data):
    if not isinstance(data, dict) or data.get('code') not in (0, '0'):
        raise bridge.BridgeError('这次没有取得音乐搜索结果。')
    rows = ((data.get('data') or {}).get('song') or {}).get('itemlist') or []
    output, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid, title = str(row.get('mid') or ''), row.get('name')
        if not re.fullmatch(r'[A-Za-z0-9]{1,40}', mid) or not isinstance(title, str) or not title.strip() or mid in seen:
            continue
        seen.add(mid)
        # Escape presentation punctuation rather than accept arbitrary model URLs.
        clean = lambda value: re.sub(r'[\[\]()\r\n]', ' ', str(value or '')).strip()[:200]
        title, artist = clean(title), clean(row.get('singer'))
        url = 'https://y.qq.com/n/ryqq/songDetail/' + mid
        label = title + (' · ' + artist if artist else '')
        output.append({'song_mid': mid, 'title': title, 'artist': artist, 'url': url,
                       'markdown': '[' + label + '](' + url + ')'})
        if len(output) == 5:
            break
    return output


async def execute(name, args, turn):
    if not isinstance(args, dict):
        raise ValueError('Invalid music arguments')
    if name == 'search_music':
        if set(args) != {'query'} or not isinstance(args['query'], str) or not 1 <= len(args['query'].strip()) <= 100:
            raise ValueError('请用歌名或歌手搜索音乐。')
        try:
            items = catalog_items(await fetch_catalog(args['query'].strip()))
        except Exception as exc:
            raise bridge.BridgeError('音乐目录暂时没有查询成功，不能当成已找到歌曲。') from exc
        if turn is not None:
            turn.setdefault('music_candidates', {}).update({x['song_mid']: x for x in items})
        return {'items': items, 'notice': '这些是实际目录结果。需要分享时调用 share_music；不能声称自己已播放，会员权限由用户手机上的音乐账号确认。'}
    if name == 'share_music':
        if set(args) != {'song_mid'} or not isinstance(args['song_mid'], str) or turn is None:
            raise ValueError('音乐分享需要当前聊天回合。')
        song = turn.get('music_candidates', {}).get(args['song_mid'])
        if song is None:
            raise bridge.BridgeError('先用 search_music 查到实际歌曲，再分享搜索结果中的 song_mid。')
        turn.setdefault('music_shares', {})[song['song_mid']] = song
        return {'status': 'prepared', 'song': song, 'notice': '已为本轮回复准备音乐卡片，回复结束时附上。自然说分享缘由，不输出参数，也不要假装已经听过。'}
    raise ValueError('Unknown music tool')


def attach_shares(text, turn):
    links = [x['markdown'] for x in turn.get('music_shares', {}).values() if x['url'] not in text]
    return '\n'.join([text, *links]).strip()
