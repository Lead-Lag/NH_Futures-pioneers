"""Read-only price collection with explicit source and historical-contract limits.

PowerShell examples:
  python .\data.py reuse-week02 --start 2026-10-20 --end 2026-10-30
  python .\data.py fetch-nh --start 2026-10-20 --end 2026-10-30
  python .\data.py fetch-dukascopy --start 2026-10-20 --end 2026-10-30 --calibration-days 20

Dukascopy BID candles are CASH FX research proxies, not CME historical futures.
No function in this module sends an order or manufactures missing observations.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import lzma
import math
import struct
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from analyze import KST, as_datetime
from config import load_config, resolve_config
from error import classify_error
from runtime import create_run, sha256_file

HERE = Path(__file__).resolve().parent
FIELDS = ["start", "open", "high", "low", "close", "volume", "symbol", "source"]
DUKASCOPY_URL = "https://datafeed.dukascopy.com/datafeed/{symbol}/{year:04d}/{month:02d}/{day:02d}/BID_candles_min_1.bi5"
DUKASCOPY_SYMBOLS = {"eur": ("EURUSD", 100000.0, False), "jpy": ("USDJPY", 1000.0, True)}
UTC = timezone.utc


def write_bars(path: Path, bars: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for bar in bars:
            row = dict(bar)
            row["start"] = as_datetime(row["start"]).isoformat()
            writer.writerow(row)


def read_bars(path: Path, source_name: str = "recorded_week02") -> list[dict]:
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            item = {"start": as_datetime(row["start"]), "volume": row.get("volume", ""),
                    "symbol": row.get("symbol", ""), "source": row.get("source") or source_name}
            for key in ("open", "high", "low", "close"):
                item[key] = float(row[key])
            rows.append(item)
    return rows


def date_range(first: date, last: date):
    current = first
    while current <= last:
        yield current
        current += timedelta(days=1)


def interval_filter(bars, first: date, last: date):
    return [b for b in bars if first <= as_datetime(b["start"]).date() <= last]


def reuse_week02(first: date, last: date, *, domestic_csv: Path | None = None):
    week02 = HERE.parents[1] / "week02"
    if domestic_csv is not None:
        paths = [domestic_csv]
    else:
        paths = [week02 / "data" / "bars_20261020_20261026.csv"]
        paths.extend(week02 / "results" / name / "bars.csv" for name in (
            "run_paper_20261027_090521", "run_paper_20261027_093818",
            "run_paper_20261028_090325", "run_paper_20261029_090749",
            "run_paper_20261030_090820"))
    merged = {}
    duplicates = conflicts = 0
    sources = []
    for path in paths:
        if not path.exists():
            sources.append({"path": str(path), "status": "missing"})
            continue
        rows = read_bars(path)
        sources.append({"path": str(path), "sha256": sha256_file(path), "status": "loaded", "rows": len(rows)})
        for row in rows:
            start = row["start"]
            if start in merged:
                duplicates += 1
                if any(float(merged[start][key]) != row[key] for key in ("open", "high", "low", "close")):
                    conflicts += 1
                    # Preserve the first known observation; report disagreement.
                    continue
            else:
                row["symbol"] = row["symbol"] or "A756B000"
                merged[start] = row
    bars = interval_filter([merged[t] for t in sorted(merged)], first, last)
    return bars, {"kind": "recorded_domestic_futures", "sources": sources,
                  "duplicate_rows": duplicates, "conflicting_ohlc_rows": conflicts,
                  "duplicate_policy": "keep_first_and_report_conflicts",
                  "note": "Original OHLC only. Replay anomalies in past orders are not replayed as new orders."}


def decode_dukascopy(payload: bytes, day: date, symbol: str, scale: float, inverse: bool):
    unpacked = lzma.decompress(payload)
    size = struct.calcsize(">5If")
    if len(unpacked) % size:
        raise ValueError("Dukascopy candle payload length is not a multiple of 24 bytes")
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    rows = []
    for offset in range(0, len(unpacked), size):
        second, op, cl, lo, hi, volume = struct.unpack_from(">5If", unpacked, offset)
        if second >= 86400 or second % 60:
            raise ValueError("Unexpected UTC timestamp in Dukascopy minute candles")
        values = [op / scale, hi / scale, lo / scale, cl / scale]
        if any(not math.isfinite(x) or x <= 0 for x in values):
            raise ValueError("Invalid price in Dukascopy candle")
        o, h, l, c = values
        if h < max(o, c) or l > min(o, c) or l > h:
            raise ValueError("Inconsistent OHLC in Dukascopy candle")
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("Invalid source volume in Dukascopy candle")
        if inverse:
            o, h, l, c = 1 / o, 1 / l, 1 / h, 1 / c
        rows.append({"start": (midnight + timedelta(seconds=second)).astimezone(KST),
                     "open": o, "high": h, "low": l, "close": c, "volume": float(volume),
                     "symbol": "JPYUSD_SPOT_BID" if inverse else "EURUSD_SPOT_BID",
                     "source": "dukascopy_cash_fx_bid_proxy"})
    return rows


def fetch_dukascopy(context, first, last, args):
    output = {"eur": [], "jpy": []}
    manifest = {"kind": "cash_fx_bid_proxy_not_cme_futures", "instruments": {},
                "volume_basis": "provider quote/activity field; not CME traded contract volume",
                "proxy_limitations": ["Cash FX basis differs from futures.",
                    "BID-only OHLC does not establish executable domestic or overseas round-trip costs.",
                    "Set fx_filter.require_positive_volume=false only for this explicitly labelled proxy analysis.",
                    "No missing candle, price, or volume is invented."], "requests": []}
    raw = context.out / "raw"
    raw.mkdir(exist_ok=True)
    session = requests.Session()
    failures = attempts = 0
    stopped = False
    # URLs are UTC days. Include the preceding UTC date for the KST interval.
    utc_first = first - timedelta(days=1)
    for name, (symbol, scale, inverse) in DUKASCOPY_SYMBOLS.items():
        rows = []
        missing = 0
        info = {"source_symbol": symbol, "inverted": inverse, "rows": 0, "status": "pending"}
        manifest["instruments"][name] = info
        for day in date_range(utc_first, last):
            if stopped:
                break
            if day.weekday() == 5:
                # Sunday evening UTC can be Monday morning KST and is retained.
                # Saturday is not filled with invented observations.
                continue
            url = DUKASCOPY_URL.format(symbol=symbol, year=day.year, month=day.month - 1, day=day.day)
            cache = raw / f"{symbol}_{day:%Y%m%d}_BID_candles_min_1.bi5"
            record = {"instrument": name, "utc_date": day.isoformat(), "url": url}
            try:
                if cache.exists():
                    payload = cache.read_bytes()
                    record["status"] = "cached"
                else:
                    if attempts >= args.max_requests:
                        stopped = True
                        info["stop_reason"] = "request_budget_exhausted"
                        break
                    attempts += 1
                    response = session.get(url, timeout=(args.timeout, args.timeout))
                    record["http_status"] = response.status_code
                    if response.status_code == 404:
                        record["status"] = "missing_source_day"
                        missing += 1
                        manifest["requests"].append(record)
                        if missing >= args.max_missing_days:
                            stopped = True
                            info["stop_reason"] = "repeated_missing_source_days"
                        continue
                    response.raise_for_status()
                    payload = response.content
                    if not payload:
                        record["status"] = "empty_source_day"
                        manifest["requests"].append(record)
                        continue
                    cache.write_bytes(payload)
                    record["status"] = "downloaded"
                decoded = decode_dukascopy(payload, day, symbol, scale, inverse)
                rows.extend(decoded)
                record["rows"] = len(decoded)
                failures = 0
                missing = 0
            except Exception as exc:
                error = classify_error(exc, operation="fetch_dukascopy")
                record.update(status="failed", error=str(error))
                context.journal.event("DATA_DOWNLOAD_ERROR", str(error), source="dukascopy", instrument=name, utc_date=day.isoformat())
                failures += 1
                if failures >= args.max_failures:
                    stopped = True
                    info["stop_reason"] = "network_or_decode_failure_circuit_breaker"
            manifest["requests"].append(record)
        unique = {row["start"]: row for row in interval_filter(rows, first, last)}
        output[name] = [unique[t] for t in sorted(unique)]
        info.update(rows=len(output[name]), status="collected" if output[name] else "unavailable")
        if stopped and "stop_reason" not in info:
            info["stop_reason"] = "shared_source_circuit_open"
    session.close()
    manifest.update(attempts=attempts, circuit_open=stopped)
    return output, manifest


def fetch_nh(context, first, last, calibration_first, config, args):
    from nh_client import NHClient
    api = dict(config.get("api", {}))
    environment = str(api.get("environment", "demo"))
    environment = "demo" if environment in ("paper", "mock") else environment
    if environment not in ("demo", "real"):
        raise ValueError("API environment must be demo or real")
    symbols = {"usdkrw": args.domestic_symbol or config["data"].get("domestic_symbol", "A756C000"),
               "eur": args.eur_symbol or config["data"].get("eur_symbol", "6EZ26"),
               "jpy": args.jpy_symbol or config["data"].get("jpy_symbol", "6JZ26")}
    output = {key: [] for key in symbols}
    manifest = {"kind": "nh_futures", "environment": environment, "instruments": {},
                "cidx_yn": args.cidx_yn, "fut_cidx_exp_mod": args.fut_cidx_exp_mod,
                "continuation_warning": "Continuation/adjustment flags do not prove retrieval of an exact expired contract."}
    circuit = False
    clients = {}
    try:
        for name, symbol in symbols.items():
            info = {"symbol": symbol, "status": "pending", "rows": 0}
            manifest["instruments"][name] = info
            if circuit:
                info.update(status="skipped", reason="NH network failure circuit open")
                continue
            market = "domestic" if name == "usdkrw" else "overseas"
            try:
                if market not in clients:
                    clients[market] = NHClient(environment=environment, market=market, journal=context.journal, settings=api)
                kwargs = {"exchange": config["data"].get("overseas_exchange", "FCME")}
                if args.cidx_yn is not None:
                    kwargs["cidx_yn"] = args.cidx_yn
                if args.fut_cidx_exp_mod is not None:
                    kwargs["fut_cidx_exp_mod"] = args.fut_cidx_exp_mod
                from_day = first if market == "domestic" else calibration_first
                rows = clients[market].minute_bars(symbol, from_day.strftime("%Y%m%d"), last.strftime("%Y%m%d"), **kwargs)
                for row in rows:
                    item = dict(row)
                    item["start"] = as_datetime(item["start"])
                    item.setdefault("symbol", symbol)
                    item["source"] = f"nh_{market}_chart_minute"
                    output[name].append(item)
                output[name].sort(key=lambda x: x["start"])
                info.update(status="collected" if rows else "empty_response", rows=len(rows))
                if not rows:
                    info["note"] = "No exact historical contract data confirmed; do not interpret as zero price/return."
            except Exception as exc:
                error = classify_error(exc, operation="fetch_nh")
                info.update(status="failed", error=str(error))
                context.journal.event("DATA_DOWNLOAD_ERROR", str(error), source="NH", instrument=name)
                text = str(error).lower()
                if getattr(exc, "code", "") in ("TRANSPORT", "RATE_LIMIT") or isinstance(exc, (requests.Timeout, requests.ConnectionError)) or any(x in text for x in ("timeout", "timed out", "connection", "network")):
                    circuit = True
        manifest["network_circuit_open"] = circuit
    finally:
        for client in clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                close()
    return output, manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("reuse-week02", "fetch-nh", "fetch-dukascopy"))
    parser.add_argument("--config", default=str(HERE / "strategy_config.yaml"))
    parser.add_argument("--variant", default="base")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--calibration-days", type=int, default=20)
    parser.add_argument("--out")
    parser.add_argument("--domestic-csv", type=Path)
    parser.add_argument("--domestic-symbol")
    parser.add_argument("--eur-symbol")
    parser.add_argument("--jpy-symbol")
    parser.add_argument("--cidx-yn", choices=("0", "1"))
    parser.add_argument("--fut-cidx-exp-mod", choices=("0", "1"))
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--max-failures", type=int, default=1)
    parser.add_argument("--max-requests", type=int, default=256)
    parser.add_argument("--max-missing-days", type=int, default=6)
    args = parser.parse_args(argv)
    source_path = Path(args.config).resolve()
    source = load_config(source_path)
    config = resolve_config(source, args.variant)
    first_text = args.start or config.get("data", {}).get("start")
    last_text = args.end or config.get("data", {}).get("end")
    if args.command == "reuse-week02":
        first_text, last_text = first_text or "2026-10-20", last_text or "2026-10-30"
    if not first_text or not last_text:
        parser.error("--start and --end are required for external data collection")
    first, last = date.fromisoformat(str(first_text)[:10]), date.fromisoformat(str(last_text)[:10])
    if first > last or args.calibration_days < 0 or args.max_failures < 1 or args.max_requests < 1:
        parser.error("Invalid date interval or request budget")
    calibration_first = first - timedelta(days=args.calibration_days * 3 + 2)
    config["data"].update(start=first.isoformat(), end=last.isoformat())
    context = create_run("data", args.command, config, source, source_path, out_root=args.out)
    context.config["data"]["directory"] = str(context.out)
    context.metadata.update(data_command=args.command, requested_start=first.isoformat(), requested_end=last.isoformat(),
                            calibration_start=calibration_first.isoformat(), actual_hedge_orders=False)
    output = {"usdkrw": [], "eur": [], "jpy": []}
    manifest = {"requested_start": first.isoformat(), "requested_end": last.isoformat(),
                "calibration_start": calibration_first.isoformat(), "sources": {},
                "precise_expired_futures_confirmed": False}
    notes = ["This command collects data only. It sends no mock or real order and runs no test suite."]
    failed = False
    try:
        if args.command == "reuse-week02":
            output["usdkrw"], original = reuse_week02(first, last, domestic_csv=args.domestic_csv)
            manifest["sources"]["usdkrw"] = original
            notes.append("No EUR/JPY data are present in week02; empty observation files indicate unavailable data, not no shock.")
        elif args.command == "fetch-nh":
            output, nh = fetch_nh(context, first, last, calibration_first, config, args)
            manifest["sources"]["nh"] = nh
            notes.extend(["Check exact symbol, continuation flags and returned interval before claiming an expired contract was recovered.",
                          "If exact expired-contract history is unavailable, official exchange historical-data products or licensed vendors are the precise alternative."])
        else:
            output["usdkrw"], original = reuse_week02(first, last, domestic_csv=args.domestic_csv)
            manifest["sources"]["usdkrw"] = original
            fx, proxy = fetch_dukascopy(context, calibration_first, last, args)
            output.update(fx)
            manifest["sources"]["overseas_proxy"] = proxy
            manifest["research_proxy"] = True
            notes.extend(proxy["proxy_limitations"])
            notes.append("These proxy candles must not be described as recovered expired CME futures.")
            context.config["fx_filter"]["require_positive_volume"] = False
        manifest["precise_expired_futures_confirmed"] = False
    except Exception as exc:
        failed = True
        error = classify_error(exc, operation="collect_data")
        manifest["error"] = str(error)
        notes.append(str(error))
        context.journal.event("DATA_COLLECTION_ERROR", str(error))
    files = {}
    for name, bars in output.items():
        path = context.out / f"{name}.csv"
        write_bars(path, bars)
        files[name] = {"filename": path.name, "rows": len(bars), "sha256": sha256_file(path),
                       "first_bar": as_datetime(bars[0]["start"]).isoformat() if bars else None,
                       "last_bar": as_datetime(bars[-1]["start"]).isoformat() if bars else None,
                       "days": len({as_datetime(b["start"]).date() for b in bars})}
    manifest["files"] = files
    manifest["exact_history_alternatives"] = {
        "CME_DataMine": "https://www.cmegroup.com/market-data/real-time-and-historical-data.html",
        "note": "Paid official/licensed contract-by-contract data may be needed. Continuous futures are not exact expired-contract execution history."}
    (context.out / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    backtest_source = copy.deepcopy(source)
    backtest_source["base"].setdefault("data", {}).update(
        directory=str(context.out), start=first.isoformat(), end=last.isoformat(),
        source_kind="cash_fx_bid_proxy" if manifest.get("research_proxy") else "recorded_or_nh_futures")
    if manifest.get("research_proxy"):
        backtest_source["base"].setdefault("fx_filter", {})["require_positive_volume"] = False
    import yaml
    analysis_config = context.out / "backtest_config.yaml"
    analysis_config.write_text(yaml.safe_dump(backtest_source, allow_unicode=True, sort_keys=False), encoding="utf-8")
    complete = all(output.values())
    status = "failed" if failed or not output["usdkrw"] else "completed" if complete else "partial"
    metrics = {"command": args.command, "files": files, "complete_three_instrument_dataset": complete,
               "research_proxy": bool(manifest.get("research_proxy")),
               "parameter_decision": "data_collection_only_no_parameter_selection",
               "source_manifest": "source_manifest.json"}
    if not complete:
        notes.append("Dataset is incomplete. Filter variants cannot establish effectiveness without adequate historical EUR and JPY observations.")
    context.finish(metrics, status=status, notes=notes)
    print(f"{status}: {context.out}")
    print(f"Next: python .\\backtest.py --config '{analysis_config}' --all-variants --data-dir '{context.out}' --start {first} --end {last}")
    return 1 if failed or not output["usdkrw"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
