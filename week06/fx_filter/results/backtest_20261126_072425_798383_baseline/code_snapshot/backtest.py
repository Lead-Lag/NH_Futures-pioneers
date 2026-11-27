"""Replay real recorded bars using YAML base/variants and the common strategy.

Example (PowerShell):
  python .\backtest.py --config .\strategy_config.yaml --all-variants --data-dir .\results\data_run

No generated or synthetic prices are used. A signal made at a completed minute
can fill only at the next contiguous bar's open. Open gaps beyond the configured
limit cancel entries; they do not turn hours-old signals into new orders.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from analyze import BUY, Engine, FXShockFilter, Params, as_datetime
from config import list_variants, load_config, resolve_config

HERE = Path(__file__).resolve().parent


def load_bars(path: Path, *, optional: bool = False) -> list[dict]:
    if not path.exists():
        if optional:
            return []
        raise FileNotFoundError(f"Required domestic bars not found: {path}")
    result = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        for line, row in enumerate(csv.DictReader(source), 2):
            try:
                start = as_datetime(row.get("start") or row.get("timestamp"))
                bar = {**row, "start": start}
                for key in ("open", "high", "low", "close"):
                    bar[key] = float(row[key])
                    if not math.isfinite(bar[key]) or bar[key] <= 0:
                        raise ValueError(f"non-positive/nonfinite {key}")
                bar["volume"] = float(row["volume"]) if row.get("volume") not in (None, "") else ""
                if bar.get("available_at"):
                    bar["available_at"] = as_datetime(bar["available_at"])
                if bar["high"] < max(bar["open"], bar["close"]) - 1e-8:
                    raise ValueError("high lower than open/close")
                if bar["low"] > min(bar["open"], bar["close"]) + 1e-8:
                    raise ValueError("low higher than open/close")
                if start in result:
                    previous = result[start]
                    if any(previous[k] != bar[k] for k in ("open", "high", "low", "close")):
                        raise ValueError("conflicting duplicate timestamp")
                result[start] = bar
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"{path.name}:{line}: {exc}") from exc
    return [result[key] for key in sorted(result)]


def _write_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: value.isoformat() if isinstance(value, datetime) else value
                             for key, value in row.items()})


class ReplayJournal:
    """Collect metrics without rereading output CSVs; delegate all durable logs."""
    def __init__(self, journal):
        self.delegate = journal
        self.now = None
        self.rows = {key: [] for key in ("bars", "signals", "trades", "orders", "fills", "fx_diagnostics", "equity")}

    def row(self, name, **row):
        self.rows.setdefault(name, []).append(dict(row))
        self.delegate.row(name, **row)

    def bar(self, row):
        self.row("bars", **row)

    def signal(self, row):
        self.row("signals", **row)

    def trade(self, row):
        self.row("trades", **row)

    def order(self, row):
        self.row("orders", **row)

    def fill(self, row):
        self.row("fills", **row)

    def event(self, kind, message="", **extra):
        extra.setdefault("ts", self.now.isoformat() if self.now else "")
        self.delegate.event(kind, message, **extra)


def calculate_metrics(journal: ReplayJournal, params: Params, *, initial_equity: float, bars: list[dict]) -> dict:
    trades = journal.rows["trades"]
    signals = journal.rows["signals"]
    equity = journal.rows["equity"]
    net = sum(float(t["net_pl"]) for t in trades)
    gross = sum(float(t["gross_pl"]) for t in trades)
    wins = [t for t in trades if float(t["net_pl"]) > 0]
    losses = [t for t in trades if float(t["net_pl"]) < 0]
    peak = initial_equity
    maximum_drawdown = 0.0
    maximum_drawdown_pct = 0.0
    for row in equity:
        value = float(row["equity"])
        peak = max(peak, value)
        maximum_drawdown = max(maximum_drawdown, peak - value)
        if peak:
            maximum_drawdown_pct = max(maximum_drawdown_pct, (peak - value) / peak)
    daily = {}
    for trade in trades:
        date = str(trade["exit_time"])[:10]
        daily[date] = daily.get(date, 0.0) + float(trade["net_pl"])
    blocks = Counter(str(s.get("blocked_by", "")) for s in signals if not int(s.get("taken", 0)))
    fx_blocked = sum(int(s.get("filtered_by_fx", 0)) for s in signals)
    missing = sum(int(s.get("filtered_by_fx", 0)) for s in signals
                  if str(s.get("fx_reason", "")).startswith("FX_DATA_UNAVAILABLE"))
    valid = sum(int(s.get("fx_valid", 0)) for s in signals)
    loss_sum = -sum(float(t["net_pl"]) for t in losses)
    dates = {bar["start"].date().isoformat() for bar in bars}
    valid_days = {str(s["ts"])[:10] for s in signals if int(s.get("fx_valid", 0))}
    return {
        "n_bars": len(bars), "n_days": len(dates), "n_signals": len(signals), "n_trades": len(trades),
        "n_wins": len(wins), "n_losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else None,
        "gross_pnl_krw": gross, "fees_krw": gross - net, "net_pnl_krw": net,
        "initial_equity_krw": initial_equity, "return_on_initial_equity": net / initial_equity if initial_equity else None,
        "max_drawdown_krw": maximum_drawdown, "max_drawdown_fraction": maximum_drawdown_pct,
        "equity_kind": "mark_to_close_after_costs", "daily_pnl_krw": daily,
        "worst_day_krw": min([0.0, *daily.values()]),
        "worst_trade_krw": min([0.0, *(float(t["net_pl"]) for t in trades)]),
        "average_trade_krw": net / len(trades) if trades else None,
        "profit_factor": sum(float(t["net_pl"]) for t in wins) / loss_sum if loss_sum else None,
        "total_held_seconds": sum(float(t.get("held_sec") or 0) for t in trades),
        "fx_valid_signals": valid, "fx_valid_days": len(valid_days),
        "fx_coverage_fraction": valid / len(signals) if signals else None,
        "fx_blocked_signals": fx_blocked, "fx_data_blocked_signals": missing,
        "fx_direction_blocked_signals": fx_blocked - missing,
        "blocked_reasons": dict(blocks), "position_at_end": 0,
        "fee_assumption": "unverified_zero" if params.fee_per_contract == 0 else "configured_per_side_krw",
        "interpretation": "exploratory_not_out_of_sample_parameter_selection",
    }


def run_backtest(bars: list[dict], config: dict, context, *, eur_bars=None, jpy_bars=None) -> dict:
    params = Params.from_config(config["strategy"])
    if params.qty <= 0 or params.tick_size <= 0 or params.tick_value <= 0:
        raise ValueError("qty/tick_size/tick_value must be positive")
    j = ReplayJournal(context.journal)
    gate = FXShockFilter(config.get("fx_filter", {}), eur_bars or [], jpy_bars or [])
    eng = Engine(params, j, entry_filter=gate.evaluate)
    eng.warmup = False
    bt = config.get("backtest", {})
    slip_ticks = float(bt.get("slippage_ticks", 1))
    slip = slip_ticks * params.tick_size
    if slip_ticks < 0:
        raise ValueError("slippage_ticks cannot be negative")
    initial_equity = float(bt.get("initial_equity_krw", 10000000))
    if initial_equity <= 0:
        raise ValueError("initial_equity_krw must be positive")
    maximum_gap = float(bt.get("max_next_bar_gap_seconds", 90))
    pending = None
    total_realized = 0.0
    minimum_intrabar_equity = initial_equity
    maximum_unobserved_gap = 0.0
    j.event("backtest_assumption", "Next-bar open plus adverse slippage; stop first within the next bar.",
            slippage_ticks=slip_ticks, equity_kind="mark_to_close_after_costs")

    def fill(intent, price, ts):
        nonlocal total_realized
        direction = 1 if intent.side == BUY else -1
        actual = round(float(price) + direction * slip, 6)
        j.now = ts
        j.order({"intent_id": intent.id, "intent_ts": intent.ts.isoformat(), "sent_ts": ts.isoformat(),
                 "purpose": intent.purpose, "side": intent.side, "qty": intent.qty,
                 "ref_price": intent.ref_price, "status": "BT_FILLED", "filled_qty": intent.qty,
                 "avg_price": actual, "ord_no": f"BT-{intent.id}", "reason": intent.reason,
                 "order_type": "next_bar_market_assumption"})
        j.fill({"intent_id": intent.id, "ord_no": f"BT-{intent.id}", "ts": ts.isoformat(),
                "purpose": intent.purpose, "side": intent.side, "qty": intent.qty, "price": actual,
                "ref_price": intent.ref_price,
                "slippage_ticks": (actual - intent.ref_price) * direction / params.tick_size if intent.ref_price else "",
                "configured_adverse_slippage_ticks": slip_ticks, "source": "backtest_bar_model"})
        eng.on_fill(intent, actual, intent.qty, ts)
        total_realized = sum(float(t["net_pl"]) for t in j.rows["trades"])

    def mark(ts, price, kind="mark_to_close_after_costs"):
        unrealized = ((price - eng.entry_price) * eng.position * params.multiplier
                      - 2 * params.fee_per_contract * abs(eng.position)) if eng.position else 0.0
        j.row("equity", ts=ts.isoformat(), equity=initial_equity + total_realized + unrealized,
              realized_pnl=total_realized, unrealized_pnl=unrealized,
              cum_net_pl=total_realized + unrealized, position=eng.position, equity_kind=kind)

    for i, bar in enumerate(bars):
        start = bar["start"]
        end = start + timedelta(minutes=params.bar_minutes)
        j.now = start
        if i:
            maximum_unobserved_gap = max(maximum_unobserved_gap, (start - bars[i - 1]["start"]).total_seconds())
        if pending is not None:
            lag = (start - pending.ts).total_seconds()
            valid_next = start.date() == pending.ts.date() and 0 <= lag <= maximum_gap
            if pending.is_entry and (not valid_next or start.time() >= datetime.strptime(params.flat_time, "%H:%M").time()):
                eng.on_order_failed(pending, f"STALE_SIGNAL: next available bar lag={lag:.0f}s")
                eng.fail_count = 0
            else:
                fill(pending, bar["open"], start)
            pending = None
        if eng.position:
            worst = bar["low"] if eng.position > 0 else bar["high"]
            minimum_intrabar_equity = min(minimum_intrabar_equity,
                initial_equity + total_realized + (worst - eng.entry_price) * eng.position * params.multiplier)
            stop = eng.on_price(worst, start)
            if stop is not None:
                gap_stop = bar["open"] <= eng.stop_price if eng.position > 0 else bar["open"] >= eng.stop_price
                fill(stop, bar["open"] if gap_stop else eng.stop_price, start + timedelta(seconds=30))
        j.now = end
        pending = eng.on_bar_close(bar)
        last_of_day = i == len(bars) - 1 or bars[i + 1]["start"].date() != start.date()
        if last_of_day:
            if pending is not None and pending.is_entry:
                eng.on_order_failed(pending, "END_OF_DAY: no following bar for an entry")
                eng.fail_count = 0
                pending = None
            if pending is not None:
                fill(pending, bar["close"], end)
                pending = None
            liquidation = eng.force_exit(end, "Observed data/day end liquidation assumption")
            if liquidation is not None:
                fill(liquidation, bar["close"], end)
        mark(end, bar["close"])
    metrics = calculate_metrics(j, params, initial_equity=initial_equity, bars=bars)
    for day, net in sorted(metrics["daily_pnl_krw"].items()):
        j.row("daily", date=day, net_pnl_krw=net,
              n_trades=sum(str(t["exit_time"])[:10] == day for t in j.rows["trades"]))
    metrics.update({"minimum_intrabar_equity_proxy_krw": minimum_intrabar_equity,
                    "maximum_bar_start_gap_seconds": maximum_unobserved_gap,
                    "position_at_end": eng.position,
                    "fill_model": bt.get("execution_model", "next_bar_open_then_intrabar_stop"),
                    "slippage_ticks": slip_ticks})
    return metrics


def _date_bound(text, *, end=False):
    if not text:
        return None
    value = as_datetime(str(text))
    if end and len(str(text)) == 10:
        value += timedelta(days=1)
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(HERE / "strategy_config.yaml"))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--variant", default=None)
    selection.add_argument("--all-variants", action="store_true")
    parser.add_argument("--data-dir")
    parser.add_argument("--out", help="Results root (one timestamped directory per variant)")
    parser.add_argument("--start", help="Inclusive KST date/time; calibration retains earlier FX observations")
    parser.add_argument("--end", help="Inclusive date or exclusive date/time")
    args = parser.parse_args(argv)
    source_path = Path(args.config).resolve()
    source = load_config(source_path)
    variants = list_variants(source) if args.all_variants else [args.variant or "fx_base"]
    variants = [name for name in variants if name != "base"] if args.all_variants else variants
    from runtime import create_run
    completed = []
    failures = 0
    for name in variants:
        config = resolve_config(source, name)
        if args.data_dir:
            config.setdefault("data", {})["directory"] = str(Path(args.data_dir).resolve())
        if args.start:
            config["data"]["start"] = args.start
        if args.end:
            config["data"]["end"] = args.end
        if args.out:
            config.setdefault("reporting", {})["results_dir"] = str(Path(args.out).resolve())
        context = create_run("backtest", name, config, source, source_path,
                             argv=list(sys.argv) if argv is None else ["backtest.py", *argv],
                             out_root=args.out)
        notes = ["Exploratory comparison; these variants do not establish a final live parameter choice.",
                 "No overseas position is traded. Overseas FX observations only restrict domestic entries.",
                 "OHLC data cannot establish within-minute execution order; configured bar assumptions apply."]
        try:
            data = config["data"]
            directory = Path(data["directory"])
            if not directory.is_absolute():
                directory = source_path.parent / directory
            paths = {"usdkrw": directory / data.get("usdkrw_csv", data.get("usdkrw_file", "usdkrw.csv")),
                     "eur": directory / data.get("eur_csv", data.get("eur_file", "eur.csv")),
                     "jpy": directory / data.get("jpy_csv", data.get("jpy_file", "jpy.csv"))}
            from runtime import sha256_file
            context.metadata["data_inputs"] = {key: {"path": str(path.resolve()),
                "sha256": sha256_file(path) if path.exists() else None} for key, path in paths.items()}
            domestic = load_bars(paths["usdkrw"])
            eur = load_bars(paths["eur"], optional=True)
            jpy = load_bars(paths["jpy"], optional=True)
            lower, upper = _date_bound(data.get("start")), _date_bound(data.get("end"), end=True)
            domestic = [b for b in domestic if (lower is None or b["start"] >= lower)
                        and (upper is None or b["start"] < upper)]
            if not domestic:
                raise ValueError("No domestic observations in the selected date interval")
            for filename in ("manifest.json", "data_manifest.json", "source_manifest.json"):
                manifest_path = directory / filename
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    (context.out / "data_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                    notes.append(f"Source provenance copied from {manifest_path.name}; inspect proxy/coverage limitations.")
                    break
            metrics = run_backtest(domestic, config, context, eur_bars=eur, jpy_bars=jpy)
            metrics.update({"variant": name, "domestic_first_bar": domestic[0]["start"].isoformat(),
                            "domestic_last_bar": domestic[-1]["start"].isoformat(),
                            "eur_input_bars": len(eur), "jpy_input_bars": len(jpy),
                            "input_data_directory": str(directory)})
            if not eur or not jpy:
                notes.append("Missing EUR/JPY observations: directional filter effectiveness cannot be established.")
            if metrics["fx_valid_signals"] == 0:
                notes.append("No eligible FX calibration at any BB signal. Zero trades in filtered variants is data unavailability, not evidence the filter improves trading.")
            if metrics["fee_assumption"] == "unverified_zero":
                notes.append("Fee is configured to zero for week02 reproduction; net results are not net of a verified account commission.")
            context.finish(metrics, notes=notes)
            completed.append({"variant": name, "run_directory": str(context.out),
                              "net_pnl_krw": metrics["net_pnl_krw"], "n_trades": metrics["n_trades"],
                              "max_drawdown_krw": metrics["max_drawdown_krw"],
                              "fx_valid_signals": metrics["fx_valid_signals"],
                              "fx_direction_blocked_signals": metrics["fx_direction_blocked_signals"],
                              "fx_data_blocked_signals": metrics["fx_data_blocked_signals"]})
            print(f"{name}: trades={metrics['n_trades']} pnl={metrics['net_pnl_krw']:.0f} KRW -> {context.out / 'summary.md'}")
        except Exception as exc:
            failures += 1
            from error import classify_error
            error = classify_error(exc, operation="backtest")
            context.journal.event("error", str(error), operation="backtest")
            context.finish({"variant": name, "error": str(error)}, status="failed", notes=notes)
            print(f"{name}: {error}", file=sys.stderr)
    if len(completed) > 1:
        comparison_config = resolve_config(source, "base")
        comparison = create_run("comparison", "all_variants", comparison_config,
                                source, source_path, out_root=args.out)
        _write_rows(comparison.out / "variants_results.csv", completed)
        comparison.finish({"variants": completed, "failed_variants": failures,
                           "parameter_decision": "not_selected_from_exploratory_results"},
                          notes=["Read each linked run's YAML and summary before comparing results.",
                                 "A lower loss with fewer trades does not by itself prove selective risk reduction."])
        print(f"Comparison: {comparison.out / 'summary.md'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
