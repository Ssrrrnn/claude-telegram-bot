"""Reliability regression tests for the proactive delivery pipeline.

Covers the P0 fixes on fix/morrow-proactive-reliability:

  F1'  durable delivery receipts (attempting / success / uncertain / failed)
       reusing companion_actions, without changing cooldown-before-delivery or
       the "never auto-retry a possibly-delivered message" rule
  F4'  background-task fault isolation in proactive_loop
  F15  a plain overnight goodnight must not swallow the next morning greeting,
       while an explicit "extended rest / do not disturb" request always wins
  F16  tzdata / ZoneInfo availability (also guards the Docker test image)
  F12  post_init wiring: single call creates one task, repeated calls stay
       idempotent, and the captured original is not the patched function
  SEC  log safety: delivery logs carry no exception text and no message body,
       and the loop diagnostics redact credential shapes

Hermetic: no database, no network, no real messages, no paid model calls. The
module under test is loaded with the same stubbed-`runner` technique that
test_proactive.py uses, so this file cannot affect (or be affected by) other
test modules sharing the process.
"""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import continuity

MODULE_PATH = Path(__file__).with_name("runner_strict_bubbles.py")
TZ = ZoneInfo("Asia/Shanghai")


class FakeActions:
    """In-memory stand-in for companion_actions, honouring UNIQUE(chat_id, action_key)."""

    def __init__(self, *, begin_error=None):
        self.rows = {}
        self.kinds = {}
        self.begin_error = begin_error
        self.begin_calls = 0

    def begin_action(self, chat_id, action_key, kind):
        self.begin_calls += 1
        if self.begin_error is not None:
            raise self.begin_error
        key = (chat_id, action_key)
        if key in self.rows:
            return False
        self.rows[key] = "attempting"
        self.kinds[key] = kind
        return True

    def finish_action(self, chat_id, action_key, status, remote_id=None):
        key = (chat_id, action_key)
        if self.rows.get(key) != "attempting":
            return
        self.rows[key] = status

    def statuses(self):
        return sorted(self.rows.values())

    def keys(self):
        return [k[1] for k in self.rows]


class Harness:
    """Builds an isolated copy of runner_strict_bubbles with stubbed collaborators.

    ``settings`` / ``actions`` / ``saved`` can be injected so two Harness
    instances can share persisted state, which models two processes (or a
    restart) talking to the same database.
    """

    def __init__(self, *, with_receipts=True, begin_error=None, name="reliability_under_test",
                 settings=None, actions=None, saved=None):
        self.settings = settings if settings is not None else {}
        self.saved = saved if saved is not None else []
        self._cleanups = []
        if actions is not None:
            self.actions = actions
        elif with_receipts:
            self.actions = FakeActions(begin_error=begin_error)
        else:
            self.actions = None

        memory = types.SimpleNamespace(
            get_setting=lambda chat, key: self.settings.get((chat, key)),
            set_setting=lambda chat, key, value: self.settings.__setitem__((chat, key), value),
            save_recent_proactive=lambda chat, text: self.saved.append(text),
        )

        def mutate(chat, operation):
            state = operation(continuity.decode(self.settings.get((chat, continuity.KEY))))
            self.settings[(chat, continuity.KEY)] = json.dumps(state)
            return state

        memory.mutate_continuity = mutate
        if self.actions is not None:
            memory.begin_action = self.actions.begin_action
            memory.finish_action = self.actions.finish_action
        self.memory = memory

        self.bot = types.SimpleNamespace(
            build_system_prompt=lambda chat: "persona",
            TELEGRAM_OWNER_ID=1,
            histories=defaultdict(list),
            chat_locks=defaultdict(asyncio.Lock),
            is_authorized=lambda chat: True,
            current_model=lambda chat: "mock",
            ask_claude=AsyncMock(return_value="刚想到你昨天说的那本书。"),
            MAX_HISTORY_MESSAGES=40,
        )
        self.runner = types.SimpleNamespace(
            bot=self.bot,
            memory_pg=memory,
            handle_text_with_memory=AsyncMock(),
            post_init=AsyncMock(),
            _looks_like_emoji_only=lambda text: False,
        )
        constants = types.ModuleType("telegram.constants")
        constants.ParseMode = types.SimpleNamespace(MARKDOWN="Markdown")
        with patch.dict(sys.modules, {"runner": self.runner, "telegram.constants": constants}):
            spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)

        self.telegram = types.SimpleNamespace(
            bot=types.SimpleNamespace(send_message=AsyncMock()),
            bot_data={},
        )

    def start(self, when, *, schedule=True):
        """Freeze the module clock at *when* and (optionally) make it due."""
        for p in (patch.object(self.app, "_now_local", return_value=when),
                  patch.object(self.app.time, "time", return_value=when.timestamp()),
                  patch.object(self.app.voice_output, "auto_spontaneous", return_value=False)):
            p.start()
            self._cleanups.append(p.stop)
        if schedule:
            self.settings[(1, self.app.KEY_NEXT_AT)] = str(int(when.timestamp()) - 1)

    def stop(self):
        for fn in reversed(self._cleanups):
            fn()


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    """F1': delivery receipts, cooldown ordering and failure classification."""

    NOW = datetime(2026, 9, 18, 14, 0, tzinfo=TZ)

    def setUp(self):
        self.h = Harness()
        self.h.start(self.NOW)
        self.addCleanup(self.h.stop)

    async def test_successful_delivery_records_success_receipt(self):
        await self.h.app._proactive_tick(self.h.telegram)
        self.assertGreater(self.h.telegram.bot.send_message.await_count, 0)
        self.assertEqual(self.h.actions.statuses(), ["success"])
        self.assertGreater(int(self.h.settings[(1, self.h.app.KEY_NEXT_AT)]), self.NOW.timestamp())
        self.assertIn((1, self.h.app.KEY_LAST_PROACTIVE_AT), self.h.settings)

    async def test_timeout_after_cooldown_is_uncertain_not_success(self):
        self.h.telegram.bot.send_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.h.app._proactive_tick(self.h.telegram)
        self.assertGreater(int(self.h.settings[(1, self.h.app.KEY_NEXT_AT)]), self.NOW.timestamp())
        self.assertEqual(self.h.actions.statuses(), ["uncertain"])
        self.assertNotIn((1, self.h.app.KEY_LAST_PROACTIVE_AT), self.h.settings)
        self.assertEqual(self.h.app._load_state(1)["silence_count"], 0)

    async def test_uncertain_is_never_retried_on_the_next_tick(self):
        self.h.telegram.bot.send_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.h.app._proactive_tick(self.h.telegram)
        self.assertEqual(self.h.telegram.bot.send_message.await_count, 1)
        self.h.telegram.bot.send_message.side_effect = None
        await self.h.app._proactive_tick(self.h.telegram)
        self.assertEqual(self.h.telegram.bot.send_message.await_count, 1)
        self.assertEqual(self.h.actions.statuses(), ["uncertain"])

    async def test_uncertain_is_not_retried_after_a_restart(self):
        """A fresh process re-reading the same persisted state must not resend."""
        settings, actions = {}, FakeActions()
        first = Harness(settings=settings, actions=actions)
        first.start(self.NOW)
        self.addCleanup(first.stop)
        first.telegram.bot.send_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await first.app._proactive_tick(first.telegram)
        self.assertEqual(actions.statuses(), ["uncertain"])

        second = Harness(settings=settings, actions=actions)
        second.start(self.NOW, schedule=False)  # do not touch the persisted schedule
        self.addCleanup(second.stop)
        await second.app._proactive_tick(second.telegram)
        second.telegram.bot.send_message.assert_not_awaited()
        self.assertEqual(actions.statuses(), ["uncertain"])

    async def test_two_instances_competing_for_one_slot_send_once(self):
        """Both instances read the pre-send state, as in a real replica race."""
        settings, actions, saved = {}, FakeActions(), []
        a = Harness(settings=settings, actions=actions, saved=saved)
        a.start(self.NOW)
        self.addCleanup(a.stop)
        b = Harness(settings=settings, actions=actions, saved=saved)
        b.start(self.NOW)
        self.addCleanup(b.stop)

        await a.app._proactive_tick(a.telegram)
        self.assertGreater(a.telegram.bot.send_message.await_count, 0)

        # instance B had already read the old schedule and the old contact clock
        settings[(1, a.app.KEY_NEXT_AT)] = str(int(self.NOW.timestamp()) - 1)
        settings.pop((1, a.app.KEY_LAST_PROACTIVE_AT), None)
        await b.app._proactive_tick(b.telegram)

        b.telegram.bot.send_message.assert_not_awaited()
        self.assertEqual(actions.statuses(), ["success"])
        self.assertEqual(len(actions.keys()), 1)

    async def test_failure_before_any_transport_is_recorded_as_failed(self):
        with patch.object(self.h.app.voice_output, "mode", side_effect=RuntimeError("voice mode unavailable")):
            with self.assertRaises(RuntimeError):
                await self.h.app._proactive_tick(self.h.telegram)
        self.h.telegram.bot.send_message.assert_not_awaited()
        self.assertEqual(self.h.actions.statuses(), ["failed"])

    async def test_receipt_claim_is_fail_closed(self):
        """If the receipt cannot be written, nothing may be sent."""
        h = Harness(begin_error=ConnectionError("receipt store down"))
        h.start(self.NOW)
        self.addCleanup(h.stop)
        with self.assertRaises(ConnectionError):
            await h.app._proactive_tick(h.telegram)
        h.telegram.bot.send_message.assert_not_awaited()
        self.assertEqual(h.actions.begin_calls, 1)

    async def test_already_claimed_slot_does_not_send(self):
        key = f"proactive:{int(self.NOW.timestamp())}"
        self.h.actions.rows[(1, key)] = "attempting"
        await self.h.app._proactive_tick(self.h.telegram)
        self.h.telegram.bot.send_message.assert_not_awaited()
        self.assertEqual(self.h.actions.rows[(1, key)], "attempting")

    async def test_memory_layer_without_receipts_still_delivers(self):
        h = Harness(with_receipts=False)
        h.start(self.NOW)
        self.addCleanup(h.stop)
        await h.app._proactive_tick(h.telegram)
        self.assertGreater(h.telegram.bot.send_message.await_count, 0)
        self.assertIn((1, h.app.KEY_LAST_PROACTIVE_AT), h.settings)

    async def test_degraded_receipts_are_reported_and_still_deduplicate(self):
        """Missing receipt helpers must not silently disable duplicate protection."""
        h = Harness(with_receipts=False)
        h.start(self.NOW)
        self.addCleanup(h.stop)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            await h.app._proactive_tick(h.telegram)
        self.assertGreater(h.telegram.bot.send_message.await_count, 0)
        self.assertIn("duplicate protection is in-process only", buffer.getvalue())

        # the in-process claim still blocks a second delivery of the same slot
        h.settings[(1, h.app.KEY_NEXT_AT)] = str(int(self.NOW.timestamp()) - 1)
        h.settings.pop((1, h.app.KEY_LAST_PROACTIVE_AT), None)
        h.telegram.bot.send_message.reset_mock()
        with contextlib.redirect_stdout(io.StringIO()):
            await h.app._proactive_tick(h.telegram)
        h.telegram.bot.send_message.assert_not_awaited()

    async def test_cancellation_records_uncertain_and_propagates(self):
        self.h.telegram.bot.send_message.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.h.app._proactive_tick(self.h.telegram)
        self.assertEqual(self.h.actions.statuses(), ["uncertain"])


class GoodnightMorningBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """F15: plain goodnight vs explicit extended-rest request."""

    MORNING = datetime(2026, 9, 18, 9, 0, tzinfo=TZ)

    def _pause_state(self, value, expires_at):
        state = continuity.decode(None)
        state["status"] = {"value": value, "source": "x", "expires_at": expires_at.timestamp()}
        return json.dumps(state)

    async def test_plain_0230_goodnight_still_allows_the_morning_greeting(self):
        goodnight = datetime(2026, 9, 18, 2, 30, tzinfo=TZ)
        h = Harness()
        h.start(self.MORNING)
        self.addCleanup(h.stop)
        h.settings[(1, continuity.KEY)] = self._pause_state(
            continuity.SLEEP_PAUSE_VALUE, goodnight + timedelta(hours=8))

        self.assertTrue(h.app._morning_is_due(1))
        await h.app._proactive_tick(h.telegram)
        self.assertGreater(h.telegram.bot.send_message.await_count, 0)
        self.assertEqual(h.actions.statuses(), ["success"])

    async def test_explicit_morning_dnd_blocks_the_morning_greeting(self):
        h = Harness()
        h.start(self.MORNING)
        self.addCleanup(h.stop)
        h.settings[(1, continuity.KEY)] = self._pause_state(
            continuity.REST_PAUSE_VALUE, self.MORNING + timedelta(hours=2))

        self.assertTrue(h.app._morning_is_due(1))
        await h.app._proactive_tick(h.telegram)
        h.telegram.bot.send_message.assert_not_awaited()

    async def test_sleeping_until_noon_blocks_the_morning_greeting(self):
        """End to end: the phrase '睡到中午' produces a pause that wins."""
        state = continuity.receive_state(
            continuity.decode(None), "睡到中午", 1, self.MORNING.timestamp())
        self.assertEqual(state["status"]["value"], continuity.REST_PAUSE_VALUE)
        h = Harness()
        h.start(self.MORNING)
        self.addCleanup(h.stop)
        h.settings[(1, continuity.KEY)] = json.dumps(state)

        await h.app._proactive_tick(h.telegram)
        h.telegram.bot.send_message.assert_not_awaited()

    async def test_pause_still_suppresses_non_morning_contact(self):
        afternoon = datetime(2026, 9, 18, 14, 0, tzinfo=TZ)
        expiry = afternoon + timedelta(hours=2)
        h = Harness()
        h.start(afternoon)
        self.addCleanup(h.stop)
        h.settings[(1, continuity.KEY)] = self._pause_state(continuity.SLEEP_PAUSE_VALUE, expiry)

        self.assertFalse(h.app._morning_is_due(1))
        await h.app._proactive_tick(h.telegram)
        h.telegram.bot.send_message.assert_not_awaited()
        self.assertEqual(int(h.settings[(1, h.app.KEY_NEXT_AT)]), int(expiry.timestamp()))

    async def test_morning_greeting_is_not_repeated_the_same_day(self):
        h = Harness()
        h.start(self.MORNING)
        self.addCleanup(h.stop)
        h.settings[(1, continuity.KEY)] = self._pause_state(
            continuity.SLEEP_PAUSE_VALUE, self.MORNING + timedelta(hours=1))

        await h.app._proactive_tick(h.telegram)
        self.assertGreater(h.telegram.bot.send_message.await_count, 0)
        self.assertFalse(h.app._first_morning_contact(1))
        h.telegram.bot.send_message.reset_mock()
        h.settings[(1, h.app.KEY_NEXT_AT)] = str(int(self.MORNING.timestamp()) - 1)
        await h.app._proactive_tick(h.telegram)
        h.telegram.bot.send_message.assert_not_awaited()


class RestRequestRecognitionTests(unittest.TestCase):
    """A pause may only be created by an explicit, well-formed request."""

    def test_explicit_rest_requests_are_recognised(self):
        for text in ("早上不要联系我", "明天早上别找我", "上午不要打扰我",
                     "我要睡到中午", "睡到中午再说", "今天不要联系我",
                     "暂时别打扰我了", "早上不要联系我了"):
            with self.subTest(text=text):
                self.assertTrue(continuity.rest_request(text))

    def test_fuzzy_or_unrelated_text_never_creates_a_pause(self):
        for text in ("早上好", "我明天早上要开会", "别打扰他", "不要联系别人",
                     "他说早上不要联系我", "我今天不忙", "早上想跟你聊聊", ""):
            with self.subTest(text=text):
                self.assertFalse(continuity.rest_request(text))

    def test_rest_pause_lapses_at_noon_local(self):
        morning = datetime(2026, 9, 18, 9, 0, tzinfo=TZ).timestamp()
        self.assertEqual(
            datetime.fromtimestamp(continuity.rest_expiry(morning), TZ).isoformat(),
            "2026-09-18T12:00:00+08:00")
        night = datetime(2026, 9, 18, 23, 0, tzinfo=TZ).timestamp()
        self.assertEqual(
            datetime.fromtimestamp(continuity.rest_expiry(night), TZ).isoformat(),
            "2026-09-19T12:00:00+08:00")

    def test_rest_pause_survives_an_ordinary_later_turn(self):
        now = datetime(2026, 9, 18, 9, 0, tzinfo=TZ).timestamp()
        state = continuity.receive_state(continuity.decode(None), "睡到中午", 1, now)
        again = continuity.receive_state(state, "在吗", 2, now + 60)
        self.assertEqual(again["status"].get("value"), continuity.REST_PAUSE_VALUE)

    def test_plain_goodnight_is_still_cancelled_by_a_new_turn(self):
        now = datetime(2026, 9, 18, 9, 0, tzinfo=TZ).timestamp()
        state = continuity.receive_state(continuity.decode(None), "晚安", 1, now)
        self.assertEqual(state["status"]["value"], continuity.SLEEP_PAUSE_VALUE)
        again = continuity.receive_state(state, "在吗", 2, now + 60)
        self.assertEqual(again["status"], {})


class LoopIsolationTests(unittest.IsolatedAsyncioTestCase):
    """F4': one failing background job must not skip the others."""

    NOW = datetime(2026, 9, 18, 14, 0, tzinfo=TZ)

    def _harness(self):
        h = Harness()
        h.start(self.NOW)
        self.addCleanup(h.stop)
        return h

    async def test_daily_task_runs_even_when_proactive_tick_fails(self):
        h = self._harness()
        daily = AsyncMock()
        sleeps = {"n": 0}

        async def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                raise asyncio.CancelledError()

        async def boom(_application):
            raise ConnectionError("proactive path down")

        with patch.object(h.app, "_proactive_tick", side_effect=boom), \
             patch.dict(os.environ, {"MORROW_ONLY": "0"}), \
             patch("companion_features.auto_diary_tick", daily), \
             patch.object(h.app.asyncio, "sleep", side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await h.app.proactive_loop(h.telegram)

        daily.assert_awaited_once()

    async def test_diary_publish_runs_when_daily_task_fails(self):
        h = self._harness()
        publish = AsyncMock()
        h.telegram.bot_data["qq_connector"] = types.SimpleNamespace(publish_pending_diary=publish)
        sleeps = {"n": 0}

        async def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                raise asyncio.CancelledError()

        async def boom(_application):
            raise RuntimeError("tick down")

        with patch.object(h.app, "_proactive_tick", side_effect=boom), \
             patch.dict(os.environ, {"MORROW_ONLY": "0"}), \
             patch("companion_features.auto_diary_tick", AsyncMock(side_effect=RuntimeError("daily down"))), \
             patch.object(h.app.asyncio, "sleep", side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await h.app.proactive_loop(h.telegram)

        publish.assert_awaited_once()


class LogSafetyTests(unittest.IsolatedAsyncioTestCase):
    """SEC: no exception text, credentials or message bodies in proactive logs."""

    NOW = datetime(2026, 9, 18, 14, 0, tzinfo=TZ)
    BODY = "刚想到你昨天说的那本书。"

    def setUp(self):
        self.h = Harness()
        self.h.start(self.NOW)
        self.addCleanup(self.h.stop)

    async def test_delivery_failure_log_has_no_exception_text_or_message_body(self):
        # Deliberately synthetic, credential-shaped fixtures: they must be
        # redacted, and they are not valid credentials for any provider.
        secret = "sk-FIXTURENOTAREALKEY01"
        private = "很私密的一句话，不该出现在日志里"
        self.h.telegram.bot.send_message.side_effect = RuntimeError(f"{secret} {private}")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(RuntimeError):
                await self.h.app._proactive_tick(self.h.telegram)

        logged = buffer.getvalue()
        self.assertNotIn(secret, logged)
        self.assertNotIn(private, logged)
        self.assertNotIn(self.BODY, logged)
        # structured, safe diagnostics are still present
        self.assertIn("reason=runtime", logged)
        self.assertIn("error=RuntimeError", logged)
        self.assertIn("op=proactive:", logged)

    def test_safe_error_redacts_several_credential_shapes(self):
        cases = (
            ("postgresql://user:FIXTUREPW@db:5432/app", "FIXTUREPW"),
            ("Authorization: Bearer FIXTUREBEARER01", "FIXTUREBEARER01"),
            ("using key sk-FIXTUREKEY01", "sk-FIXTUREKEY01"),
            ("api_key=FIXTUREVALUE01", "FIXTUREVALUE01"),
            ("https://api.example.com/v1?token=FIXTUREQUERY01&x=1", "FIXTUREQUERY01"),
            ("password: FIXTUREPASS01", "FIXTUREPASS01"),
        )
        for raw, secret in cases:
            with self.subTest(secret=secret):
                out = self.h.app._safe_error(RuntimeError(raw))
                self.assertNotIn(secret, out)

    def test_safe_error_bounds_length(self):
        self.assertLessEqual(len(self.h.app._safe_error(RuntimeError("x" * 5000))), 400)

    def test_reason_codes_are_fixed_and_message_independent(self):
        app = self.h.app
        self.assertEqual(app._reason_code(TimeoutError()), "timeout")
        self.assertEqual(app._reason_code(ConnectionError()), "transport")
        self.assertEqual(app._reason_code(asyncio.CancelledError()), "cancelled")
        self.assertEqual(app._reason_code(RuntimeError()), "runtime")
        self.assertEqual(app._reason_code(ValueError()), "bad-value")
        self.assertEqual(app._reason_code(ZeroDivisionError("sekret")), "error")


class TimezoneAndInitTests(unittest.TestCase):
    """F16 (tzdata) and F12 (post_init wiring / single background task)."""

    def setUp(self):
        self.h = Harness()

    def test_proactive_timezone_is_beijing(self):
        self.assertEqual(str(self.h.app.PROACTIVE_TZ), "Asia/Shanghai")

    def test_zoneinfo_resolves_and_is_utc_plus_8(self):
        """Guards the tzdata dependency: fails if the IANA database is missing."""
        self.assertEqual(datetime(2026, 9, 18, 12, tzinfo=TZ).utcoffset(), timedelta(hours=8))

    def test_now_local_is_timezone_aware(self):
        value = self.h.app._now_local()
        self.assertIsNotNone(value.tzinfo)
        self.assertEqual(value.utcoffset(), timedelta(hours=8))

    def test_captured_original_post_init_is_not_the_patched_function(self):
        app = self.h.app
        self.assertIs(app.runner.post_init, app.post_init_with_proactive)
        self.assertIsNot(app._original_post_init, app.post_init_with_proactive)

    class _FakeApplication:
        def __init__(self):
            self.bot_data = {}

        def create_task(self, coro):
            self.created.append(coro)
            coro.close()
            return object()

    def _run_post_init(self, calls):
        app = self.h.app
        created = []
        fake = self._FakeApplication()
        fake.created = created
        original = app._original_post_init
        mock = AsyncMock()
        app._original_post_init = mock
        try:
            for _ in range(calls):
                asyncio.run(app.post_init_with_proactive(fake))
        finally:
            app._original_post_init = original
        return created, mock

    def test_post_init_single_call_creates_exactly_one_task(self):
        created, mock = self._run_post_init(1)
        self.assertEqual(len(created), 1)
        mock.assert_awaited_once()

    def test_post_init_repeated_calls_stay_idempotent(self):
        """A repeated initialisation must not create a second proactive loop."""
        created, mock = self._run_post_init(3)
        self.assertEqual(len(created), 1)
        self.assertEqual(mock.await_count, 3)

    def test_delivery_receipts_detected_only_when_available(self):
        self.assertTrue(self.h.app._delivery_receipts_available())
        self.assertFalse(Harness(with_receipts=False).app._delivery_receipts_available())


if __name__ == "__main__":
    unittest.main()
