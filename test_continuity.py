import copy
import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import continuity as c


def thread(**changes):
    value = {"id": "work1", "title": "镜头还没改完", "purpose": "问镜头是否顺利", "category": "work",
             "subject": "user", "realm": "real", "evidence": "镜头还没改完", "source_message_id": 1,
             "created_at": 100, "due_at": 1200, "expires_at": 20000, "status": "open", "contacted_at": None}
    value.update(changes)
    return value


class ContinuityTests(unittest.TestCase):
    def test_finished_work_closes_followup_before_next_reply(self):
        state = c.empty()
        state["threads"] = [thread()]
        state = c.receive_state(state, "我已经下班了", 2, 1500)
        self.assertEqual(state["threads"][0]["status"], "done")
        self.assertIsNone(c.due_followup(state, 1600))

    def test_question_about_someone_else_does_not_mark_user_finished(self):
        for text in ["你下班了吗", "他下班了", "我还没下班了呢", "等我下班了再说"]:
            state = c.empty()
            state["threads"] = [thread()]
            self.assertEqual(c.receive_state(state, text, 2, 1500)["threads"][0]["status"], "open")

    def test_sleep_pauses_contact_until_user_returns(self):
        state = c.empty()
        state["threads"] = [thread()]
        state = c.receive_state(state, "我睡了", 2, 1500)
        self.assertIsNone(c.due_followup(state, 1600))
        state = c.receive_state(state, "醒了", 3, 1700)
        self.assertIsNotNone(c.due_followup(state, 1700))

    def test_stale_analysis_cannot_resurrect_finished_state(self):
        state = c.receive_state(c.empty(), "下班了", 9, 1500)
        result = c.apply_analysis(state, {"topic": "还在工作"}, "还在工作", "陪着你", 8, 1600)
        self.assertEqual(result, state)

    def test_pending_items_need_current_source_and_keep_realm(self):
        state = c.empty()
        state["revision"] = 3
        op = {"action": "open", "title": "等镜头结果", "purpose": "看看是否顺利", "category": "work", "subject": "user",
              "realm": "real", "evidence": "这个镜头还没改完", "delay_minutes": 1}
        state = c.apply_analysis(state, {"threads": [op]}, "这个镜头还没改完", "我陪着", 3, 1000)
        self.assertEqual(len(state["threads"]), 1)
        self.assertEqual(state["threads"][0]["due_at"], 2200)
        again = c.apply_analysis(state, {"threads": [dict(op, title="同一镜头换个说法")]}, "这个镜头还没改完", "", 3, 1001)
        self.assertEqual(len(again["threads"]), 1)
        bad = dict(op, title="没有证据", evidence="用户明天去医院")
        self.assertEqual(c.apply_analysis(state, {"threads": [bad]}, "嗯", "", 3, 1001)["threads"], state["threads"])
        state["threads"][0]["realm"] = "roleplay"
        self.assertIsNone(c.due_followup(state, 3000))

    def test_no_repeated_followup_or_expired_thread(self):
        for item in [thread(contacted_at=1300), thread(status="cancelled"), thread(expires_at=1400)]:
            state = c.empty()
            state["threads"] = [item]
            self.assertIsNone(c.due_followup(state, 1500))

    def test_short_completion_can_close_only_the_referenced_item(self):
        state = c.empty()
        state.update(revision=3, threads=[thread(), thread(id='other')])
        result = c.apply_analysis(state, {'threads': [{'action': 'done', 'id': 'work1',
            'subject': 'user', 'evidence': '好了'}]}, '好了', '那就好', 3, 1600)
        self.assertEqual(result['threads'][0]['status'], 'done')
        self.assertEqual(result['threads'][1]['status'], 'open')

    def test_memory_management_requires_an_explicit_target(self):
        self.assertEqual(c.memory_request("这条记错了"), ("clarify", None, None))
        self.assertEqual(c.memory_request("纠正记忆 #12：我喜欢热茶"), ("replace", 12, "我喜欢热茶"))
        self.assertEqual(c.memory_request("忘掉记忆 #12"), ("delete", 12, None))
        self.assertIsNone(c.memory_request("他说忘掉记忆 #12"))

    def test_reset_preserves_corrections_but_invalidates_pending_analysis(self):
        state = c.empty()
        state.update(revision=4, threads=[thread()], corrections=[{"old": "爱喝冰水"}])
        result = c.reset_transient(state)
        self.assertEqual(result["threads"], [])
        self.assertNotEqual(result["revision"], 4)
        self.assertTrue(c.blocked_memory(result, "用户爱喝冰水"))


class CursorAdapter:
    def __init__(self, db):
        self.cursor = db.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def execute(self, query, args=()):
        self.cursor.execute(query.replace("%s", "?").replace(" FOR UPDATE", ""), args)
    def fetchone(self): return self.cursor.fetchone()


class ConnectionAdapter:
    def __init__(self, db): self.db = db
    def __enter__(self): return self
    def __exit__(self, kind, *args):
        self.db.rollback() if kind else self.db.commit()
    def cursor(self): return CursorAdapter(self.db)
    def commit(self): self.db.commit()


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict(sys.modules, {name: MagicMock() for name in ["httpx", "psycopg", "psycopg.rows"]}):
            spec = importlib.util.spec_from_file_location("memory_for_continuity_tests", Path(__file__).with_name("memory_pg.py"))
            self.memory = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.memory)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE bot_settings(chat_id INTEGER,key TEXT,value TEXT,PRIMARY KEY(chat_id,key))")
        self.db.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY,chat_id INTEGER,text TEXT,kind TEXT,source_message_id INTEGER)")
        self.db.execute("INSERT INTO memories VALUES (1,10,'喜欢冰水','fact',5)")
        self.db.execute("INSERT INTO memories VALUES (2,20,'另一位用户的记忆','fact',6)")
        self.db.commit()
        self.memory._connect = lambda: ConnectionAdapter(self.db)
        self.addCleanup(self.db.close)

    def test_correction_and_audit_are_owned_and_persistent(self):
        self.assertFalse(self.memory.correct_memory(10, 2, "错误修改"))
        self.assertTrue(self.memory.correct_memory(10, 1, "喜欢热茶"))
        row = self.db.execute("SELECT text FROM memories WHERE id=1").fetchone()
        self.assertEqual(row["text"], "喜欢热茶")
        state = c.load(self.memory, 10)
        self.assertEqual(state["corrections"][0]["old"], "喜欢冰水")
        self.assertTrue(self.memory.delete_memory(10, 1))
        self.assertIsNone(self.db.execute("SELECT * FROM memories WHERE id=1").fetchone())
        self.assertIsNotNone(self.db.execute("SELECT * FROM memories WHERE id=2").fetchone())

    def test_claim_is_durable_and_can_only_happen_once(self):
        state = c.empty()
        state["threads"] = [thread(expires_at=10**12)]
        self.memory.mutate_continuity(10, lambda _: state)
        self.assertTrue(c.claim_followup(self.memory, 10, "work1"))
        self.assertFalse(c.claim_followup(self.memory, 10, "work1"))
        self.assertIsNotNone(c.load(self.memory, 10)["threads"][0]["contacted_at"])

    def test_failed_mutation_rolls_back(self):
        def fail(state):
            state["topic"] = "should not persist"
            raise RuntimeError("fail")
        with self.assertRaises(RuntimeError):
            self.memory.mutate_continuity(10, fail)
        self.assertEqual(c.load(self.memory, 10)["topic"], "")

    async def test_analyzer_result_cannot_undo_a_newer_correction(self):
        self.memory.mutate_continuity(10, lambda _: dict(c.empty(), revision=5))
        self.memory.get_relationship = lambda _: ""
        async def analyze(messages):
            self.memory.correct_memory(10, 1, "喜欢热茶")
            return {"memories": [{"text": "用户喜欢冰水", "importance": 3}], "relationship_state": "旧关系",
                    "continuity": {"topic": "旧状态"}}
        self.memory._analyzer_call = analyze
        self.memory.add_memory = MagicMock()
        self.memory.set_relationship = MagicMock()
        await self.memory.analyze_and_store(10, 5, "喜欢冰水", "嗯", [])
        self.memory.add_memory.assert_not_called()
        self.memory.set_relationship.assert_not_called()
        self.assertEqual(c.load(self.memory, 10)["topic"], "")
