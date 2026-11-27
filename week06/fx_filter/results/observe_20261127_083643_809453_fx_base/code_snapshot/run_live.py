"""Domestic paper trading; WebSocket trades drive bars/stops and O1 confirms fills.

No order is sent without --trade. REST submits orders (NH's order interface),
seeds/repairs history and reconciles final account state. It is not price polling.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analyze import Engine, FXShockFilter, Params, as_datetime
from backtest import load_bars
from config import HERE, load_config, resolve_config, project_path
from error import TradingError, classify_error
from runtime import create_run

KST = timezone(timedelta(hours=9))


class MinuteBuilder:
    """Only actual trades create candles. Never fill an empty minute with a price."""
    def __init__(self):
        self.bars: dict[str, dict[datetime, dict]] = {}
        self.closed: dict[str, datetime] = {}

    def add(self, name: str, trade: dict):
        ts = as_datetime(trade.get("timestamp") or trade.get("ts"))
        price = float(trade.get("price", 0))
        if price <= 0 or ts > datetime.now(KST) + timedelta(seconds=5):
            return
        start = ts.replace(second=0, microsecond=0)
        if name in self.closed and start <= self.closed[name]:
            return
        volume = float(trade.get("volume", 0) or 0)
        rows = self.bars.setdefault(name, {})
        if start not in rows:
            rows[start] = {"start": start, "open": price, "high": price, "low": price,
                           "close": price, "volume": volume, "symbol": trade["symbol"],
                           "first_trade_at": ts, "last_trade_at": ts}
        else:
            bar = rows[start]
            bar["high"], bar["low"] = max(bar["high"], price), min(bar["low"], price)
            bar["volume"] += volume
            if ts >= bar["last_trade_at"]:
                bar["close"], bar["last_trade_at"] = price, ts
            if ts < bar["first_trade_at"]:
                bar["open"], bar["first_trade_at"] = price, ts

    def drain(self, name: str, now: datetime, grace: float) -> list[dict]:
        rows = self.bars.setdefault(name, {})
        eligible = [t for t in sorted(rows) if t + timedelta(minutes=1, seconds=grace) <= now]
        result = []
        for start in eligible:
            bar = rows.pop(start)
            self.closed[name] = start
            result.append({k: v for k, v in bar.items() if k not in ("first_trade_at", "last_trade_at")})
        return result


class LiveSession:
    def __init__(self, config: dict, context, trade: bool, history_dir: Path | None):
        from nh_client import NHClient
        self.config, self.context, self.j = config, context, context.journal
        self.params = Params.from_config(config["strategy"])
        self.trade_enabled = trade
        if self.params.qty != 1:
            raise TradingError("CONFIG", "Live version requires qty=1; do not scale an unverified partial-fill path.")
        api = dict(config.get("api", {}))
        environment = api.get("environment", "paper")
        if environment not in ("paper", "demo"):
            raise TradingError("CONFIG", "run_live.py is restricted to the NH paper server.")
        self.domestic = NHClient(environment=environment, market="domestic", journal=self.j, settings=api)
        self.overseas = NHClient(environment=environment, market="overseas", journal=self.j, settings=api)
        d = config["data"]
        self.symbols = {"usdkrw": d["domestic_symbol"], "eur": d["eur_symbol"], "jpy": d["jpy_symbol"]}
        self.by_symbol = {symbol: name for name, symbol in self.symbols.items()}
        eur, jpy = [], []
        if history_dir:
            manifest_path = history_dir / "source_manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                text = json.dumps(manifest).lower()
                if trade and ("spot_proxy" in text or "dukascopy" in text) and not config.get("live", {}).get("allow_spot_proxy_history", False):
                    raise TradingError("CONFIG", "Spot-proxy history is exploratory; live CME calibration requires matching futures history.")
                context.metadata["history_manifest"] = manifest
            eur = load_bars(history_dir / d.get("eur_csv", "eur.csv"), optional=True)
            jpy = load_bars(history_dir / d.get("jpy_csv", "jpy.csv"), optional=True)
        cutoff = datetime.now(KST)
        eur = [b for b in eur if b["start"] + timedelta(minutes=1) <= cutoff]
        jpy = [b for b in jpy if b["start"] + timedelta(minutes=1) <= cutoff]
        self.gate = FXShockFilter(config.get("fx_filter", {}), eur, jpy)
        self.context.metadata["history_counts"] = {"eur": len(eur), "jpy": len(jpy)}
        if trade and config.get("fx_filter", {}).get("enabled", True):
            minimum = int(config["fx_filter"].get("min_history_days", 20))
            for name, bars in (("eur", eur), ("jpy", jpy)):
                dates = {b["start"].date() for b in bars if b["start"].date() < cutoff.date()}
                if len(dates) < minimum:
                    raise TradingError("DATA", f"{name}: {len(dates)} prior history days; {minimum} required before --trade.")
                maximum_age = int(config.get("live", {}).get("max_history_age_calendar_days", 4))
                if (cutoff.date() - max(dates)).days > maximum_age:
                    raise TradingError("DATA", f"{name}: calibration history ends {max(dates)}; update history before --trade (maximum {maximum_age} calendar days).")
        self.eng = Engine(self.params, self.j, entry_filter=self.entry_filter)
        self.builder = MinuteBuilder()
        self.feeds = []
        self.latest: dict[str, dict] = {}
        self.shutdown = asyncio.Event()
        self.order_task = None
        self.order_lock = asyncio.Lock()
        self.owned_orders: set[str] = set()
        self.stop_entries = False
        self.repair_needed = False
        self.first_complete = cutoff.replace(second=0, microsecond=0) + timedelta(minutes=1)
        self.feed_ready = False
        self.trade_rows: list[dict] = []
        original_trade = self.j.trade
        def record_trade(row):
            self.trade_rows.append(dict(row))
            original_trade(row)
        self.j.trade = record_trade

    def entry_filter(self, signal_end, side):
        if self.stop_entries or self.repair_needed or not self.feeds or not all(feed.healthy for feed in self.feeds):
            return False, "LIVE_FEED_NOT_READY", {"fx_valid": 0, "fx_state": "UNAVAILABLE"}
        # The exchange bar end and actual decision/receipt time are distinct.
        allowed, reason, details = self.gate.evaluate(datetime.now(KST), side)
        details["signal_bar_end"] = signal_end.isoformat()
        return allowed, reason, details

    async def on_trade(self, event):
        name = self.by_symbol.get(event.get("symbol"))
        if not name:
            return
        if event.get("timestamp_verified") is False:
            self.j.event("unverified_exchange_clock", symbol=event.get("symbol"), name=name)
            return
        now = datetime.now(KST)
        ts = as_datetime(event["timestamp"])
        maximum_age = float(self.config.get("live", {}).get("maximum_tick_age_seconds", 5))
        previous = self.latest.get(name)
        if ts > now + timedelta(seconds=5) or (now - ts).total_seconds() > maximum_age or (previous and ts < as_datetime(previous["timestamp"])):
            self.j.event("stale_or_out_of_order_tick", name=name, exchange_ts=ts.isoformat(), received_at=now.isoformat())
            return
        self.latest[name] = event
        self.j.row("ticks", {"name": name, **event})
        self.builder.add(name, event)
        if name == "usdkrw" and self.eng.position and not self.eng.pending:
            if self.eng.entry_time and ts < self.eng.entry_time:
                return
            intent = self.eng.on_price(float(event["price"]), ts)
            if intent:
                self.queue_intent(intent)

    async def on_quote(self, event):
        self.j.row("quotes", event)

    async def on_order(self, event):
        self.j.row("ws_orders", event)

    async def on_status(self, event):
        self.j.event("ws_status", **event)
        state = str(event.get("state", event.get("status", ""))).lower()
        if any(word in state for word in ("disconnect", "gap", "error", "reconnect")):
            self.repair_needed = True
            self.feed_ready = False
        if state == "ready" and self.feeds and all(feed.healthy for feed in self.feeds):
            self.feed_ready = True
            self.discard_partial_minutes()

    def discard_partial_minutes(self):
        self.first_complete = datetime.now(KST).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for rows in self.builder.bars.values():
            for start in list(rows):
                if start < self.first_complete:
                    del rows[start]

    def queue_intent(self, intent):
        previous = self.order_task
        async def after_previous():
            if previous and not previous.done():
                await asyncio.shield(previous)
            await self.execute(intent)
        # A stop arriving during the entry's final position check must wait,
        # rather than being discarded while Engine.pending remains set.
        self.order_task = asyncio.create_task(after_previous())

    @staticmethod
    def order_number(value):
        if isinstance(value, dict):
            for key in ("order_no", "ord_no", "ord_no1"):
                if value.get(key): return str(value[key]).strip()
            for child in value.values():
                found = LiveSession.order_number(child)
                if found: return found
        return ""

    async def execute(self, intent):
        async with self.order_lock:
            if not self.trade_enabled:
                self.j.order({"intent_id": intent.id, "purpose": intent.purpose, "side": intent.side,
                              "qty": intent.qty, "ref_price": intent.ref_price, "status": "OBSERVE_NO_ORDER",
                              "sent_ts": datetime.now(KST).isoformat()})
                self.eng.on_order_failed(intent, "Observe mode: --trade not supplied")
                self.eng.fail_count = 0
                self.eng.halted = ""
                return
            sent = datetime.now(KST)
            try:
                if intent.is_entry and (self.stop_entries or self.repair_needed):
                    self.eng.on_order_failed(intent, "Entries paused")
                    self.eng.fail_count = 0
                    return
                result = await asyncio.to_thread(self.domestic.place_order, self.symbols["usdkrw"], intent.side,
                                                 intent.qty, order_type="market", price=0, client_tag=str(intent.id))
                order_no = self.order_number(result)
                if not order_no:
                    raise TradingError("UNKNOWN_ORDER", "Order response contains no order number; never resend.")
                self.owned_orders.add(order_no)
                self.j.order({"intent_id": intent.id, "ord_no": order_no, "purpose": intent.purpose,
                              "side": intent.side, "qty": intent.qty, "ref_price": intent.ref_price,
                              "sent_ts": sent.isoformat(), "status": "ACCEPTED_NOT_YET_FILLED"})
                deadline = time.monotonic() + float(self.config["execution"].get("fill_timeout_seconds", 10))
                terminal = None
                while time.monotonic() < deadline:
                    for feed in self.feeds:
                        update = feed.orders.get(order_no) or feed.orders.get(order_no.zfill(10)) or feed.orders.get(order_no.lstrip("0"))
                        if update:
                            qty = int(update.get("filled_qty", 0) or 0)
                            status = str(update.get("status", ""))
                            if qty >= intent.qty or status in ("6", "8", "9", "FILLED", "REJECTED", "CANCELLED"):
                                terminal = update
                                break
                    if terminal:
                        break
                    await asyncio.sleep(0.1)
                # O1 accelerates confirmation, but final REST status/account check
                # also covers delayed or missing notifications and cancel races.
                final = await asyncio.to_thread(self.domestic.reconcile_order, order_no, self.symbols["usdkrw"])
                if not final.get("found") or final.get("remaining_qty") is None:
                    raise TradingError("RECONCILE", "Final order state is unavailable; never assume missing remaining quantity is zero.")
                filled = int(final.get("filled_qty", 0) or 0)
                remaining = int(final.get("remaining_qty", max(0, intent.qty - filled)) or 0)
                if remaining:
                    await asyncio.to_thread(self.domestic.cancel_order, order_no, self.symbols["usdkrw"], intent.side, remaining)
                    final = await asyncio.to_thread(self.domestic.reconcile_order, order_no, self.symbols["usdkrw"])
                    if not final.get("found") or final.get("remaining_qty") is None:
                        raise TradingError("RECONCILE", "Post-cancel order state is unavailable.")
                    filled = int(final.get("filled_qty", 0) or 0)
                    remaining = int(final.get("remaining_qty", 0) or 0)
                if remaining:
                    raise TradingError("RECONCILE", "Cancel acknowledgement did not establish zero remaining quantity.")
                if filled:
                    avg = float(final.get("avg_price", 0) or 0)
                    if avg <= 0 or filled != intent.qty:
                        raise TradingError("RECONCILE", "Final fill quantity/price does not match this one-contract intent.")
                    self.j.fill({"intent_id": intent.id, "ord_no": order_no, "ts": datetime.now(KST).isoformat(),
                                 "purpose": intent.purpose, "side": intent.side, "qty": filled, "price": avg,
                                 "ref_price": intent.ref_price, "source": "O1_plus_REST_final_reconciliation" if terminal else "REST_final_reconciliation_after_O1_timeout"})
                    self.eng.on_fill(intent, avg, filled, datetime.now(KST))
                else:
                    self.eng.on_order_failed(intent, "Final order cancelled/rejected without fill")
                await self.reconcile_position()
            except Exception as exc:
                error = classify_error(exc, operation="execute_order", order=True)
                self.stop_entries = True
                self.j.event("order_error", str(error), policy=error.to_dict())
                self.context.metadata["execution_error"] = error.to_dict()
                if error.code == "UNKNOWN_ORDER":
                    self.context.metadata["unknown_order_requires_review"] = True
                # Unknown orders are not resent. Account cleanup resolves open
                # orders before attempting liquidation, then verifies final state.
                self.shutdown.set()

    async def position_quantity(self):
        rows = await asyncio.to_thread(self.domestic.positions)
        return sum(int(row.get("qty", 0)) for row in rows if row.get("symbol") == self.symbols["usdkrw"])

    async def reconcile_position(self):
        actual = await self.position_quantity()
        self.j.row("positions", ts=datetime.now(KST).isoformat(), symbol=self.symbols["usdkrw"],
                   expected_qty=self.eng.position, actual_qty=actual)
        if actual != self.eng.position:
            raise TradingError("RECONCILE", f"Expected position {self.eng.position}, broker position {actual}")

    async def seed_and_repair(self, initial=False):
        day = datetime.now(KST).strftime("%Y%m%d")
        for name, client in (("eur", self.overseas), ("jpy", self.overseas), ("usdkrw", self.domestic)):
            kwargs = {} if name == "usdkrw" else {"exchange": self.config["data"].get("overseas_exchange", "FCME")}
            bars = await asyncio.to_thread(client.minute_bars, self.symbols[name], day, day, **kwargs)
            now = datetime.now(KST)
            closed = [b for b in bars if as_datetime(b["start"]) + timedelta(minutes=1) <= now]
            for bar in closed:
                bar["start"] = as_datetime(bar["start"])
            if name == "usdkrw":
                last = self.builder.closed.get(name)
                previous_warmup = self.eng.warmup
                self.eng.warmup = True
                try:
                    for bar in sorted(closed, key=lambda b: as_datetime(b["start"])):
                        if last is None or as_datetime(bar["start"]) > last:
                            self.eng.on_bar_close(bar)
                            self.builder.closed[name] = as_datetime(bar["start"])
                finally:
                    self.eng.warmup = previous_warmup
            else:
                for bar in closed:
                    self.gate.add_bar(name, bar)
            self.j.event("history_seed" if initial else "gap_repair", name=name, n_bars=len(closed),
                         source="REST_only_for_initial_history_or_recovery")
        self.eng.warmup = False
        self.repair_needed = False

    async def cleanup(self):
        if not self.trade_enabled:
            return
        self.stop_entries = True
        try:
            if self.order_task and not self.order_task.done():
                await asyncio.wait_for(asyncio.shield(self.order_task), timeout=35)
            # Flat-start is mandatory, and the traded symbol must be exclusive to
            # this run. Resolve every remaining order before sending a close.
            opened = await asyncio.to_thread(self.domestic.open_orders, self.symbols["usdkrw"])
            for row in opened:
                number = str(row.get("order_no", ""))
                if not number:
                    raise TradingError("RECONCILE", "Cannot identify remaining order during cleanup")
                await asyncio.to_thread(self.domestic.cancel_order, number, self.symbols["usdkrw"],
                                         row.get("side", "BUY"), int(row.get("remaining_qty", 0)))
                final = await asyncio.to_thread(self.domestic.reconcile_order, number, self.symbols["usdkrw"])
                if not final.get("found") or final.get("remaining_qty") is None:
                    raise TradingError("RECONCILE", "Cannot establish final cancel state during cleanup; no additional close order sent")
                if int(final.get("remaining_qty", 0)):
                    raise TradingError("RECONCILE", "Pending order still present after cancel; automatic close halted")
            actual = await self.position_quantity()
            if actual and self.config["execution"].get("flatten_on_exit", True):
                result = await asyncio.to_thread(self.domestic.place_order, self.symbols["usdkrw"],
                                                 "SELL" if actual > 0 else "BUY", abs(actual),
                                                 order_type="market", price=0, client_tag="shutdown_flatten")
                number = self.order_number(result)
                if not number:
                    raise TradingError("UNKNOWN_ORDER", "Cleanup order response missing order number")
                deadline = time.monotonic() + 15
                final = {}
                while time.monotonic() < deadline:
                    final = await asyncio.to_thread(self.domestic.reconcile_order, number, self.symbols["usdkrw"])
                    if int(final.get("filled_qty", 0)) >= abs(actual) and not int(final.get("remaining_qty", 0)):
                        break
                    await asyncio.sleep(0.5)
                self.j.row("cleanup_orders", order_no=number, result=final)
                remaining = await self.position_quantity()
                if remaining:
                    raise TradingError("RECONCILE", f"Shutdown position remains {remaining}; manual account review required")
                if self.eng.position and not self.eng.pending:
                    intent = self.eng.force_exit(datetime.now(KST), "Shutdown liquidation confirmed")
                    avg = float(final.get("avg_price", 0) or 0)
                    if intent and avg > 0 and intent.qty == abs(actual):
                        self.eng.on_fill(intent, avg, abs(actual), datetime.now(KST))
            self.context.metadata["final_broker_position"] = await self.position_quantity()
            if self.context.metadata["final_broker_position"] != self.eng.position or self.eng.pending:
                self.context.metadata["engine_state_unreconciled"] = True
                self.j.event("ENGINE_STATE_REVIEW_REQUIRED", broker_position=self.context.metadata["final_broker_position"],
                             engine_position=self.eng.position, pending_intent=bool(self.eng.pending))
            if self.context.metadata.get("unknown_order_requires_review"):
                self.j.event("MANUAL_RECONCILIATION_REQUIRED", "An unknown order remains unproven even if the latest account snapshot is flat; do not restart before reviewing order history.")
        except Exception as exc:
            error = classify_error(exc, operation="shutdown_reconciliation", order=True)
            self.context.metadata["cleanup_error"] = error.to_dict()
            self.j.event("MANUAL_RECONCILIATION_REQUIRED", str(error))

    async def run(self, minutes=0):
        from websocket import RealtimeFeed
        if self.trade_enabled:
            if await self.position_quantity() or await asyncio.to_thread(self.domestic.open_orders, self.symbols["usdkrw"]):
                raise TradingError("RECONCILE", "This symbol must have no position/open order at startup.")
        await self.seed_and_repair(initial=True)
        callbacks = dict(on_trade=self.on_trade, on_order=self.on_order, on_quote=self.on_quote, on_status=self.on_status,
                         settings=self.config.get("live", {}))
        dom_id = getattr(self.domestic, "credential_id", "domestic")
        ov_id = getattr(self.overseas, "credential_id", "overseas")
        if dom_id == ov_id:
            mixed = {name: {"symbol": symbol, "market": "domestic" if name == "usdkrw" else "overseas"}
                     for name, symbol in self.symbols.items()}
            self.feeds = [RealtimeFeed(self.domestic, mixed, market="mixed", **callbacks)]
        else:
            self.feeds = [RealtimeFeed(self.domestic, {"usdkrw": self.symbols["usdkrw"]}, market="domestic", **callbacks),
                          RealtimeFeed(self.overseas, {"eur": self.symbols["eur"], "jpy": self.symbols["jpy"]}, market="overseas", **callbacks)]
        tasks = [asyncio.create_task(feed.run()) for feed in self.feeds]
        started = time.monotonic()
        grace = float(self.config.get("live", {}).get("close_grace_seconds", 2))
        try:
            while not self.shutdown.is_set():
                now = datetime.now(KST)
                self.j.now = now
                if minutes and time.monotonic() - started >= minutes * 60:
                    break
                if now.strftime("%H:%M") >= self.params.flat_time:
                    break
                if self.repair_needed and all(feed.healthy for feed in self.feeds) and not self.eng.pending:
                    await self.seed_and_repair()
                    self.discard_partial_minutes()
                for name in (("eur", "jpy", "usdkrw") if self.feed_ready else ()):
                    for bar in self.builder.drain(name, now, grace):
                        if bar["start"] < self.first_complete:
                            continue
                        bar["available_at"] = now
                        if name == "usdkrw":
                            if (now - bar["start"] - timedelta(minutes=1)).total_seconds() > float(self.config["live"].get("maximum_bar_age_seconds", 90)):
                                self.eng.warmup = True
                                self.eng.on_bar_close(bar)
                                self.eng.warmup = False
                                self.j.event("stale_bar_skipped", bar_start=bar["start"].isoformat())
                            else:
                                intent = self.eng.on_bar_close(bar)
                                if intent:
                                    self.queue_intent(intent)
                        else:
                            self.gate.add_bar(name, bar)
                if self.eng.position and not self.eng.pending:
                    timed = self.eng.on_clock(now)
                    if timed:
                        self.queue_intent(timed)
                latest = self.latest.get("usdkrw")
                if latest:
                    realized = sum(float(t["net_pl"]) for t in self.trade_rows)
                    unrealized = (float(latest["price"]) - self.eng.entry_price) * self.eng.position * self.params.multiplier if self.eng.position else 0
                    self.j.row("equity", ts=now.isoformat(), realized_pnl=realized, unrealized_pnl=unrealized,
                               equity=self.config.get("backtest", {}).get("initial_equity_krw", 10000000) + realized + unrealized,
                               position=self.eng.position)
                await asyncio.sleep(0.5)
        finally:
            await self.cleanup()
            for feed in self.feeds:
                await feed.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(HERE / "strategy_config.yaml"))
    parser.add_argument("--variant", default="fx_base")
    parser.add_argument("--history-dir", help="Matching EUR/JPY futures calibration CSV directory")
    parser.add_argument("--run-minutes", type=float, default=None)
    parser.add_argument("--trade", action="store_true", help="Enable domestic paper orders; otherwise observe only")
    parser.add_argument("--out")
    args = parser.parse_args()
    source = load_config(args.config)
    config = resolve_config(source, args.variant)
    config["execution"]["allow_orders"] = args.trade
    if args.history_dir:
        config["data"]["directory"] = str(Path(args.history_dir).resolve())
    if args.run_minutes is not None:
        config["live"]["run_minutes"] = args.run_minutes
    ctx = create_run("live" if args.trade else "observe", args.variant, config, source, args.config, out_root=args.out)
    ctx.metadata["actual_orders_enabled"] = args.trade
    session = None
    status = "completed"
    notes = ["Only domestic USD/KRW orders are permitted. Overseas FX is a filter, not an actual hedge position.",
             "Account-order smoke testing and a real WebSocket connection have not been verified by offline fake-response tests; inspect the separate validation run.",
             "Use this symbol exclusively for this session; flat startup and final broker reconciliation are required."]
    try:
        history = project_path(config["data"]["directory"])
        session = LiveSession(config, ctx, args.trade, history if history.exists() else None)
        async def execute():
            loop = asyncio.get_running_loop()
            try:
                loop.add_signal_handler(signal.SIGINT, session.shutdown.set)
            except (NotImplementedError, RuntimeError):
                signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(session.shutdown.set))
            await session.run(float(config["live"].get("run_minutes", 0)))
        asyncio.run(execute())
    except KeyboardInterrupt:
        status = "interrupted"
    except Exception as exc:
        status = "failed"
        error = classify_error(exc, operation="run_live")
        ctx.journal.event("error", str(error), policy=error.to_dict())
        notes.append(str(error))
        print(error, file=sys.stderr)
    finally:
        trades = session.trade_rows if session else []
        metrics = {"n_trades": len(trades), "net_pnl_krw": sum(float(t["net_pl"]) for t in trades),
                   "engine_position_at_end": session.eng.position if session else None,
                   "broker_position_at_end": ctx.metadata.get("final_broker_position"),
                   "orders_enabled": args.trade, "actual_hedge_orders": False,
                   "interpretation": "paper_execution_progress_not_strategy_profitability_proof"}
        if ctx.metadata.get("cleanup_error") or ctx.metadata.get("engine_state_unreconciled") or ctx.metadata.get("unknown_order_requires_review"):
            status = "requires_manual_reconciliation"
        elif ctx.metadata.get("execution_error"):
            status = "failed"
        ctx.finish(metrics, status=status, notes=notes)
        print(ctx.out / "summary.md")
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
