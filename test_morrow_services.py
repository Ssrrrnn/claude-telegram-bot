import asyncio
import copy
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer
import morrow_services as services
import morrow_daily as daily
from ios_api import Gateway, create_web_app
from test_ios_api import FakeStore

class SafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_health_and_listening_validate_actual_sources(self):
        value=services.normalize_context('health',{'source':'manual','steps':8000,'sleep_hours':7.5})
        self.assertEqual(value['source'],'manual');self.assertEqual(value['steps'],8000)
        for data in [{'source':'manual','steps':True},{'source':'manual','sleep_hours':25},{'source':'manual','heart_rate':90},{'source':'pretend','steps':1}]:
            with self.assertRaises(ValueError):services.normalize_context('health',data)
        self.assertIsNone(services.normalize_context('health',None))
        with self.assertRaises(ValueError):services.normalize_context('listening',{'url':'https://example.com','title':'x','kind':'music','playing':True,'position':float('nan')})
    async def test_browser_blocks_private_addresses_and_dns(self):
        for url in ['http://example.com','https://localhost','https://127.0.0.1','https://169.254.169.254','https://[::1]','https://user:pw@example.com','https://example.com:444']:
            with self.assertRaises(ValueError):services.safe_url(url)
        fake=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.3',443))])
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',fake):
            with self.assertRaises(ValueError):await services.PublicResolver().resolve('public-looking.example',443)
    def test_daily_favorite_is_real_user_quote(self):
        recent=[{'role':'user','content':'真实的话'},{'role':'assistant','content':'回复'}]
        value={'diary':'一天的真实记忆。'*8,'note':'留给你的一句话','favorite_index':0,'favorite_note':'想记住'}
        self.assertEqual(daily.validate(value,recent)['favorite_index'],0)
        for index in [True,1,2,-1,'0']:
            with self.assertRaises(ValueError):daily.validate({**value,'favorite_index':index},recent)
    def test_absent_favorite_reason_does_not_block_private_diary(self):
        value={'diary':'一天的真实记忆。'*8,'note':'留给你的一句话','favorite_index':None,'favorite_note':None}
        self.assertEqual(daily.validate(value,[])['favorite_note'],'')

    async def test_mcp_never_reports_failed_read_as_success(self):
        with patch.object(services,'_store',object()),patch.object(services,'browse_url',AsyncMock(side_effect=ValueError('private secret'))):
            result=await services.mcp({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'browse_public_page','arguments':{'url':'https://example.com'}}})
        self.assertTrue(result['result']['isError']);self.assertNotIn('secret',json.dumps(result))

class ExtensionAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context={};self.previous=services._store
        services.install(SimpleNamespace(browsing=lambda:[],context=lambda kind:self.context.get(kind),put_context=lambda kind,data:self.context.__setitem__(kind,data)))
        self.client=TestClient(TestServer(create_web_app(Gateway(owner=123,handler=AsyncMock(),store=FakeStore(),token='a'*32))))
        await self.client.start_server();self.headers={'Authorization':'Bearer '+'a'*32}
    async def asyncTearDown(self):services.install(self.previous);await self.client.close()
    async def test_owner_only_routes_and_deletion(self):
        for path in ['/v1/browsing','/v1/context/health','/v1/context/listening']:
            self.assertEqual((await self.client.get(path)).status,401)
        self.assertEqual((await self.client.post('/mcp',json={'jsonrpc':'2.0','method':'tools/list','id':1})).status,401)
        response=await self.client.post('/v1/context/health',headers=self.headers,json={'source':'manual','steps':12})
        self.assertEqual(response.status,200)
        response=await self.client.get('/v1/context/health',headers=self.headers)
        self.assertEqual((await response.json())['context']['steps'],12)
        response=await self.client.post('/v1/context/health',headers=self.headers,data='null',skip_auto_headers={'Content-Type'})
        self.assertEqual(response.status,200);self.assertIsNone(self.context['health'])
    async def test_mcp_lists_only_bounded_owner_tools(self):
        response=await self.client.post('/mcp',headers=self.headers,json={'jsonrpc':'2.0','method':'tools/list','id':7})
        data=await response.json();self.assertEqual(data['id'],7)
        names={item['name'] for item in data['result']['tools']}
        self.assertIn('browse_public_page',names);self.assertNotIn('execute_script',names)

class DailyEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_activity_can_fill_diary_without_inventing_a_conversation(self):
        import sys
        from collections import defaultdict
        from datetime import datetime
        import phone_bridge
        now=datetime(2026,10,5,21,tzinfo=phone_bridge.TZ)
        memories=SimpleNamespace(get_setting=lambda *args:None,diary_context_between=lambda *args:[])
        activity={'kind':'reading','status':'done','text':'实际读了第一页'}
        store=SimpleNamespace(activities=lambda day:[activity] if day=='2026-10-05' else [])
        result={'diary':'读到这里，留下一点自己的想法。'*5,'note':'想把这页留给你。','favorite_index':None,'favorite_note':''}
        heartbeat=SimpleNamespace(store=store,generate=AsyncMock(return_value=json.dumps(result,ensure_ascii=False)))
        runner=SimpleNamespace(memory_pg=memories,bot=SimpleNamespace(chat_locks=defaultdict(asyncio.Lock)))
        application=SimpleNamespace(bot_data={'companion_heartbeat':heartbeat})
        committed=[]
        with patch.dict(sys.modules,{'runner':runner}),patch.object(daily,'_retry_at',0),patch.object(daily,'done',return_value=False),patch.object(daily,'commit',side_effect=lambda *args:committed.append(args)):
            await daily.tick(application,123,now=now)
        self.assertEqual(len(committed),1);self.assertEqual(committed[0][2],'2026-10-05')
        self.assertIsNone(committed[0][3]['favorite_index'])
        self.assertIn('实际读了第一页',json.dumps(heartbeat.generate.call_args.args[1],ensure_ascii=False))
    async def test_failed_generation_does_not_commit_and_can_retry(self):
        import sys
        from collections import defaultdict
        from datetime import datetime
        import phone_bridge
        now=datetime(2026,10,5,21,tzinfo=phone_bridge.TZ)
        memories=SimpleNamespace(get_setting=lambda *args:None,diary_context_between=lambda *args:[{'role':'user','content':'真实对话'}])
        heartbeat=SimpleNamespace(store=SimpleNamespace(activities=lambda day:[]),generate=AsyncMock(side_effect=RuntimeError('offline')))
        runner=SimpleNamespace(memory_pg=memories,bot=SimpleNamespace(chat_locks=defaultdict(asyncio.Lock)))
        application=SimpleNamespace(bot_data={'companion_heartbeat':heartbeat})
        with patch.dict(sys.modules,{'runner':runner}),patch.object(daily,'_retry_at',0),patch.object(daily,'done',return_value=False),patch.object(daily,'commit') as commit:
            with self.assertRaises(RuntimeError):await daily.tick(application,123,now=now)
            commit.assert_not_called()
            daily._retry_at=0
            heartbeat.generate.side_effect=None
            heartbeat.generate.return_value=json.dumps({'diary':'一天的真实记忆。'*8,'note':'留给你的一句话','favorite_index':0,'favorite_note':'想记住'})
            await daily.tick(application,123,now=now)
            commit.assert_called_once()
