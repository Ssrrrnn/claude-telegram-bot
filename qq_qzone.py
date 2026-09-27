"""Owner controls and isolated generation for the bot account's Qzone posts."""

import json
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

PENDING_KEY = 'qq_qzone_pending_v1'
LAST_CONFIRM_KEY = 'qq_qzone_last_confirm_v1'
LAST_REQUEST_KEY = 'qq_qzone_last_request_v1'
PENDING_SECONDS = 10 * 60
MAX_POST_CHARS = 300
AUTO_ENABLED_KEY = 'qq_qzone_auto_enabled_v1'
AUTO_NEXT_THOUGHT_KEY = 'qq_qzone_auto_next_thought_v2'
AUTO_LAST_POST_KEY = 'qq_qzone_auto_last_post_v1'
AUTO_LAST_CONTENT_KEY = 'qq_qzone_auto_last_content_v1'
LAST_TOPIC_AT_KEY = 'qq_qzone_last_topic_at_v1'
BEIJING = ZoneInfo('Asia/Shanghai')


def command(text):
    value = text.strip()
    if ('：' not in value and ':' not in value
            and re.search(r'(?:QQ空间|空间说说|发说说|发表说说|发表空间|空间发帖)', value) and re.search(
        r'(?:能不能|可不可以|会不会|有没有|有无|有.{0,8}权限|没有.{0,8}权限|怎么|如何|调用|接口|(?:能|会|可以).{0,12}[吗？?])', value,
    ) and not re.search(r'(?:别发|不要发|先别发)', value)):
        return 'help', ''
    if re.fullmatch(r'(?:你|老公)?(?:怎么|如何)(?:让你|教你|才能)?(?:发|发表)(?:QQ|空间)?说说(?:呢|啊|呀|吗)?[？?。！!]*', value) or re.fullmatch(
        r'(?:给你|给我|教你|告诉你)(?:一份|一个)?(?:发|发表)(?:QQ|空间)?说说(?:的)?教程[？?。！!]*', value,
    ):
        return 'help', ''
    if value in ('开启自动空间说说', '关闭自动空间说说', '自动空间说说状态'):
        return {'开启自动空间说说': 'auto_on', '关闭自动空间说说': 'auto_off',
                '自动空间说说状态': 'auto_status'}[value], ''
    if value in ('确认发表空间说说', '取消发表空间说说'):
        return ('confirm' if value.startswith('确认') else 'cancel'), ''
    if (re.fullmatch(r'(?:你|老公)?(?:刚才|已经)?(?:发|发表)(?:过|了)?(?:那条)?(?:空间)?说说(?:了吗|了没|没有|吗|没)?[？?。！!]*', value)
            and any(x in value for x in ('了吗', '了没', '没有', '发过'))) or re.fullmatch(
                r'(?:你|老公)?(?:那条|空间)?说说(?:发了没|发了吗|发成功了吗|发的是什么)[？?。！!]*', value):
        return 'status', ''
    match = re.fullmatch(r'(?:发|发表)(?:QQ)?空间说说[：:]\s*(.+)', value, re.S)
    if match:
        return 'immediate', match.group(1).strip()
    match = re.fullmatch(r'(?:帮我)?写(?:一条|条)?空间说说[：:]\s*(.+)', value, re.S)
    if match:
        return 'draft', match.group(1).strip()
    # Explicit imperatives only. Questions about capability and casual mentions
    # must never publish to someone else's feed.
    if re.search(r'(?:能不能|可以吗|会不会|怎么|为什么|别发|不要发|先别发|还没发|发过吗)', value) or re.search(
        r'(?:能|会|可以).{0,6}(?:发|发表).{0,6}说说.{0,3}[吗?？]', value,
    ):
        return None
    if re.search(r'(?:发|发表)(?:一条|一篇|个|条|一下|起来|出去)?(?:QQ|空间)?说说', value) and re.search(
        r'(?:^(?:你|老公)?(?:现在|赶紧|快|去|就|直接)?(?:发|发表)|'
        r'(?:你|老公).{0,9}(?:发|发表)|(?:可以|该|能|想让你|让你|帮我|现在).{0,10}(?:发|发表))', value,
    ):
        return 'immediate', ''
    if re.search(r'(?:发|发表)(?:一条|一篇|个|条)?说说', value) and re.search(
        r'^(?:你|老公)?(?:现在|赶紧|快|去|就|直接)?(?:发|发表)', value,
    ):
        return 'immediate', ''
    return None


def topic_mentioned(text):
    return bool(re.search(r'QQ空间|空间说说|(?:发|发表|写|看).{0,5}说说|说说.{0,6}(?:发|发表|看)'
                          r'|(?:发|发表|写|发布|放到|放进).{0,8}(?:空间|动态)', text))


def contextual_command(text):
    """Handle short follow-ups only when the conversation is about QQ Space."""
    value = text.strip(' 。！!？?，,')
    # After a QQ Space discussion, a short "try it" asks for the actual post.
    # The connector only uses this match when a recent QQ Space topic exists.
    if re.fullmatch(r'(?:你|老公)?(?:现在|再|先)?试(?:一|两)?下(?:喽|吧|嘛|呀|啊)?(?:宝宝|老公|亲爱的)?', value):
        return 'immediate', ''
    if re.fullmatch(r'(?:你|老公)?(?:到底)?(?:有|没有|有没有|有无)(?:实际)?(?:调用|发帖|发表)?权限(?:吗|呢|啊)?', value) or re.fullmatch(
        r'(?:你|老公)?(?:到底)?(?:能|可以|会)(?:实际)?(?:调用|发表|发)(?:吗|呢|啊)?', value,
    ):
        return 'help', ''
    if re.fullmatch(r'(?:那|就|你|老公|现在|直接|赶紧|快|去|可以|给我|嗯|好|行|的|吧|啊|呀|嘛|先|再|就这样)*发(?:吧|一下|一条|出去|说说)?(?:老公|亲爱的|吧|呀|啊|嘛)?', value) or re.fullmatch(
        r'(?:那|就|你|老公|现在|直接|赶紧|快|去|可以|给我|就这样)*自己发(?:一条|条)?(?:吧|呀|啊|嘛)?', value,
    ):
        return 'immediate', ''
    if re.fullmatch(r'(?:你|老公)?(?:发了没|发了吗|发了没有|发成功了吗|发没发|到底发了没)', value):
        return 'status', ''
    # Short ordinary chat replies such as “没有” and “我看看” must stay in
    # the conversation, even if QQ Space was discussed a few turns ago.
    if re.fullmatch(r'(?:我|给我|让我|发给我|你让我)?(?:看一下|看看|看下|看一眼)?(?:空间说说|说说|空间动态|截图)(?:看一下|看看|看下|看一眼)?', value):
        return 'view', ''
    if re.fullmatch(r'(?:根本|明明|还是|我看|我这边|这里)?(?:空间|QQ空间|说说|那条说说)(?:根本|还是)?(?:没有|没发|看不到|找不到|没有啊)', value) or re.fullmatch(
        r'(?:根本|明明|还是|我看|我这边|这里)?(?:没有|没发|看不到|找不到)(?:空间|QQ空间|说说|那条说说)', value
    ):
        return 'missing', ''
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


async def generate_requested_post(bot, chat_id, hint=''):
    """Write a post for an explicit owner request without reading private history."""
    import httpx

    messages = [
        {'role': 'system', 'content': (
            '你是一位成年男性。用户刚刚明确叫你自己写一条QQ空间说说，请只输出最终正文。'
            '克制、亲密、偏爱、有一点嘴硬，像本人临时想写的，不要像客服。'
            '不能声称自己已经发表，不能引用私聊原话、姓名、位置、工作、照片或共同经历。'
            '不要编造现实活动、天气，不要省略号，不加标题、引号或解释。'
            '只写15到100字的中文正文。')},
        {'role': 'user', 'content': '自己写一条说说。' + (f'用户希望表达的方向：{hint[:100]}。' if hint else '')},
    ]
    async with httpx.AsyncClient(timeout=35) as client:
        response = await client.post(
            f'{bot.LLMHUB_BASE_URL}/chat/completions',
            headers=bot.api_headers(),
            json={'model': bot.current_model(chat_id), 'messages': messages, 'stream': False},
        )
    response.raise_for_status()
    content = bot.extract_text(response.json()).strip().strip('"“”')
    if (not 10 <= len(content) <= MAX_POST_CHARS or content == 'NO_POST'
            or any(mark in content for mark in ('...', '……', '…', '<', '>', 'http://', 'https://', '\n'))):
        raise ValueError('Qzone requested post did not pass validation')
    return content


async def decide_chat_post(bot, chat_id, user_text, *, recent_topic=False):
    """Let the QQ companion choose a real publish action for natural requests.

    This decision is separate from the visible reply so a verbal promise can
    never masquerade as a successful OneBot action.
    """
    import httpx

    messages = [
        {'role': 'system', 'content': (
            '你是QQ私聊里的恋爱伴侣，拥有发表自己QQ空间说说的真实操作入口。'
            '判断用户这句话是否明确要求你现在发表一条说说。'
            '只有现在就要发表才选择publish；询问能力、讨论教程、否定、假设、'
            '引用别人的话、要草稿、查发表状态、普通聊天一律选择none。'
            '如果上一轮已在聊QQ空间，"试一下"、"就发那个"也可以是发表请求；'
            '没有相关上下文时不得把这些短句当成发表请求。'
            '只输出一个JSON对象：{"action":"none"}，或者'
            '{"action":"publish","content":"最终公开正文"}。'
            '需要自己写时，正文10到100字，像真人随手写的亲密心情。'
            '用户指定了明确正文则照用，但不得泄露私聊原话、真实姓名、位置、'
            '账号、工作或具体共同经历。不得编造现实见面和活动。'
            '正文不含换行、链接、省略号、内部标签；不要说自己已经发出。')},
        {'role': 'user', 'content': json.dumps({
            'message': user_text[:4000], 'recent_qzone_topic': bool(recent_topic),
        }, ensure_ascii=False)},
    ]
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            f'{bot.LLMHUB_BASE_URL}/chat/completions',
            headers=bot.api_headers(),
            json={'model': bot.current_model(chat_id), 'messages': messages, 'stream': False},
        )
    response.raise_for_status()
    raw = bot.extract_text(response.json()).strip()
    # Some providers wrap a JSON response in a Markdown fence.
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.I).strip()
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('action') not in ('none', 'publish'):
        raise ValueError('Invalid Qzone action decision')
    if value['action'] == 'none':
        return None
    content = value.get('content')
    if (not isinstance(content, str) or not 10 <= len(content) <= MAX_POST_CHARS
            or any(mark in content for mark in ('...', '……', '…', '<', '>', 'http://',
                                                'https://', '\n', '\r'))):
        raise ValueError('Invalid Qzone action content')
    return content
