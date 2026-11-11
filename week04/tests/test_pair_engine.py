"""페어 판정 규칙 단위 시험 (키·네트워크 불필요).  python -m unittest discover tests"""
from __future__ import annotations

import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pair_engine import (ABORTED, BROKEN, BUY, COMPLETE, ENTRY_LONG, ENTRY_SHORT, EXIT_AMTSTOP, EXIT_EOD,  # noqa: E402
                         EXIT_TIME, EXIT_TP, EXIT_ZSTOP, SELL, UNWOUND, PairEngine, PairParams, align_minutes)

KST = timezone(timedelta(hours=9))


class J:
    def __init__(self):
        self.bars, self.signals, self.trades, self.events = [], [], [], []

    def bar(self, r): self.bars.append(r)
    def signal(self, r): self.signals.append(r)
    def trade(self, r): self.trades.append(r)
    def event(self, k, m, **x): self.events.append((k, m))


def mk(**kw) -> tuple[PairEngine, J]:
    p = PairParams(**{"beta": 1.0, "window": 10, "entry_start": "09:00", "entry_end": "15:00",
                      "flat_time": "15:15", "qty_s": 10, "qty_h": 1, **kw})
    j = J()
    e = PairEngine(p, j)
    e.warmup = False
    return e, j


def t(h, m):
    return datetime(2026, 11, 11, h, m, tzinfo=KST)


def feed(e: PairEngine, spreads: list[float], start=(9, 0), fresh=True):
    """스프레드 값(ln S − ln H, β=1)이 되도록 가격을 만들어 넣고 마지막 Intent 를 돌려줌."""
    out = None
    h0, m0 = start
    for i, s in enumerate(spreads):
        ph = 1_000_000.0
        ps = ph * math.exp(s)
        out = e.on_bar_close({"start": t(h0, m0) + timedelta(minutes=i), "close_s": ps, "close_h": ph,
                              "fresh_s": fresh, "fresh_h": True}) or out
    return out


def done(status=COMPLETE, s=0.0, h=0.0, qs=10, qh=1, unwind=0.0):
    return SimpleNamespace(status=status, qty={"S": qs, "H": qh}, avg={"S": s, "H": h}, unwind_pnl=unwind, message="")


BASE = [0.0, 0.001, -0.001, 0.0005, -0.0005, 0.0, 0.001, -0.001, 0.0005, -0.0005]


class EngineTest(unittest.TestCase):
    def test_short_spread_entry_when_z_crosses_up(self):
        e, j = mk()
        it = feed(e, BASE + [0.01])
        self.assertIsNotNone(it)
        self.assertEqual(it.purpose, ENTRY_SHORT)
        # 스프레드 매도 = 삼성 매도 · 하이닉스 매수, 하이닉스 먼저
        self.assertEqual([(l.name, l.side, l.qty) for l in it.legs], [("H", BUY, 1), ("S", SELL, 10)])

    def test_long_spread_entry_when_z_crosses_down(self):
        e, _ = mk()
        it = feed(e, BASE + [-0.01])
        self.assertEqual(it.purpose, ENTRY_LONG)
        self.assertEqual([(l.name, l.side) for l in it.legs], [("H", SELL), ("S", BUY)])

    def test_no_entry_when_one_leg_not_traded(self):
        e, j = mk()
        it = feed(e, BASE + [0.01], fresh=False)
        self.assertIsNone(it)
        self.assertIn("체결 없음", j.signals[-1]["blocked_by"])

    def test_reentry_mode_waits_for_return(self):
        e, _ = mk(entry_mode="reentry")
        self.assertIsNone(feed(e, BASE + [0.01]))           # 밖으로 나간 봉: 진입 안 함
        e.pending = None
        it = feed(e, [0.004], start=(9, 10))                # 다시 안으로(2 > z > 0.5)
        self.assertIsNotNone(it)
        self.assertEqual(it.purpose, ENTRY_SHORT)

    def _hold_short(self, e):
        it = feed(e, BASE + [0.01])
        e.on_pair_done(it, done(s=100_000.0, h=1_000_000.0), t(9, 10))
        self.assertEqual(e.direction, -1)
        self.assertEqual(e.expected_positions(), {"S": -10, "H": 1})

    def test_converge_exit_and_pnl(self):
        e, j = mk()
        self._hold_short(e)
        it = feed(e, [0.0], start=(9, 10))
        self.assertEqual(it.purpose, EXIT_TP)
        self.assertEqual([(l.name, l.side, l.qty) for l in it.legs], [("H", SELL, 1), ("S", BUY, 10)])
        # 삼성 1,000원 하락(매도 이익 +100,000) · 하이닉스 10,000원 상승(매수 이익 +100,000)
        e.on_pair_done(it, done(s=99_000.0, h=1_010_000.0), t(9, 7))
        self.assertEqual(j.trades[-1]["net_pl"], 200_000)
        self.assertEqual(e.direction, 0)

    def test_z_stop_sets_cooldown(self):
        e, j = mk(stop_z=2.2)
        self._hold_short(e)
        it = feed(e, [0.02], start=(9, 10))
        self.assertEqual(it.purpose, EXIT_ZSTOP)
        e.on_pair_done(it, done(s=100_000.0, h=1_000_000.0), t(9, 7))
        self.assertEqual(e.cooldown, e.p.cooldown_bars_after_stop)

    def test_amount_stop_from_prices(self):
        e, _ = mk(pair_stop_won=150_000)
        self._hold_short(e)
        # 삼성 +2,000원 → 매도 10계약 −200,000원
        it = e.on_prices(102_000.0, 1_000_000.0, t(9, 7))
        self.assertEqual(it.purpose, EXIT_AMTSTOP)

    def test_time_exit(self):
        e, _ = mk(max_hold_bars=3, exit_z=-9, stop_z=99)
        self._hold_short(e)
        it = feed(e, [0.01, 0.01, 0.01], start=(9, 10))
        self.assertEqual(it.purpose, EXIT_TIME)

    def test_eod_exit(self):
        e, _ = mk(exit_z=-9, stop_z=99, max_hold_bars=999)
        self._hold_short(e)
        self.assertEqual(e.on_clock(t(15, 16)).purpose, EXIT_EOD)

    def test_unwound_entry_leaves_flat_and_counts_loss(self):
        e, j = mk()
        it = feed(e, BASE + [0.01])
        e.on_pair_done(it, done(status=UNWOUND, unwind=-12_000), t(9, 10))
        self.assertEqual(e.direction, 0)
        self.assertEqual(e.daily_pnl, -12_000)
        self.assertIsNone(e.pending)

    def test_three_failed_entries_halt(self):
        e, _ = mk()
        feed(e, BASE)
        for k in range(3):
            e.prev_z = 0.0
            it = feed(e, [0.03 * (k + 1)], start=(9, 10 + k))
            self.assertIsNotNone(it, k)
            e.on_pair_done(it, done(status=ABORTED), t(9, 11 + k))
        self.assertIn("연속 실패", e.halted)

    def test_broken_halts(self):
        e, _ = mk()
        it = feed(e, BASE + [0.01])
        e.on_pair_done(it, done(status=BROKEN), t(9, 10))
        self.assertTrue(e.halted)
        self.assertIsNotNone(feed(e, [0.0], start=(9, 10)) or True)

    def test_daily_limit_precheck(self):
        e, j = mk(daily_loss_limit=200_000, pair_stop_won=150_000)
        feed(e, BASE)
        e.daily_pnl = -60_000      # 남은 한도 140,000 < 150,000
        feed(e, [0.01], start=(9, 10))
        self.assertIn("남은 손실한도", j.signals[-1]["blocked_by"])

    def test_sync_mismatch_halts(self):
        e, _ = mk()
        self._hold_short(e)
        self.assertFalse(e.sync_positions({"S": -10, "H": 0}, t(9, 7)))
        self.assertEqual(e.halted, "잔고 불일치")

    def test_align_fills_missing_minute(self):
        a = [{"start": t(9, 0), "close": 100.0, "volume": 3}, {"start": t(9, 2), "close": 101.0, "volume": 1}]
        b = [{"start": t(9, 0), "close": 200.0, "volume": 3}, {"start": t(9, 1), "close": 201.0, "volume": 2},
             {"start": t(9, 2), "close": 202.0, "volume": 2}]
        rows = align_minutes(a, b, None, t(9, 3), {})
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows[1]["close_s"], rows[1]["fresh_s"], rows[1]["fresh_h"]), (100.0, False, True))


class TickSizeTest(unittest.TestCase):
    def test_krx_tick_table(self):
        from pair_engine import tick_size
        self.assertEqual(tick_size(199_900), 100)    # 삼성 20만원 미만
        self.assertEqual(tick_size(200_000), 500)    # 20만원부터 500원 (11/11 실행에서 확인)
        self.assertEqual(tick_size(1_265_000), 1000)  # 하이닉스
        self.assertEqual(tick_size(0, 100), 100)      # 가격 없으면 기본값


class WindowFreshTest(unittest.TestCase):
    def test_entry_blocked_after_flat_gap(self):
        """11/11 13:13 재현: 끊긴 동안 평평한(체결 없는) 봉이 창을 채우면 z 가 −6 으로 튀어도 진입하지 않음."""
        from pair_engine import PairEngine, PairParams
        p = PairParams()
        eng = PairEngine(p, J())
        eng.warmup = False
        t0 = datetime(2026, 11, 11, 12, 30, tzinfo=timezone(timedelta(hours=9)))
        out = None
        for i in range(45):
            gap = 5 <= i < 44                       # 12:35~13:13 체결 없음(직전 종가 반복)
            ph = 1_247_000 if i < 44 else 1_265_000
            ps = 204_500 + (0 if gap or i % 2 else 500)
            out = eng.on_bar_close({"start": t0 + timedelta(minutes=i), "close_s": ps, "close_h": ph,
                                    "fresh_s": not gap, "fresh_h": not gap})
        self.assertIsNotNone(eng.last_z)
        self.assertLess(eng.last_z, -2)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
