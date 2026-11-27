"""Offline behavioral tests only; no API client, network, or order is imported."""
from __future__ import annotations

import copy
import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = Path(os.environ.get("HEDGE02_SOURCE", str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(SOURCE))

from analyze import BUY, SELL, Engine, FXShockFilter, Params

KST = timezone(timedelta(hours=9))
DECISION = datetime(2026, 10, 29, 9, 20, tzinfo=KST)


def bar(start, close, symbol, **extra):
    return {"start": start, "open": close, "high": close, "low": close,
            "close": close, "volume": 10, "symbol": symbol, **extra}


def fixture(direction="strong"):
    config = {"enabled": True, "require_coverage": True, "horizon_minutes": 2,
              "lookback_days": 2, "min_history_days": 2, "bucket_minutes": 60,
              "min_samples": 4, "threshold": 2, "max_age_seconds": 90,
              "require_same_end": True, "require_positive_volume": True,
              "min_sigma": 1e-12, "min_move_ticks": 2,
              "eur_tick_size": .00005, "jpy_tick_size": .0000005}
    instruments = {}
    for name, base, symbol in (("eur", 1.10, "6EZ26"), ("jpy", .0065, "6JZ26")):
        rows = []
        for day in (27, 28):
            for minute in range(26):
                close = base * (1 + .00001 * math.sin(minute * 1.7 + day))
                rows.append(bar(DECISION.replace(day=day, minute=minute), close, symbol))
        sign = -1 if direction == "strong" else 1
        for index, minute in enumerate(range(15, 20)):
            rows.append(bar(DECISION.replace(minute=minute), base * (1 + sign * .01 * index), symbol))
        instruments[name] = rows
    return config, instruments["eur"], instruments["jpy"]


class MemoryJournal:
    def __init__(self):
        self.rows = {}

    def row(self, name, **row):
        self.rows.setdefault(name, []).append(dict(row))

    def bar(self, row): self.row("bars", **row)
    def signal(self, row): self.row("signals", **row)
    def trade(self, row): self.row("trades", **row)
    def event(self, kind, message="", **kwargs): self.row("events", kind=kind, message=message, **kwargs)


class FXFilterBehavior(unittest.TestCase):
    def test_dollar_strength_blocks_short_but_never_creates_long(self):
        config, eur, jpy = fixture("strong")
        gate = FXShockFilter(config, eur, jpy)
        allowed, reason, detail = gate.evaluate(DECISION, SELL)
        self.assertFalse(allowed)
        self.assertTrue(reason.startswith("FX_USD_STRONG"))
        self.assertGreater(detail["eur_change"], 0)
        self.assertGreater(detail["jpy_change"], 0)
        self.assertTrue(gate.evaluate(DECISION, BUY)[0])

    def test_dollar_weakness_blocks_long_not_short(self):
        config, eur, jpy = fixture("weak")
        gate = FXShockFilter(config, eur, jpy)
        allowed, reason, detail = gate.evaluate(DECISION, BUY)
        self.assertFalse(allowed)
        self.assertTrue(reason.startswith("FX_USD_WEAK"))
        self.assertLess(detail["eur_change"], 0)
        self.assertLess(detail["jpy_change"], 0)
        self.assertTrue(gate.evaluate(DECISION, SELL)[0])

    def test_future_and_same_day_observations_do_not_change_calibration(self):
        config, eur, jpy = fixture()
        original = FXShockFilter(config, eur, jpy).evaluate(DECISION, SELL)[2]
        altered_eur, altered_jpy = copy.deepcopy(eur), copy.deepcopy(jpy)
        for rows, base, symbol in ((altered_eur, 1.10, "6EZ26"), (altered_jpy, .0065, "6JZ26")):
            for minute in range(12):
                rows.append(bar(DECISION.replace(minute=minute), base * (1 + .02 * minute), symbol))
                rows.append(bar(DECISION.replace(day=30, minute=minute), base * (1 - .02 * minute), symbol))
        changed = FXShockFilter(config, altered_eur, altered_jpy).evaluate(DECISION, SELL)[2]
        for field in ("eur_mean", "eur_sigma", "jpy_mean", "jpy_sigma", "eur_z", "jpy_z"):
            self.assertAlmostEqual(original[field], changed[field], places=10)
        self.assertEqual(changed["eur_history_days"], 2)

    def test_not_yet_received_bar_cannot_be_used(self):
        config, eur, jpy = fixture()
        jpy[-1]["available_at"] = DECISION + timedelta(seconds=2)
        gate = FXShockFilter(config, eur, jpy)
        allowed, reason, detail = gate.evaluate(DECISION, SELL)
        self.assertFalse(allowed)
        self.assertEqual(detail["fx_valid"], 0)
        self.assertIn("not received", reason)
        self.assertEqual(gate.evaluate(DECISION + timedelta(seconds=2), SELL)[2]["fx_state"], "USD_STRONG")

    def test_stale_data_is_unavailable_not_no_shock(self):
        config, eur, jpy = fixture()
        allowed, reason, detail = FXShockFilter(config, eur, jpy).evaluate(DECISION + timedelta(seconds=91), SELL)
        self.assertFalse(allowed)
        self.assertTrue(reason.startswith("FX_DATA_UNAVAILABLE"))
        self.assertEqual(detail["fx_valid"], 0)

    def test_mismatched_endpoints_are_not_compared(self):
        config, eur, jpy = fixture()
        allowed, reason, detail = FXShockFilter(config, eur, jpy[:-1]).evaluate(DECISION, SELL)
        self.assertFalse(allowed)
        self.assertIn("not synchronized", reason)
        self.assertEqual(detail["fx_valid"], 0)

    def test_missing_minute_is_not_forward_filled(self):
        config, eur, jpy = fixture()
        jpy = [row for row in jpy if row["start"] != DECISION.replace(minute=18)]
        allowed, reason, detail = FXShockFilter(config, eur, jpy).evaluate(DECISION, SELL)
        self.assertFalse(allowed)
        self.assertTrue(reason.startswith("FX_DATA_UNAVAILABLE"))
        self.assertEqual(detail["fx_valid"], 0)

    def test_baseline_and_common_coverage_have_different_missing_data_policies(self):
        config, _, _ = fixture()
        config.update(enabled=False, require_coverage=False)
        self.assertTrue(FXShockFilter(config, [], []).evaluate(DECISION, BUY)[0])
        config["require_coverage"] = True
        self.assertFalse(FXShockFilter(config, [], []).evaluate(DECISION, BUY)[0])

    def test_quote_scale_and_tick_scale_leave_decision_unchanged(self):
        config, eur, jpy = fixture()
        first = FXShockFilter(config, eur, jpy).evaluate(DECISION, SELL)
        scaled = copy.deepcopy(jpy)
        for row in scaled:
            for field in ("open", "high", "low", "close"):
                row[field] *= 100
        config["jpy_tick_size"] *= 100
        second = FXShockFilter(config, eur, scaled).evaluate(DECISION, SELL)
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[2]["fx_state"], second[2]["fx_state"])
        self.assertAlmostEqual(first[2]["jpy_z"], second[2]["jpy_z"], places=4)

    def test_rejected_bollinger_signal_does_not_execute_after_filter_clears(self):
        journal = MemoryJournal()
        state = {"allow": False}
        def gate(ts, side):
            return state["allow"], "blocked_for_test" if not state["allow"] else "", {"fx_state": "TEST"}
        params = Params(bb_period=3, bb_k=.5, min_band_width_ticks=0, entry_start="09:00", entry_end="15:15")
        engine = Engine(params, journal, entry_filter=gate)
        engine.warmup = False
        for minute, close in enumerate((100, 100, 100, 98, 99)):
            self.assertIsNone(engine.on_bar_close(bar(DECISION.replace(minute=minute), close, "DOMESTIC")))
        signals = journal.rows.get("signals", [])
        self.assertTrue(signals)
        self.assertEqual(signals[-1]["signal"], "ENTRY_LONG")
        self.assertEqual(signals[-1]["taken"], 0)
        self.assertIsNone(engine.pending)
        state["allow"] = True
        next_result = engine.on_bar_close(bar(DECISION.replace(minute=5), 99.1, "DOMESTIC"))
        self.assertIsNone(next_result)
        self.assertEqual(engine.position, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
