import asyncio
import copy
import json
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from aiohttp.test_utils import TestClient, TestServer
import companion_agent as agent
import phone_bridge as bridge
from ios_api import Gateway, create_web_app
from test_ios_api import FakeStore
from test_phone_bridge import PhoneMemory

class ActivityMemory:
    owner=123
    def __init__(self):
        self.catalog={};self.pages={};self.records=[];self.deleted=set();self.leased=False
        self.config={'enabled':True,'next_at':0,'plan':None,'plan_day':None};self.finished=[]
    def books(self):return [copy.deepcopy(b) for b in self.catalog.values() if b['id'] not in self.deleted]
    def put_book(self,d):
        if d['id'] in self.deleted:raise bridge.BridgeError('deleted')
        b=self.catalog.setdefault(d['id'],{k:d[k] for k in ('id','title','page_count','user_page')})
        b.setdefault('assistant_page',-1);b.setdefault('received',0)
        if d['start']>b['received']:raise bridge.BridgeError('gap')
        for n,text in enumerate(d['pages'],d['start']):self.pages.setdefault((d['id'],n),text)
        b['received']=max(b['received'],d['start']+len(d['pages']));b['user_page']=d['user_page']
        return {'received':b['received']}
    def delete_book(self,id):self.deleted.add(id);self.pages={k:v for k,v in self.pages.items() if k[0]!=id};return True
    def next_page(self,id):
        if id in self.deleted:return None
        b=self.catalog[id];n=b['assistant_page']+1;text=self.pages.get((id,n))
        return {'id':id,'title':b['title'],'page':n,'page_count':b['page_count'],'text':text} if text else None
    def complete_page(self,id,n):
        b=self.catalog[id]
        if id in self.deleted or n!=b['assistant_page']+1:return False
        b['assistant_page']=n;return True
    def record(self,kind,status,title,text,evidence=None,identifier=None):
        d={'id':identifier or str(uuid4()),'day':datetime.now(bridge.TZ).strftime('%Y-%m-%d'),'kind':kind,'status':status,'title':title,'text':text,'evidence':evidence or {},'at':datetime.now(bridge.TZ).isoformat()}
        self.records.append(d);return d
    def activities(self,day):return [d for d in self.records if d['day']==day]
    def state(self):return copy.deepcopy(self.config)
    def configure(self,enabled):self.config['enabled']=enabled
    def claim(self,now,force=False):
        if self.leased or not self.config['enabled'] or self.config['next_at']>now and not force:return None
        self.leased=True;return 'lease'
    def finish(self,lease,next_at):self.leased=False;self.config['next_at']=next_at;self.finished.append(next_at)
    def plan(self,day,data):self.config.update(plan_day=day,plan=data)

class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=ActivityMemory();self.phone=PhoneMemory();self.previous=agent._store;agent.install(self.store)
        self.id=str(uuid4());self.chunk={'id':self.id,'title':'测试书','page_count':2,'user_page':1,'start':0,'pages':['第一段真实正文','第二段真实正文']}
        self.store.put_book(agent.book_chunk(self.chunk));self.turn={'key':'test','results':{}}
        async def handler(update,context):await update.message.reply_text('我在')
        self.client=TestClient(TestServer(create_web_app(Gateway(owner=123,handler=handler,store=FakeStore(),token='a'*32),self.phone,self.store)))
        await self.client.start_server();self.headers={'Authorization':'Bearer '+'a'*32}
    async def asyncTearDown(self):agent.install(self.previous);await self.client.close()
    async def test_read_requires_actual_page_and_does_not_copy_user_progress(self):
        with self.assertRaises(bridge.BridgeError):await agent.execute('finish_book_page',{'book_id':self.id,'page':1,'note':'假读'},self.turn)
        read=await agent.execute('read_next_book_page',{'book_id':self.id},self.turn)
        self.assertEqual(read['passage']['text'],'第一段真实正文');self.assertEqual(read['passage']['page'],0)
        self.assertEqual(self.store.books()[0]['assistant_page'],-1)
        args={'book_id':self.id,'page':0,'note':'读过第一段后的感想'}
        await agent.execute('finish_book_page',args,self.turn);await agent.execute('finish_book_page',args,self.turn)
        self.assertEqual(self.store.books()[0]['assistant_page'],0);self.assertEqual(len(self.store.records),1)
    async def test_deleted_book_cannot_resurrect_or_finish_read(self):
        await agent.execute('read_next_book_page',{'book_id':self.id},self.turn)
        r=await self.client.delete('/v1/books/'+self.id,headers=self.headers);self.assertEqual(r.status,200)
        r=await self.client.post('/v1/books',json=self.chunk,headers=self.headers);self.assertEqual(r.status,400)
        with self.assertRaises(bridge.BridgeError):await agent.execute('finish_book_page',{'book_id':self.id,'page':0,'note':'旧任务'},self.turn)
        self.assertFalse(self.store.pages)
    async def test_new_routes_authenticate_and_reject_actor_or_owner_override(self):
        for method,path in [('get','/v1/books'),('post','/v1/books'),('get','/v1/activity'),('post','/v1/heartbeat'),('delete','/v1/books/'+self.id)]:
            r=await getattr(self.client,method)(path);self.assertEqual(r.status,401)
        r=await self.client.post('/v1/books',json={**self.chunk,'assistant_page':1},headers=self.headers);self.assertEqual(r.status,400)
        r=await self.client.post('/v1/heartbeat',json={'enabled':False},headers=self.headers);self.assertEqual(r.status,200)
        self.assertFalse(self.store.config['enabled'])
    async def test_invalid_chunks_rejected(self):
        for fields in [{'page_count':True},{'start':-1},{'pages':['x'*12001]},{'pages':['a']*21},{'user_page':2},{'id':'bad'}]:
            with self.assertRaises((ValueError,bridge.BridgeError)):agent.book_chunk({**self.chunk,**fields})
    async def test_heartbeat_skips_active_chat_then_saves_plan_mood_read_receipt(self):
        last=[time.time()]
        memory=SimpleNamespace(get_setting=lambda owner,key:str(last[0]),load_recent_messages=lambda *a:[],list_memories=lambda *a:[])
        bot=SimpleNamespace(chat_locks={123:asyncio.Lock()})
        engine=agent.Heartbeat(self.store,self.phone,bot,memory)
        outputs=[json.dumps({'plan':['继续读书'],'welcome':'回来啦','mood':{'emoji':'📖','text':'今天读书时很安静'},'task':{'type':'read','book_id':self.id}}),'第一段的确写了这句话。']
        async def generate(instruction,data):return outputs.pop(0)
        engine.generate=generate
        self.assertFalse(await engine.tick());self.assertFalse(self.store.records)
        last[0]=time.time()-1000;self.assertTrue(await engine.tick())
        self.assertEqual(self.store.books()[0]['assistant_page'],0)
        self.assertEqual(len(self.phone.records),1);self.assertEqual(next(iter(self.phone.records.values()))['actor'],'assistant')
        self.assertTrue(any(r['kind']=='reading' and r['status']=='done' for r in self.store.records))
        self.assertTrue(self.store.config['next_at']>time.time())
    async def test_invalid_or_failed_action_never_claims_success_and_releases_lease(self):
        memory=SimpleNamespace(get_setting=lambda *a:'0',load_recent_messages=lambda *a:[])
        engine=agent.Heartbeat(self.store,self.phone,SimpleNamespace(chat_locks={123:asyncio.Lock()}),memory)
        async def broken(*a):return '{"task":{"type":"shell"}}'
        engine.generate=broken
        self.assertFalse(await engine.tick());self.assertFalse(self.store.leased)
        self.assertTrue(any(r['status']=='failed' for r in self.store.records));self.assertFalse(any(r['status']=='done' for r in self.store.records))
    async def test_actual_research_result_is_saved_with_sources(self):
        response=http_response={'query':'月亮','sources':[{'title':'月球','url':'https://zh.wikipedia.org/?curid=1','excerpt':'资料'}],'message':'已实际查询'}
        async def search(query):return response
        with patch.object(agent,'search_topic',search):result=await agent.execute('search_personal_topic',{'query':'月亮'},self.turn)
        self.assertEqual(result['receipt']['evidence']['sources'],http_response['sources'])
    def test_night_interval_and_location_validation(self):
        self.assertEqual(agent.interval(datetime(2026,10,5,3,tzinfo=bridge.TZ).timestamp()),7200)
        self.assertEqual(agent.interval(datetime(2026,10,5,12,tzinfo=bridge.TZ).timestamp()),3000)
        d={'device_id':str(uuid4()),'location_read':True,'location':{'latitude':31.2,'longitude':121.5,'accuracy':100,'sampled_at':datetime.now(bridge.TZ).isoformat()}}
        self.assertIsNotNone(bridge.normalize_snapshot(d)['location'])
        self.assertIsNone(bridge.normalize_snapshot({**d,'location_read':False})['location'])
        for value in [float('nan'),float('inf'),True,1000]:
            with self.assertRaises(bridge.BridgeError):bridge.normalize_snapshot({**d,'location':{**d['location'],'latitude':value}})
