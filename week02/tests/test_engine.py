"""전략 규칙이 README 에 적은 대로 동작하는지 확인하는 단위 시험 (python -m unittest discover tests)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import BUY, ENTRY_LONG, ENTRY_SHORT, EXIT_EOD, EXIT_STOP, EXIT_TIME, EXIT_TP, SELL, Engine, Params  # noqa: E402

KST = timezone(timedelta(hours=9))


class NullJournal:
    def __init__(self):
        self.signals, self.trades, self.events = [], [], []

    def bar(self, row): pass
    def signal(self, row): self.signals.append(row)
    def trade(self, row): self.trades.append(row)
    def event(self, kind, message, **extra): self.events.append((kind, message))


def bar(t, c, o=None):
    o = c if o is None else o
    return {"start": t, "open": o, "high": max(o, c), "low": min(o, c), "close": c}


def warm(eng, start, n=20, base=1380.0):
    """밴드폭이 넉넉하도록 ±0.6원 지그재그 20봉."""
    t = start
    for i in range(n):
        eng.on_bar_close(bar(t, base + (0.6 if i % 2 else -0.6)))
        t += timedelta(minutes=1)
    return t


class EngineRules(unittest.TestCase):
    def setUp(self):
        self.j = NullJournal()
        self.p = Params()
        self.e = Engine(self.p, self.j)
        self.e.warmup = False
        self.t0 = datetime(2026, 10, 26, 9, 10, tzinfo=KST)

    def test_long_entry_after_reentry_from_below(self):
        t = warm(self.e, self.t0)
        self.assertIsNone(self.e.on_bar_close(bar(t, 1377.0)))  # 하단 이탈 — 아직 신호 아님
        it = self.e.on_bar_close(bar(t + timedelta(minutes=1), 1379.6))  # 밴드 안으로 복귀
        self.assertIsNotNone(it)
        self.assertEqual((it.side, it.purpose), (BUY, ENTRY_LONG))
        self.assertEqual(self.j.signals[-1]["taken"], 1)

    def test_short_entry_after_reentry_from_above(self):
        t = warm(self.e, self.t0)
        self.e.on_bar_close(bar(t, 1383.0))
        it = self.e.on_bar_close(bar(t + timedelta(minutes=1), 1380.4))
        self.assertEqual((it.side, it.purpose), (SELL, ENTRY_SHORT))

    def test_still_outside_is_not_a_signal(self):
        t = warm(self.e, self.t0)
        self.e.on_bar_close(bar(t, 1377.0))
        self.assertIsNone(self.e.on_bar_close(bar(t + timedelta(minutes=1), 1376.0)))

    def test_narrow_band_blocks_entry(self):
        t = self.t0
        for i in range(20):
            self.e.on_bar_close(bar(t, 1380.0 + (0.1 if i % 2 else 0.0)))
            t += timedelta(minutes=1)
        self.e.on_bar_close(bar(t, 1379.7))
        self.assertIsNone(self.e.on_bar_close(bar(t + timedelta(minutes=1), 1380.0)))
        self.assertIn("밴드폭", self.j.signals[-1]["blocked_by"])

    def _enter_long(self):
        t = warm(self.e, self.t0)
        self.e.on_bar_close(bar(t, 1377.0))
        it = self.e.on_bar_close(bar(t + timedelta(minutes=1), 1379.6))
        self.e.on_fill(it, 1379.7, 1, t + timedelta(minutes=2))
        return t + timedelta(minutes=2)

    def test_stop_loss_from_fill_price(self):
        t = self._enter_long()
        self.assertAlmostEqual(self.e.stop_price, 1379.2)  # 1379.7 − 5틱
        self.assertIsNone(self.e.on_price(1379.3, t))
        it = self.e.on_price(1379.2, t)
        self.assertEqual((it.side, it.purpose), (SELL, EXIT_STOP))
        self.e.on_fill(it, 1379.1, 1, t)
        self.assertEqual(self.j.trades[-1]["net_pl"], -6000)  # 6틱 × 1,000원
        self.assertEqual(self.e.cooldown, self.p.cooldown_bars_after_stop)

    def test_take_profit_at_mid(self):
        t = self._enter_long()
        it = self.e.on_bar_close(bar(t, 1380.5))
        self.assertEqual(it.purpose, EXIT_TP)

    def test_time_exit(self):
        t = self._enter_long()
        self.p.max_hold_bars = 2
        self.assertIsNone(self.e.on_bar_close(bar(t, 1379.5)))
        it = self.e.on_bar_close(bar(t + timedelta(minutes=1), 1379.5))
        self.assertEqual(it.purpose, EXIT_TIME)

    def test_eod_flat(self):
        self._enter_long()
        it = self.e.on_clock(datetime(2026, 10, 26, 15, 30, 1, tzinfo=KST))
        self.assertEqual(it.purpose, EXIT_EOD)

    def test_daily_loss_precheck(self):
        self.e.day = self.t0.date()
        t = warm(self.e, self.t0)
        self.e.daily_pnl = -25_001  # 남은 한도 4,999원 < 1회 손절액 5,000원 → 막음
        self.e.on_bar_close(bar(t, 1377.0))
        self.assertIsNone(self.e.on_bar_close(bar(t + timedelta(minutes=1), 1379.6)))
        self.assertIn("남은 손실한도", self.j.signals[-1]["blocked_by"])

    def test_entry_window(self):
        t = warm(self.e, datetime(2026, 10, 26, 15, 0, tzinfo=KST))
        self.e.on_bar_close(bar(t, 1377.0))
        self.assertIsNone(self.e.on_bar_close(bar(t + timedelta(minutes=1), 1379.6)))
        self.assertEqual(self.j.signals[-1]["blocked_by"], "진입시간 밖")


if __name__ == "__main__":
    unittest.main()
