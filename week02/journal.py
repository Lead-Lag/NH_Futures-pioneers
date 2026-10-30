"""실행 기록. 실행 하나 = results/<run_id>/ 폴더 하나.

    run_meta.json   실행 환경·종목·전략 설정·시작/종료 시각
    events.jsonl    한 줄 = 한 사건(상태 변화, 오류, 위험 관리 등)
    bars.csv        마감된 봉과 그때의 밴드 값
    signals.csv     진입 신호(실제 진입했는지, 막혔다면 이유)
    orders.csv      주문 한 건(의도 → 접수 → 결과)
    fills.csv       체결 한 건(체결가, 기준가 대비 미끄러짐)
    trades.csv      진입~청산 한 건(손익)
    api_calls.csv   API 호출 한 건(이름, HTTP 상태, 지연, 오류코드)
CSV 는 엑셀에서 한글이 깨지지 않도록 utf-8-sig 로 씁니다.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))

COLUMNS = {
    "bars": ["start", "open", "high", "low", "close", "volume", "mid", "upper", "lower", "position", "warmup"],
    "signals": [
        "ts", "bar_start", "signal", "reason", "close", "mid", "upper", "lower",
        "prev_close", "prev_upper", "prev_lower", "width_ticks", "reward_ticks", "slope_ticks", "swing", "taken", "blocked_by",
    ],
    "orders": [
        "intent_id", "intent_ts", "purpose", "side", "qty", "order_type", "reason", "ref_price",
        "sent_ts", "ord_no", "status", "filled_qty", "avg_price", "message",
    ],
    "fills": ["intent_id", "ord_no", "ts", "purpose", "side", "qty", "price", "ref_price", "slippage_ticks", "source"],
    "trades": [
        "trade_no", "direction", "qty", "entry_time", "entry_price", "exit_time", "exit_price", "exit_reason",
        "ticks", "gross_pl", "fee", "net_pl", "held_bars", "held_sec", "daily_pnl_after",
    ],
    "api_calls": ["ts", "api", "http_status", "latency_ms", "error_code", "message"],
}


class RunJournal:
    def __init__(self, folder: Path, *, echo: bool = True) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self._files = {}
        self._writers = {}
        for name, cols in COLUMNS.items():
            f = open(folder / f"{name}.csv", "w", newline="", encoding="utf-8-sig")
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            self._files[name], self._writers[name] = f, w
        self._events = open(folder / "events.jsonl", "w", encoding="utf-8")

    def _row(self, name: str, row: dict[str, Any]) -> None:
        self._writers[name].writerow(row)
        self._files[name].flush()

    # engine.Journal 프로토콜
    def bar(self, row: dict[str, Any]) -> None:
        self._row("bars", row)

    def signal(self, row: dict[str, Any]) -> None:
        self._row("signals", row)
        state = "진입" if row.get("taken") else f"보류({row.get('blocked_by')})"
        self.event("signal", f"{row['signal']} {row['reason']} 종가 {row['close']} → {state}", echo_only=False)

    def trade(self, row: dict[str, Any]) -> None:
        self._row("trades", row)
        self.event("trade", f"#{row['trade_no']} {row['direction']} {row['entry_price']}→{row['exit_price']} "
                            f"{row['exit_reason']} {row['net_pl']:+,}원")

    def order(self, row: dict[str, Any]) -> None:
        self._row("orders", row)

    def fill(self, row: dict[str, Any]) -> None:
        self._row("fills", row)

    def api_call(self, row: dict[str, Any]) -> None:
        self._row("api_calls", row)

    def event(self, kind: str, message: str, echo_only: bool = False, **extra: Any) -> None:
        ts = extra.pop("ts", None) or datetime.now(KST).isoformat(timespec="seconds")
        line = {"ts": ts, "kind": kind, "message": message, **extra}
        self._events.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        self._events.flush()
        if self.echo:
            print(f"{ts[11:19]} [{kind}] {message}", flush=True)

    def meta(self, data: dict[str, Any]) -> None:
        (self.folder / "run_meta.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._events.close()


class SimClockJournal(RunJournal):
    """백테스트용: 사건 시각을 봉 시각으로 적습니다(벽시계가 아님)."""

    def __init__(self, folder: Path, *, echo: bool = False) -> None:
        super().__init__(folder, echo=echo)
        self.now: datetime | None = None

    def event(self, kind: str, message: str, echo_only: bool = False, **extra: Any) -> None:
        if "ts" not in extra and self.now is not None:
            extra["ts"] = self.now.isoformat(timespec="seconds")
        super().event(kind, message, **extra)
