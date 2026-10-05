"""Private daily keepsakes. Generated from evidence; committed together, retryable."""
import asyncio
import json
import time
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5
import companion_features
import phone_bridge

_retry_at = 0

def validate(value, recent):
    if not isinstance(value, dict) or set(value) != {'diary','note','favorite_index','favorite_note'}:
        raise ValueError('Invalid daily output')
    if value['favorite_note'] is None:value={**value,'favorite_note':''}
    for field, low, high in [('diary',30,1200),('note',1,1000),('favorite_note',0,300)]:
        if not isinstance(value[field],str) or not low <= len(value[field]) <= high:
            raise ValueError('Invalid daily text: '+field)
    index=value['favorite_index']
    if index is not None and (isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(recent) or recent[index]['role']!='user'):
        raise ValueError('Favorite must identify a real user message')
    return value

def commit(memory, owner, day, value, recent):
    source=-int(day.replace('-',''))
    note_id=str(uuid5(NAMESPACE_URL,f'morrow-daily-note:{owner}:{day}'))
    with memory._connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS morrow_daily_receipts (
                chat_id BIGINT NOT NULL,day DATE NOT NULL,created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY(chat_id,day))''')
            cur.execute('INSERT INTO morrow_daily_receipts(chat_id,day) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING day',(owner,day))
            if not cur.fetchone(): return False
            cur.execute('''INSERT INTO companion_diary(chat_id,source_message_id,text,publish_status)
                VALUES(%s,%s,%s,'private') ON CONFLICT(chat_id,source_message_id) DO NOTHING''',(owner,source,day+'\n'+value['diary']))
            entry=phone_bridge.normalize_entry({'id':note_id,'actor':'assistant','kind':'note','day':day,'title':'留给你的纸条','text':value['note']},'assistant')
            # Same schema as PhoneStore; the receipt and notes are one transaction.
            cur.execute('''INSERT INTO companion_shared_entries(chat_id,id,actor,day,data)
                VALUES(%s,%s,'assistant',%s,%s::jsonb) ON CONFLICT(chat_id,id) DO NOTHING''',(owner,note_id,day,json.dumps(entry,ensure_ascii=False)))
            index=value['favorite_index']
            if index is not None:
                cur.execute('''INSERT INTO favorites(chat_id,telegram_message_id,text,note)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(chat_id,telegram_message_id) DO NOTHING''',
                    (owner,-1000000000+source,recent[index]['content'][:5000],value['favorite_note']))
        conn.commit()
    return True

def done(memory, owner, day):
    with memory._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM morrow_daily_receipts WHERE chat_id=%s AND day=%s",(owner,day))
            return bool(cur.fetchone())

async def tick(application, owner, *, now=None):
    global _retry_at
    now=now or datetime.now(phone_bridge.TZ)
    if time.time()<_retry_at:return
    import runner
    memory=runner.memory_pg
    if memory.get_setting(owner,companion_features.AUTO_DIARY_KEY)=='0':return
    heartbeat=application.bot_data.get('companion_heartbeat')
    if heartbeat is None:return
    lock=runner.bot.chat_locks[owner]
    if lock.locked():return
    # Repair the last three days using dated evidence. Empty days remain empty.
    candidate=None
    for offset in (2,1,0):
        date=now.date()-timedelta(days=offset)
        if offset==0 and now.hour<20:continue
        day=date.isoformat()
        if await asyncio.to_thread(done,memory,owner,day):continue
        start=datetime.combine(date,datetime.min.time(),tzinfo=phone_bridge.TZ)
        end=start+timedelta(days=1)
        recent=await asyncio.to_thread(memory.diary_context_between,owner,start,end,30)
        activities=await asyncio.to_thread(heartbeat.store.activities,day)
        activities=[item for item in activities if item['kind'] not in ('wake','plan') and item['status'] not in ('failed','started','planned')]
        if not recent and not activities:continue
        candidate=(day,recent,activities);break
    if candidate is None:return
    day,recent,activities=candidate
    _retry_at=time.time()+120
    async with lock:
        raw=await heartbeat.generate('只返回JSON，键严格为 diary、note、favorite_index、favorite_note。diary 是80到220字的私人日记；note是一张自然亲密的小纸条；favorite_index 是资料 recent 中一句值得珍惜的用户原话的0起始索引，没有就 null；favorite_note 是收藏缘由。只根据指定日期的真实对话与已经完成的活动。没有对话就写自己的阅读或思考，不能虚构两人聊过或见过。不能捏造天气、浏览、健康或音乐。不要生成收藏原话，由程序直接取出。',{'recent':[{'index':i,**item} for i,item in enumerate(recent)],'activities':activities,'day':day})
        raw=raw.removeprefix('```json').removeprefix('```').removesuffix('```').strip()
        try:
            value=validate(json.loads(raw),recent)
        except json.JSONDecodeError:
            print('Morrow daily decision invalid: JSON syntax',flush=True)
            raise
        except ValueError as exc:
            print('Morrow daily decision invalid: '+str(exc),flush=True)
            raise
        await asyncio.to_thread(commit,memory,owner,day,value,recent)
        print('Morrow daily keepsakes committed: '+day,flush=True)
