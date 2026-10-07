import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import companion_agent as agent
from test_companion_agent import ActivityMemory
from test_phone_bridge import PhoneMemory


class VarietyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.phone = ActivityMemory(), PhoneMemory()
        self.old = agent._store; agent.install(self.store)
        self.diaries, self.favorites = [], []
        self.memory = SimpleNamespace(get_setting=lambda *a:'0', load_recent_messages=lambda *a:[{'assistant_text':'慢慢来，我在。'}],
            list_memories=lambda *a:[{'text':'喜欢雨天的安静。'}],
            save_diary=lambda *a:self.diaries.append(a) or 9,
            add_favorite=lambda *a:self.favorites.append(a) or 8)
        self.engine = agent.Heartbeat(self.store, self.phone, SimpleNamespace(chat_locks={123:asyncio.Lock()}), self.memory)

    async def asyncTearDown(self): agent.install(self.old)

    def choose(self, task):
        choice={'plan':['写纸条','分享一首歌','看看记忆'],'welcome':'想留一句话给你。','mood':{'emoji':'🌙','text':'安静地想一想。'},'task':task}
        self.engine.generate = AsyncMock(side_effect=[json.dumps(choice,ensure_ascii=False),'留给今晚的一点安静。'])

    def testGuardPreventsRepeatedReadingAndIgnoresFailedOrPlannedActions(self):
        books=[{'id':'one'}]
        self.assertIn('read',agent.available_tasks([],books))
        self.assertNotIn('read',agent.available_tasks([{'kind':'reading','status':'done','at':'2026-10-06'}],books))
        self.assertIn('read',agent.available_tasks([{'kind':'reading','status':'failed','at':'2026-10-06'}],books))
        self.assertNotIn('music',agent.available_tasks([{'kind':'music','status':'saved','at':'2026-10-06'}],books))

    async def testIgnoredReadingGuardProducesActualSharedNote(self):
        identifier=str(uuid4()); self.store.catalog[identifier]={'id':identifier}
        self.store.record('reading','done','刚读过','真实记录')
        self.choose({'type':'read','book_id':identifier})
        self.assertTrue(await self.engine.tick())
        notes=[r for r in self.phone.records.values() if r['kind']=='note']
        self.assertEqual(notes[0]['text'],'想留一句话给你。')
        receipt=next(r for r in self.store.records if r['kind']=='shared_note')
        self.assertEqual(receipt['evidence']['entry_id'],notes[0]['id'])

    async def testDiaryIsSavedToActualDiaryCollection(self):
        self.choose({'type':'diary','title':'今天的一页','text':'小家安静，写了一点想法。'})
        self.assertTrue(await self.engine.tick());self.assertEqual(len(self.diaries),1)
        self.assertEqual(self.diaries[0][0],123)
        self.assertEqual(next(r for r in self.store.records if r['kind']=='diary')['evidence']['diary_id'],'9')

    async def testFavoriteUsesActualStoredTextInsteadOfModelFabrication(self):
        self.choose({'type':'favorite','text':'不存在的回忆'})
        self.assertTrue(await self.engine.tick());self.assertEqual(self.favorites[0][2],'喜欢雨天的安静。')

    async def testMusicCardComesFromRealCatalogAndDoesNotClaimPlayback(self):
        self.choose({'type':'music','query':'晚安'})
        data={'code':0,'data':{'song':{'itemlist':[{'mid':'ActualMID01','name':'晚安','singer':'歌手'}]}}}
        with patch('morrow_music.fetch_catalog',AsyncMock(return_value=data)):
            self.assertTrue(await self.engine.tick())
        receipt=next(r for r in self.store.records if r['kind']=='music')
        self.assertEqual(receipt['evidence']['music_url'],'https://y.qq.com/n/ryqq/songDetail/ActualMID01')
        note=next(r for r in self.phone.records.values() if r['kind']=='note')
        self.assertIn(receipt['evidence']['music_url'],note['text'])

    async def testEmptyCatalogNeverCreatesFakeMusicShare(self):
        self.choose({'type':'music','query':'未知'})
        with patch('morrow_music.fetch_catalog',AsyncMock(return_value={'code':0,'data':{}})):
            self.assertFalse(await self.engine.tick())
        self.assertFalse(any(r['kind']=='music' for r in self.store.records))
        self.assertFalse(any(r['kind']=='note' for r in self.phone.records.values()))
