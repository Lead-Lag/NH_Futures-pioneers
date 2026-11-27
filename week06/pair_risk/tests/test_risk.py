"""6주차 위험관리 규칙 시험 (키·네트워크 불필요).  python -m unittest discover tests"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_engine import (BUY, COMPLETE, ENTRY_ADD, ENTRY_SHORT, EXIT_AMTSTOP, EXIT_MONITOR, EXIT_PARTIAL,  # noqa: E402
                         EXIT_TIME, EXIT_TP, EXIT_ZSTOP, PairParams)
from risk_monitor import Monitor, ShadowBook  # noqa: E402
from test_pair_engine import BASE, J, done, feed, mk, t  # noqa: E402


def enter_short(e, j, v: float = 0.02, base=None):
    """BASE 뒤 양의 스프레드로 스프레드 매도 진입(하이닉스 1,000,000 기준 가격)."""
    it = feed(e, (base or BASE) + [v])
    assert it is not None and it.purpose == ENTRY_SHORT, it
    e.on_pair_done(it, done(s=it.ref["S"], h=it.ref["H"], qs=it.legs[1].qty, qh=it.legs[0].qty), it.ts)
    return it


class RiskOffTest(unittest.TestCase):
    def test_no_stops_no_limits(self):
        e, j = mk(risk_off=True, stop_z=2.5, max_hold_bars=2, pair_stop_won=1)
        enter_short(e, j)
        # 금액 손절선 1원·z 손절 2.5·시간 2봉이어도 risk_off 면 아무것도 안 함
        self.assertIsNone(e.on_prices(e.entry_px["S"] * 1.01, e.entry_px["H"], t(9, 12)))
        for k in range(5):
            it = e.on_bar_close({"start": t(9, 11) + timedelta(minutes=k), "close_s": e.entry_px["S"] * 1.03,
                                 "close_h": e.entry_px["H"], "fresh_s": True, "fresh_h": True})
            self.assertIsNone(it)
        self.assertEqual(e.direction, -1)

    def test_daily_limit_ignored(self):
        e, j = mk(risk_off=True)
        e.daily_pnl = -10_000_000
        self.assertEqual(e._entry_block(t(10, 0), True), "")


class ConsecLossTest(unittest.TestCase):
    def _lose(self, e, it_ts):
        it = e._exit(it_ts, EXIT_TP, "수렴", 0, 0, {})
        e.on_pair_done(it, done(s=e.entry_px["S"] + 100, h=e.entry_px["H"]), it_ts)   # 매도 보유에서 삼성 상승 = 손실

    def test_halts_after_n_losses_and_resets_next_day(self):
        e, j = mk(max_consec_losses=3)
        for n in range(3):
            e.direction, e.open_qty, e.entry_px, e.units = -1, {"S": 10, "H": 1}, {"S": 1_000_000.0, "H": 1_000_000.0}, 1
            self._lose(e, t(10, n))
        self.assertTrue(e.halted.startswith("연속 손실 3"))
        self.assertIn("연속 손실", e._entry_block(t(10, 30), True))
        e._new_day(t(10, 0).date() + timedelta(days=1))
        self.assertEqual(e.halted, "")
        self.assertEqual(e.consec_losses, 0)

    def test_win_resets_count(self):
        e, j = mk(max_consec_losses=3)
        for n, d in enumerate((+100, +100, -100, +100, +100)):
            e.direction, e.open_qty, e.entry_px, e.units = -1, {"S": 10, "H": 1}, {"S": 1_000_000.0, "H": 1_000_000.0}, 1
            it = e._exit(t(10, n), EXIT_TP, "수렴", 0, 0, {})
            e.on_pair_done(it, done(s=1_000_000.0 + d, h=1_000_000.0), t(10, n))
        self.assertEqual(e.consec_losses, 2)
        self.assertEqual(e.halted, "")


class OpenPnlLimitTest(unittest.TestCase):
    def test_unrealized_loss_counts_for_add(self):
        e, j = mk(open_pnl_in_limit=True, daily_loss_limit=500_000, pair_stop_won=150_000, max_units=2, add_z=2.5)
        e.direction, e.units = -1, 1
        e.daily_pnl = -100_000
        e._last_mtm = -300_000           # 남은 한도 = 50만 − 10만 − 30만 = 10만 < 15만
        self.assertIn("남은 손실한도", e._entry_block(t(10, 0), True, add=True))
        e._last_mtm = 0
        self.assertEqual(e._entry_block(t(10, 0), True, add=True), "")


class ScaleTest(unittest.TestCase):
    def _bar(self, e, k, spread):
        import math
        return e.on_bar_close({"start": t(9, 21) + timedelta(minutes=k), "close_s": 1_000_000.0 * math.exp(spread),
                               "close_h": 1_000_000.0, "fresh_s": True, "fresh_h": True})

    def test_add_unit_then_partial_then_final(self):
        e, j = mk(window=20, max_units=2, add_z=2.5, partial_exit_z=1.0, stop_per_unit=True, stop_z=9,
                  pair_stop_won=150_000)
        enter_short(e, j, v=0.0019, base=BASE + BASE)      # z ≈ 2.2 → 진입
        self.assertEqual(e.units, 1)
        self.assertEqual(e.stop_won(), 150_000)
        it = self._bar(e, 0, 0.003)                         # z ≈ 2.75 → 분할 진입
        self.assertIsNotNone(it)
        self.assertEqual(it.purpose, ENTRY_ADD)
        self.assertTrue(it.is_entry)
        self.assertEqual({l.name: l.qty for l in it.legs}, {"S": 10, "H": 1})
        e.on_pair_done(it, done(s=it.ref["S"], h=it.ref["H"]), it.ts)
        self.assertEqual(e.units, 2)
        self.assertEqual(e.open_qty, {"S": 20, "H": 2})
        self.assertEqual(e.stop_won(), 300_000)
        got = None
        for k, sp in enumerate([0.0028, 0.0024, 0.0020, 0.0016, 0.0012, 0.0010, 0.0008, 0.0006, 0.0004, 0.0002, 0.0]):
            got = self._bar(e, k + 1, sp)
            if got is not None:
                break
        self.assertIsNotNone(got)
        self.assertEqual(got.purpose, EXIT_PARTIAL)
        self.assertEqual({l.name: l.qty for l in got.legs}, {"S": 10, "H": 1})
        self.assertLessEqual(abs(e.last_z), 1.0)
        self.assertGreater(abs(e.last_z), 0.5)
        e.on_pair_done(got, done(s=got.ref["S"], h=got.ref["H"]), got.ts)
        self.assertEqual(e.units, 1)
        self.assertEqual(e.open_qty, {"S": 10, "H": 1})
        self.assertEqual(j.trades[-1]["part"], "partial")
        self.assertEqual(e.direction, -1)
        # 남은 1단위는 수렴(|z| ≤ 0.5)에서 청산, 포지션 전체 손익(pos_net)이 final 줄에
        fin = None
        for k in range(30):
            fin = self._bar(e, 20 + k, -0.0005)
            if fin is not None:
                break
        self.assertEqual(fin.purpose, EXIT_TP)
        e.on_pair_done(fin, done(s=fin.ref["S"], h=fin.ref["H"]), fin.ts)
        self.assertEqual(e.direction, 0)
        self.assertEqual(j.trades[-1]["part"], "final")
        self.assertEqual(j.trades[-1]["units_max"], 2)
        self.assertEqual(j.trades[-1]["pos_net"], j.trades[-1]["net_pl"] + j.trades[-2]["net_pl"])

    def test_failed_add_keeps_base(self):
        e, j = mk(window=20, max_units=2, add_z=2.5, stop_z=9)
        enter_short(e, j, v=0.0019, base=BASE + BASE)
        it = self._bar(e, 0, 0.003)
        self.assertEqual(it.purpose, ENTRY_ADD)
        e.on_pair_done(it, SimpleNamespace(status="UNWOUND", qty={"S": 0, "H": 0}, avg={}, unwind_pnl=-10_000,
                                           message=""), it.ts)
        self.assertEqual(e.units, 1)
        self.assertEqual(e.open_qty, {"S": 10, "H": 1})
        self.assertEqual(e.direction, -1)
        self.assertEqual(e.daily_pnl, -10_000)

    def test_defaults_unchanged(self):
        p = PairParams()
        self.assertEqual((p.max_units, p.add_z, p.partial_exit_z, p.max_consec_losses, p.risk_off),
                         (1, 0.0, 0.0, 0, False))


class TimeStopTest(unittest.TestCase):
    def test_time_exit_at_30(self):
        e, j = mk(max_hold_bars=30, stop_z=9, pair_stop_won=10**9, daily_loss_limit=10**10)
        enter_short(e, j)
        it = None
        for k in range(40):
            it = e.on_bar_close({"start": t(9, 11) + timedelta(minutes=k), "close_s": 1_000_000.0 * 1.02,
                                 "close_h": 1_000_000.0, "fresh_s": True, "fresh_h": True})
            if it:
                break
        self.assertIsNotNone(it)
        self.assertIn(it.purpose, (EXIT_TIME, EXIT_TP))
        if it.purpose == EXIT_TIME:
            self.assertEqual(e.held_bars, 30)


class MonitorTest(unittest.TestCase):
    def test_blind_exit_once(self):
        j = J()
        m = Monitor({"blind_exit_sec": 30}, j, ws_enabled=True)
        e, _ = mk()
        enter_short(e, _)
        m.saw_quote(t(10, 0))
        self.assertIsNone(m.blind_exit(e, t(10, 0) + timedelta(seconds=20)))
        it = m.blind_exit(e, t(10, 0) + timedelta(seconds=31))
        self.assertIsNotNone(it)
        self.assertEqual(it.purpose, EXIT_MONITOR)
        e.pending = None
        self.assertIsNone(m.blind_exit(e, t(10, 1)))   # 한 번만
        m.saw_quote(t(10, 2))
        self.assertFalse(m.blind_fired)

    def test_ws_down_blocks_entry(self):
        j = J()
        m = Monitor({}, j, ws_enabled=True)
        e, _ = mk()
        m.entry_gate(e, live=False)
        self.assertIn("모니터링", e._entry_block(t(10, 0), True))
        m.entry_gate(e, live=True)
        self.assertEqual(e._entry_block(t(10, 0), True), "")

    def test_exposure_breaches_halt(self):
        j = J()
        m = Monitor({"max_exposure_ms": 3000, "exposure_breaches_halt": 2}, j, ws_enabled=True)
        e, _ = mk()
        m.after_pair(e, SimpleNamespace(exposure_ms=200, pair_id=1))
        m.after_pair(e, SimpleNamespace(exposure_ms=4000, pair_id=2))
        self.assertEqual(e.halted, "")
        m.after_pair(e, SimpleNamespace(exposure_ms=5000, pair_id=3))
        self.assertIn("한쪽 체결 지연", e.halted)


class ShadowTest(unittest.TestCase):
    def test_shadow_fills_at_quotes(self):
        with tempfile.TemporaryDirectory() as d:
            sb = ShadowBook("R0", PairParams(beta=1.0, window=10, entry_start="09:00", risk_off=True), Path(d))
            sb.eng.warmup = False
            import math
            q = {"S": {"bid": 999_000.0, "ask": 1_001_000.0, "last": 1_000_000.0},
                 "H": {"bid": 999_000.0, "ask": 1_001_000.0, "last": 1_000_000.0}}
            for i, s in enumerate(BASE + [0.02]):
                ps = 1_000_000.0 * math.exp(s)
                sb.on_bar({"start": t(9, 0) + timedelta(minutes=i), "close_s": ps, "close_h": 1_000_000.0,
                           "fresh_s": True, "fresh_h": True}, q, t(9, 0) + timedelta(minutes=i + 1))
            self.assertEqual(sb.eng.direction, -1)
            self.assertEqual(sb.eng.entry_px["S"], 999_000.0)   # 삼성 매도 → 매수호가
            self.assertEqual(sb.eng.entry_px["H"], 1_001_000.0)  # 하이닉스 매수 → 매도호가
            sb.finish(t(9, 20), q)
            self.assertEqual(sb.eng.direction, 0)
            sb.close()


if __name__ == "__main__":
    unittest.main()


class VolStopTest(unittest.TestCase):
    def test_sigma_stop_scales_and_clips(self):
        e, j = mk(stop_sigma_k=3.0, stop_floor_won=50_000, stop_cap_won=150_000, qty_s=10, mult_s=10)
        e.direction, e.units, e.entry_px = -1, 1, {"S": 200_000.0, "H": 1_000_000.0}
        e.entry_sd = 0.002                      # 20bp → 3 × 0.002 × 2,000만 = 12만
        self.assertAlmostEqual(e.stop_won(), 120_000)
        e.entry_sd = 0.0005                     # 3만 → 바닥 5만
        self.assertAlmostEqual(e.stop_won(), 50_000)
        e.entry_sd = 0.01                       # 60만 → 천장 15만
        self.assertAlmostEqual(e.stop_won(), 150_000)

    def test_off_by_default(self):
        e, j = mk(pair_stop_won=150_000)
        e.direction, e.units, e.entry_px, e.entry_sd = -1, 1, {"S": 200_000.0, "H": 1.0}, 0.01
        self.assertEqual(e.stop_won(), 150_000)


class BreakCorrTest(unittest.TestCase):
    def test_recent_corr(self):
        import math
        e, j = mk(break_corr=0.0, break_bars=5)
        for k in range(7):                      # 같은 방향으로 움직이면 상관 +1
            e.hist.append((math.log(100 + k + (k % 2)), math.log(200 + 2 * (k + (k % 2))), None))
        self.assertAlmostEqual(e.recent_corr(5), 1.0, places=6)
        e.hist.clear()
        for k in range(7):                      # 반대로 움직이면 −1
            e.hist.append((math.log(100 + k + (k % 2)), math.log(300 - 2 * (k + (k % 2))), None))
        self.assertLess(e.recent_corr(5), -0.99)
        self.assertIsNone(mk()[0].recent_corr(30))   # 봉이 모자라면 None

    def test_off_by_default(self):
        from pair_engine import PairParams
        self.assertLess(PairParams().break_corr, -1)
