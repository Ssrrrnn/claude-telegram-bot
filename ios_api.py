"""Optional owner-only iOS transport for the existing companion pipeline.

Disabled unless IOS_APP_TOKEN is configured. TLS is terminated by Railway.
The client cannot select a chat ID, model, persona, or provider credential.
"""
import asyncio
import hmac
import json
import os
import re
from types import SimpleNamespace
from uuid import UUID

from qq_gateway import _Bot, _Message


class Store:
    def __init__(self, memory, owner):
        self.memory, self.owner = memory, owner

    def init(self):
        with self.memory._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS ios_requests (
                    id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL,
                    request_id UUID NOT NULL, user_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing', result JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(chat_id, request_id))""")
                # A crash may leave a partially delivered turn. Never regenerate it silently.
                cur.execute("UPDATE ios_requests SET status='interrupted' "
                            "WHERE chat_id=%s AND status='processing'", (self.owner,))
            conn.commit()

    def get(self, request_id):
        with self.memory._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,user_text,status,result FROM ios_requests "
                            "WHERE chat_id=%s AND request_id=%s", (self.owner, request_id))
                return cur.fetchone()

    def begin(self, request_id, text):
        with self.memory._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO ios_requests(chat_id,request_id,user_text) "
                            "VALUES (%s,%s,%s) RETURNING id", (self.owner, request_id, text))
                value = cur.fetchone()['id']
            conn.commit()
        # Telegram message IDs are 32-bit; QQ IDs are negative. Reserve a high positive range.
        return 2**62 + value

    def finish(self, request_id, result, status='done'):
        # Replays return text, so large audio files are never retained in PostgreSQL.
        cached = dict(result)
        cached['replies'] = [
            {'type': 'text', 'text': item.get('fallback_text') or '这条语音已发送。'}
            if item['type'] == 'record' else item for item in result.get('replies', [])]
        with self.memory._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ios_requests SET status=%s,result=%s::jsonb "
                            "WHERE chat_id=%s AND request_id=%s",
                            (status, json.dumps(cached, ensure_ascii=False), self.owner, request_id))
                # Retain small text receipts so a reused UUID cannot regenerate a turn.
            conn.commit()

    def history(self):
        with self.memory._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,user_text,assistant_text FROM recent_conversation "
                            "WHERE chat_id=%s ORDER BY id DESC LIMIT 40", (self.owner,))
                rows = list(reversed(cur.fetchall()))
        messages = []
        for row in rows:
            for role, field in [('user', 'user_text'), ('assistant', 'assistant_text')]:
                if row.get(field):
                    messages.append({'id': f"{row['id']}:{role}", 'role': role, 'text': row[field]})
        return messages

    def assistant_id(self, message_id):
        with self.memory._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM recent_conversation WHERE chat_id=%s "
                            "AND telegram_message_id=%s", (self.owner, message_id))
                row = cur.fetchone()
        return f"{row['id']}:assistant" if row else None

    def collection(self, kind):
        rows = (self.memory.list_favorites(self.owner, 30) if kind == 'favorites'
                else self.memory.list_diary(self.owner, 30))
        return [{'id': str(row['id']), 'text': row['text']} for row in rows]


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Gateway:
    def __init__(self, *, owner, handler, store, token):
        if isinstance(owner, bool) or not isinstance(owner, int) or owner <= 0 or len(token) < 32:
            raise ValueError('iOS API needs a fixed owner and a token of at least 32 characters')
        self.owner, self.handler, self.store, self.token = owner, handler, store, token
        self.lock = asyncio.Lock()

    def authenticate(self, value):
        return hmac.compare_digest(value.encode(), ('Bearer ' + self.token).encode())

    async def process(self, data):
        import time
        if not isinstance(data, dict) or set(data) - {'request_id', 'text', 'sent_at'}:
            raise APIError(400, '消息格式不正确。')
        try:
            request_id = str(UUID(data.get('request_id', '')))
        except (ValueError, TypeError, AttributeError):
            raise APIError(400, '消息编号不正确。')
        text, sent_at = data.get('text'), data.get('sent_at')
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise APIError(400, '请发送 1–4000 字的消息。')
        if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)) or not time.time() - 7*86400 <= sent_at <= time.time() + 300:
            raise APIError(400, '这条待发送消息已过期，请重新输入。')
        text = text.strip()
        existing = await asyncio.to_thread(self.store.get, request_id)
        if existing:
            if existing['user_text'] != text:
                raise APIError(409, '同一个消息编号不能用于不同内容。')
            if existing['status'] == 'done':
                return {**existing['result'], 'duplicate': True}
            raise APIError(409, '这条消息正在处理或曾被中断，请先查看聊天记录。')
        if self.lock.locked():
            raise APIError(429, '上一条消息还在处理，请稍后重试。')
        async with self.lock:
            message_id = await asyncio.to_thread(self.store.begin, request_id, text)
            outbox = []
            message = _Message(text, message_id, outbox)
            message.source_platform = 'ios'
            update = SimpleNamespace(effective_chat=SimpleNamespace(id=self.owner), message=message)
            context = SimpleNamespace(bot=_Bot(outbox))
            try:
                await self.handler(update, context)
                # Do not forward provider URLs or raw exception details from the legacy handler.
                for item in outbox:
                    if item.get('text', '').startswith('API 请求失败'):
                        raise APIError(503, '这次回复没有生成成功，请先查看聊天记录再重新发送。')
                if not outbox:
                    raise RuntimeError('Empty response')
                result = {'request_id': request_id, 'replies': outbox,
                          'assistant_id': await asyncio.to_thread(self.store.assistant_id, message_id),
                          'duplicate': False}
                await asyncio.to_thread(self.store.finish, request_id, result)
                return result
            except Exception:
                await asyncio.to_thread(self.store.finish, request_id, {}, 'interrupted')
                raise


def create_web_app(gateway, phone=None):
    from aiohttp import web
    import phone_bridge

    @web.middleware
    async def protection(request, handler):
        if request.path != '/health' and not gateway.authenticate(request.headers.get('Authorization', '')):
            return web.json_response({'error': '连接密钥不正确。'}, status=401,
                                     headers={'Cache-Control': 'no-store'})
        try:
            response = await handler(request)
        except APIError as exc:
            response = web.json_response({'error': exc.message}, status=exc.status)
        except (phone_bridge.BridgeError, ValueError, TypeError, KeyError) as exc:
            response = web.json_response({'error': '记录或设备参数不正确，请检查后重试。'}, status=400)
        except web.HTTPException:
            raise
        except Exception as exc:
            print(f'iOS API request failed: {type(exc).__name__}', flush=True)
            response = web.json_response({'error': '暂时连接不上，稍后再试。'}, status=503)
        response.headers['Cache-Control'] = 'no-store'
        return response

    async def health(request):
        return web.json_response({'ok': True})

    async def history(request):
        return web.json_response({'messages': await asyncio.to_thread(gateway.store.history)})

    async def chat(request):
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise APIError(400, '消息格式不正确。')
        return web.json_response(await gateway.process(data))

    async def collection(request):
        return web.json_response({'items': await asyncio.to_thread(
            gateway.store.collection, request.match_info['kind'])})

    def phone_store():
        if phone is None:
            raise APIError(503, '共享日历暂不可用。')
        return phone

    async def shared_records(request):
        store = phone_store()
        if request.method == 'POST':
            entry = phone_bridge.normalize_entry(await request.json(), 'user')
            return web.json_response({'entry': await asyncio.to_thread(store.put_entry, entry)})
        month = request.query.get('month', '')
        if not re.fullmatch(r'\d{4}-\d{2}', month):
            raise APIError(400, '请选择一个月份。')
        from datetime import datetime, timedelta
        begin = datetime.strptime(month + '-01', '%Y-%m-%d')
        end = (begin.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return web.json_response({'entries': await asyncio.to_thread(store.entries, begin.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))})

    async def delete_shared(request):
        identifier = phone_bridge.identifier(request.match_info['id'])
        return web.json_response({'removed': await asyncio.to_thread(phone_store().delete_entry, identifier)})

    async def phone_context(request):
        snapshot = phone_bridge.normalize_snapshot(await request.json())
        await asyncio.to_thread(phone_store().put_snapshot, snapshot)
        return web.json_response({'ok': True})

    async def phone_actions(request):
        device = phone_bridge.identifier(request.query.get('device_id', ''))
        return web.json_response({'actions': await asyncio.to_thread(phone_store().pending, device)})

    async def phone_receipt(request):
        data = await request.json()
        if not isinstance(data, dict) or set(data) - {'device_id', 'success', 'event_id', 'message'}:
            raise APIError(400, '回执格式不正确。')
        device, identifier = phone_bridge.identifier(data.get('device_id', '')), phone_bridge.identifier(request.match_info['id'])
        result = {'success': data.get('success') is True, 'event_id': phone_bridge.short(data.get('event_id', ''), 500), 'message': phone_bridge.short(data.get('message', ''), 300)}
        return web.json_response({'result': await asyncio.to_thread(phone_store().acknowledge, device, identifier, result)})

    app = web.Application(middlewares=[protection], client_max_size=128_000)
    app.router.add_get('/health', health)
    app.router.add_get('/v1/history', history)
    app.router.add_post('/v1/chat', chat)
    app.router.add_get('/v1/{kind:favorites|diaries}', collection)
    app.router.add_get('/v1/shared', shared_records)
    app.router.add_post('/v1/shared', shared_records)
    app.router.add_delete('/v1/shared/{id}', delete_shared)
    app.router.add_post('/v1/phone/context', phone_context)
    app.router.add_get('/v1/phone/actions', phone_actions)
    app.router.add_post('/v1/phone/actions/{id}/receipt', phone_receipt)
    return app


async def start(application, *, handler, bot, memory):
    token = os.getenv('IOS_APP_TOKEN', '').strip()
    if not token:
        return
    from aiohttp import web
    owner = bot.TELEGRAM_OWNER_ID or bot.CLAIMED_OWNER_ID
    if not bot.is_authorized(owner):
        raise ValueError('iOS API owner is not authorized')
    store = Store(memory, owner)
    gateway = Gateway(owner=owner, handler=handler, store=store, token=token)
    await asyncio.to_thread(store.init)
    import phone_bridge
    phone = phone_bridge.PhoneStore(memory, owner)
    try:
        await asyncio.to_thread(phone.init)
        phone_bridge.install(phone)
    except Exception as exc:
        print(f'Shared phone bridge unavailable: {type(exc).__name__}', flush=True)
        phone = None
    runner = web.AppRunner(create_web_app(gateway, phone), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', '8080'))).start()
    except Exception:
        await runner.cleanup()
        raise
    application.bot_data['ios_api_runner'] = runner
    print('Owner-only iOS API enabled', flush=True)


async def stop(application):
    runner = application.bot_data.pop('ios_api_runner', None)
    if runner:
        await runner.cleanup()
