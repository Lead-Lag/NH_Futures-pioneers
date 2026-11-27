"""Offline checks for queued stops, REST repair, exchange clocks and warmup.

No credentials, real account, network connection, OS event loop or socket is
created. Coroutines execute inline using the fake-only harness.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(os.environ.get("HEDGE02_SOURCE", str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

import run_live
from run_live import KST, LiveSession, MinuteBuilder
from test_execution import MemoryJournal, NoSocketAsyncCase

NOW = datetime(2026, 11, 26, 12, 0, 30, tzinfo=KST)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def tick_session():
    result = LiveSession.__new__(LiveSession)
    result.config = {"live": {"maximum_tick_age_seconds": 5}}
    result.by_symbol = {"DOMESTIC": "usdkrw"}
    result.j = MemoryJournal()
    result.latest = {}
    result.builder = SimpleNamespace(add=Mock())
    result.eng = SimpleNamespace(position=1, pending=None, entry_time=NOW - timedelta(seconds=3), on_price=Mock(return_value=None))
    result.queue_intent = Mock()
    return result


def seed_session():
    result = LiveSession.__new__(LiveSession)
    result.config = {"data": {"overseas_exchange": "FCME"}}
    result.symbols = {"usdkrw": "DOMESTIC", "eur": "6EZ26", "jpy": "6JZ26"}
    result.j = MemoryJournal()
    result.eng = SimpleNamespace(warmup=False, on_bar_close=Mock())
    result.builder = SimpleNamespace(closed={})
    result.gate = SimpleNamespace(add_bar=Mock())
    bar_start = NOW.replace(minute=57, second=0) - timedelta(hours=1)
    closed = {"start": bar_start, "open": 1330, "high": 1330.1, "low": 1329.9,
              "close": 1330, "volume": 10}
    result.overseas = SimpleNamespace(minute_bars=Mock(return_value=[]))
    result.domestic = SimpleNamespace(minute_bars=Mock(return_value=[closed]))
    result.repair_needed = True
    return result


class LiveGuardTests(NoSocketAsyncCase):
    async def test_queued_exit_waits_for_previous_task_instead_of_being_dropped(self):
        session = LiveSession.__new__(LiveSession)
        events = []
        previous = SimpleNamespace(complete=False)
        previous.done = lambda: previous.complete
        session.order_task = previous
        intent = SimpleNamespace(purpose="EXIT_STOP")
        async def execute(value):
            self.assertTrue(previous.complete)
            events.append(("execute", value.purpose))
        async def shield(task):
            self.assertIs(task, previous)
            events.append(("await_previous", None))
            previous.complete = True
        def create_task(coroutine):
            return SimpleNamespace(coroutine=coroutine)
        session.execute = execute
        with patch.object(run_live.asyncio, "create_task", side_effect=create_task), patch.object(run_live.asyncio, "shield", side_effect=shield):
            session.queue_intent(intent)
            self.assertIsNot(session.order_task, previous)
            self.assertEqual(events, [])
            await session.order_task.coroutine
        self.assertEqual(events, [("await_previous", None), ("execute", "EXIT_STOP")])

    async def test_rest_history_wait_does_not_disable_live_stop_processing(self):
        session = seed_session()
        observations = []
        async def rest_wait(function, *args, **kwargs):
            observations.append(session.eng.warmup)
            self.assertFalse(session.eng.warmup)
            return function(*args, **kwargs)
        def inject(bar):
            self.assertTrue(session.eng.warmup)
        session.eng.on_bar_close.side_effect = inject
        with patch.object(run_live, "datetime", FixedDateTime), patch.object(run_live.asyncio, "to_thread", side_effect=rest_wait):
            await session.seed_and_repair()
        self.assertEqual(observations, [False, False, False])
        self.assertFalse(session.eng.warmup)
        self.assertFalse(session.repair_needed)
        session.eng.on_bar_close.assert_called_once()

    async def test_history_injection_exception_restores_previous_warmup(self):
        session = seed_session()
        session.eng.on_bar_close.side_effect = RuntimeError("in-memory injection failure")
        with patch.object(run_live, "datetime", FixedDateTime):
            with self.assertRaisesRegex(RuntimeError, "in-memory injection failure"):
                await session.seed_and_repair()
        self.assertFalse(session.eng.warmup)
        self.assertTrue(session.repair_needed)

    async def test_stale_out_of_order_and_preentry_ticks_never_trigger_stop(self):
        cases = [
            ("stale", NOW - timedelta(seconds=6), None, NOW - timedelta(seconds=10)),
            ("out_of_order", NOW - timedelta(seconds=2), NOW - timedelta(seconds=1), NOW - timedelta(seconds=10)),
            ("preentry", NOW - timedelta(seconds=1), None, NOW - timedelta(milliseconds=500)),
        ]
        for name, timestamp, previous_timestamp, entry in cases:
            with self.subTest(case=name):
                session = tick_session()
                session.eng.entry_time = entry
                if previous_timestamp:
                    session.latest["usdkrw"] = {"timestamp": previous_timestamp.isoformat()}
                event = {"symbol": "DOMESTIC", "timestamp": timestamp.isoformat(),
                         "timestamp_verified": True, "price": 1320, "volume": 1}
                with patch.object(run_live, "datetime", FixedDateTime):
                    await session.on_trade(event)
                session.eng.on_price.assert_not_called()
                session.queue_intent.assert_not_called()

    async def test_fresh_postentry_tick_can_queue_stop(self):
        session = tick_session()
        intent = SimpleNamespace(purpose="EXIT_STOP")
        session.eng.on_price.return_value = intent
        timestamp = NOW - timedelta(seconds=1)
        event = {"symbol": "DOMESTIC", "timestamp": timestamp.isoformat(),
                 "timestamp_verified": True, "price": 1320, "volume": 1}
        with patch.object(run_live, "datetime", FixedDateTime):
            await session.on_trade(event)
        session.eng.on_price.assert_called_once_with(1320.0, timestamp)
        session.queue_intent.assert_called_once_with(intent)

    async def test_unverified_exchange_clock_is_not_used_for_stop(self):
        session = tick_session()
        event = {"symbol": "DOMESTIC", "timestamp": NOW.isoformat(),
                 "timestamp_verified": False, "price": 1320, "volume": 1}
        with patch.object(run_live, "datetime", FixedDateTime):
            await session.on_trade(event)
        session.eng.on_price.assert_not_called()
        session.builder.add.assert_not_called()

    async def test_ready_discards_partial_minute_before_first_complete_bar(self):
        session = LiveSession.__new__(LiveSession)
        session.j = MemoryJournal()
        session.feeds = [SimpleNamespace(healthy=True), SimpleNamespace(healthy=True)]
        session.feed_ready = False
        session.builder = MinuteBuilder()
        partial = NOW.replace(second=0)
        next_minute = partial + timedelta(minutes=1)
        session.builder.bars = {"eur": {partial: {"start": partial}, next_minute: {"start": next_minute}},
                                "jpy": {partial - timedelta(minutes=1): {"start": partial - timedelta(minutes=1)}}}
        with patch.object(run_live, "datetime", FixedDateTime):
            await session.on_status({"state": "ready"})
        self.assertTrue(session.feed_ready)
        self.assertEqual(session.first_complete, next_minute)
        self.assertEqual(set(session.builder.bars["eur"]), {next_minute})
        self.assertEqual(session.builder.bars["jpy"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
