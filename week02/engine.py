"""전략 판정과 포지션·위험 관리. 실시간 실행(run_live.py)과 백테스트(backtest.py)가 **같은 이 코드**를 씁니다.

전략: 볼린저 밴드 이탈 뒤 복귀 (평균회귀), 미국달러 선물 1분봉
    매수 진입  직전 봉 종가 < 직전 하단밴드  그리고  이번 봉 종가 >= 이번 하단밴드
    매도 진입  직전 봉 종가 > 직전 상단밴드  그리고  이번 봉 종가 <= 이번 상단밴드
    익절 청산  매수: 종가 >= 중심선 / 매도: 종가 <= 중심선   (봉 마감 판정)
    손절 청산  진입 체결가에서 불리하게 stop_ticks 틱       (현재가로 수시 판정)
    시간 청산  보유 max_hold_bars 봉 경과                  (봉 마감 판정)
    장 마감    flat_time 이후 보유가 있으면 전량 청산
판정은 봉이 **마감된 뒤 한 번만** 합니다(진행 중인 봉은 쓰지 않음).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from itertools import count
from typing import Any, Protocol

BUY, SELL = "BUY", "SELL"
ENTRY_LONG, ENTRY_SHORT = "ENTRY_LONG", "ENTRY_SHORT"
EXIT_TP, EXIT_STOP, EXIT_TIME, EXIT_EOD, EXIT_MANUAL = "EXIT_TP", "EXIT_STOP", "EXIT_TIME", "EXIT_EOD", "EXIT_MANUAL"


@dataclass
class Params:
    bar_minutes: int = 1
    bb_period: int = 20
    bb_k: float = 2.0
    min_band_width_ticks: int = 8  # 상단−하단이 이보다 좁으면 진입 안 함(먹을 폭이 비용보다 작음)
    stop_ticks: int = 5
    max_hold_bars: int = 30
    cooldown_bars_after_stop: int = 3
    qty: int = 1
    entry_start: str = "09:05"  # 이 시각 이후 마감된 봉부터 진입 판정
    entry_end: str = "15:15"  # 이 시각까지 마감된 봉까지만 신규 진입
    flat_time: str = "15:30"  # 이 시각부터 보유 전량 청산(15:35 단일가 전)
    daily_loss_limit: int = 30_000  # 원. 남은 한도 < 1회 손절액이면 신규 진입 막음
    max_trades_per_day: int = 20
    tick_size: float = 0.1  # 원
    tick_value: int = 1_000  # 원 (0.1원 × 10,000달러)
    fee_per_contract: float = 0.0  # 원, 편도

    @property
    def multiplier(self) -> float:
        return self.tick_value / self.tick_size

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Params":
        known = {k: v for k, v in data.items() if k in Params.__dataclass_fields__}
        return Params(**known)


class Journal(Protocol):
    def bar(self, row: dict[str, Any]) -> None: ...
    def signal(self, row: dict[str, Any]) -> None: ...
    def trade(self, row: dict[str, Any]) -> None: ...
    def event(self, kind: str, message: str, **extra: Any) -> None: ...


@dataclass
class Intent:
    id: int
    ts: datetime
    side: str
    qty: int
    purpose: str
    reason: str
    ref_price: float  # 신호 봉 종가 또는 손절 판정가 — 체결가와 비교해 미끄러짐을 잽니다
    snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        return self.purpose in (ENTRY_LONG, ENTRY_SHORT)


_ids = count(1)


def _hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def bollinger(closes: list[float], k: float) -> tuple[float, float, float]:
    n = len(closes)
    mid = sum(closes) / n
    var = sum((c - mid) ** 2 for c in closes) / n  # 모표준편차(흔한 정의)
    sd = math.sqrt(var)
    return mid, mid + k * sd, mid - k * sd


class Engine:
    def __init__(self, params: Params, journal: Journal) -> None:
        self.p = params
        self.j = journal
        self.closes: deque[float] = deque(maxlen=params.bb_period)
        self.prev: dict[str, float] | None = None  # 직전 봉 {close, mid, upper, lower}
        self.day = None
        self.position = 0  # +qty 매수, −qty 매도
        self.entry_price = 0.0
        self.entry_time: datetime | None = None
        self.stop_price = 0.0
        self.held_bars = 0
        self.pending: Intent | None = None
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.cooldown = 0
        self.halted = ""  # 비어 있지 않으면 신규 진입 중지 사유
        self.fail_count = 0
        self.warmup = True
        self.trade_no = 0

    # ---------------------------------------------------------------- 하루 초기화
    def _new_day(self, d) -> None:
        self.day = d
        self.closes.clear()
        self.prev = None
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.cooldown = 0
        if self.halted.startswith("일일"):
            self.halted = ""
        self.j.event("day", f"거래일 시작 {d}")

    @property
    def stop_amount(self) -> float:
        return self.p.stop_ticks * self.p.tick_value * self.p.qty + 2 * self.p.fee_per_contract * self.p.qty

    # ---------------------------------------------------------------- 봉 마감
    def on_bar_close(self, bar: dict[str, Any]) -> Intent | None:
        start: datetime = bar["start"]
        end = start + timedelta(minutes=self.p.bar_minutes)
        if self.day != start.date():
            self._new_day(start.date())

        close = float(bar["close"])
        self.closes.append(close)
        cur: dict[str, float] | None = None
        if len(self.closes) == self.p.bb_period:
            mid, up, low = bollinger(list(self.closes), self.p.bb_k)
            cur = {"close": close, "mid": mid, "upper": up, "lower": low}

        self.j.bar(
            {
                "start": start.isoformat(),
                "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": close,
                "volume": bar.get("volume", ""),
                "mid": _r(cur, "mid"), "upper": _r(cur, "upper"), "lower": _r(cur, "lower"),
                "position": self.position, "warmup": int(self.warmup),
            }
        )

        prev, self.prev = self.prev, cur
        if self.position and not self.pending:
            self.held_bars += 1
        if self.cooldown > 0:
            self.cooldown -= 1
        if self.warmup or self.pending or cur is None:
            return None

        # ---- 보유 중: 청산 판정
        if self.position:
            snap = {**cur, "held_bars": self.held_bars}
            if end.time() >= _hm(self.p.flat_time):
                return self._exit(end, EXIT_EOD, f"장 마감 정리({self.p.flat_time})", close, snap)
            if self.position > 0 and close >= cur["mid"]:
                return self._exit(end, EXIT_TP, f"종가 {close:.1f} ≥ 중심선 {cur['mid']:.2f}", close, snap)
            if self.position < 0 and close <= cur["mid"]:
                return self._exit(end, EXIT_TP, f"종가 {close:.1f} ≤ 중심선 {cur['mid']:.2f}", close, snap)
            if self.held_bars >= self.p.max_hold_bars:
                return self._exit(end, EXIT_TIME, f"보유 {self.held_bars}봉 경과", close, snap)
            return None

        # ---- 무포지션: 진입 판정
        if prev is None:
            return None
        side = purpose = None
        if prev["close"] < prev["lower"] and close >= cur["lower"]:
            side, purpose, why = BUY, ENTRY_LONG, "하단밴드 이탈 뒤 복귀"
        elif prev["close"] > prev["upper"] and close <= cur["upper"]:
            side, purpose, why = SELL, ENTRY_SHORT, "상단밴드 이탈 뒤 복귀"
        if side is None:
            return None

        width_ticks = round((cur["upper"] - cur["lower"]) / self.p.tick_size, 1)
        snap = {
            **{k: round(v, 4) for k, v in cur.items()},
            "prev_close": prev["close"], "prev_upper": round(prev["upper"], 4), "prev_lower": round(prev["lower"], 4),
            "width_ticks": width_ticks,
        }
        blocked = self._entry_block(end, width_ticks)
        self.j.signal(
            {
                "ts": end.isoformat(), "bar_start": start.isoformat(), "signal": purpose, "reason": why,
                **snap, "taken": int(not blocked), "blocked_by": blocked,
            }
        )
        if blocked:
            return None
        self.pending = Intent(next(_ids), end, side, self.p.qty, purpose, why, close, snap)
        return self.pending

    def _entry_block(self, end: datetime, width_ticks: float) -> str:
        t = end.time()
        if self.halted:
            return f"중지:{self.halted}"
        if t < _hm(self.p.entry_start) or t > _hm(self.p.entry_end):
            return "진입시간 밖"
        if width_ticks < self.p.min_band_width_ticks:
            return f"밴드폭 {width_ticks}틱 < {self.p.min_band_width_ticks}"
        if self.cooldown > 0:
            return f"손절 뒤 대기 {self.cooldown}봉"
        if self.daily_trades >= self.p.max_trades_per_day:
            return "하루 거래 횟수 한도"
        if self.p.daily_loss_limit + self.daily_pnl < self.stop_amount:
            return f"남은 손실한도 {self.p.daily_loss_limit + self.daily_pnl:,.0f} < 1회 손절액 {self.stop_amount:,.0f}"
        return ""

    def _exit(self, ts: datetime, purpose: str, reason: str, ref: float, snap: dict[str, Any]) -> Intent:
        side = SELL if self.position > 0 else BUY
        self.pending = Intent(next(_ids), ts, side, abs(self.position), purpose, reason, ref, snap)
        return self.pending

    # ---------------------------------------------------------------- 현재가(손절)
    def on_price(self, price: float, ts: datetime) -> Intent | None:
        if not self.position or self.pending or self.warmup or price <= 0:
            return None
        if self.position > 0 and price <= self.stop_price:
            return self._exit(ts, EXIT_STOP, f"현재가 {price:.1f} ≤ 손절가 {self.stop_price:.1f}", price, {"price": price})
        if self.position < 0 and price >= self.stop_price:
            return self._exit(ts, EXIT_STOP, f"현재가 {price:.1f} ≥ 손절가 {self.stop_price:.1f}", price, {"price": price})
        return None

    def on_clock(self, ts: datetime) -> Intent | None:
        if self.position and not self.pending and ts.time() >= _hm(self.p.flat_time):
            return self._exit(ts, EXIT_EOD, f"장 마감 정리({self.p.flat_time})", 0.0, {})
        return None

    def force_exit(self, ts: datetime, reason: str) -> Intent | None:
        if not self.position or self.pending:
            return None
        return self._exit(ts, EXIT_MANUAL, reason, 0.0, {})

    # ---------------------------------------------------------------- 체결/실패 반영
    def on_fill(self, intent: Intent, price: float, qty: int, ts: datetime) -> None:
        self.pending = None
        self.fail_count = 0
        if intent.is_entry:
            d = 1 if intent.side == BUY else -1
            self.position = d * qty
            self.entry_price = price
            self.entry_time = ts
            self.held_bars = 0
            self.stop_price = round(price - d * self.p.stop_ticks * self.p.tick_size, 4)
            self.daily_trades += 1
            self.j.event("position", f"{'매수' if d > 0 else '매도'} {qty}계약 보유 @ {price:.1f}, 손절가 {self.stop_price:.1f}")
            return

        d = 1 if self.position > 0 else -1
        gross = (price - self.entry_price) * d * qty * self.p.multiplier
        fee = 2 * self.p.fee_per_contract * qty
        net = gross - fee
        self.daily_pnl += net
        self.trade_no += 1
        self.j.trade(
            {
                "trade_no": self.trade_no,
                "direction": "LONG" if d > 0 else "SHORT",
                "qty": qty,
                "entry_time": self.entry_time.isoformat() if self.entry_time else "",
                "entry_price": self.entry_price,
                "exit_time": ts.isoformat(),
                "exit_price": price,
                "exit_reason": intent.purpose,
                "ticks": round((price - self.entry_price) * d / self.p.tick_size, 1),
                "gross_pl": round(gross),
                "fee": round(fee),
                "net_pl": round(net),
                "held_bars": self.held_bars,
                "held_sec": round((ts - self.entry_time).total_seconds()) if self.entry_time else "",
                "daily_pnl_after": round(self.daily_pnl),
            }
        )
        if intent.purpose == EXIT_STOP:
            self.cooldown = self.p.cooldown_bars_after_stop
        self.position = 0
        self.entry_price = 0.0
        self.entry_time = None
        self.held_bars = 0
        if self.daily_pnl <= -self.p.daily_loss_limit:
            self.halted = "일일 손실 한도 도달"
            self.j.event("risk", f"일일 손실 {self.daily_pnl:,.0f}원 — 오늘 신규 진입 중지")

    def on_order_failed(self, intent: Intent, reason: str) -> None:
        self.pending = None
        self.fail_count += 1
        self.j.event("order_failed", f"{intent.purpose} 실패: {reason}", intent_id=intent.id)
        if self.fail_count >= 3:
            self.halted = "주문 연속 실패 3회"
            self.j.event("risk", "주문 연속 실패 3회 — 신규 진입 중지(청산 판정은 계속)")

    def sync_position(self, broker_position: int, ts: datetime) -> None:
        """잔고 조회 결과와 엔진 상태가 다르면 기록하고 신규 진입을 멈춥니다."""
        if broker_position != self.position:
            self.halted = "잔고 불일치"
            self.j.event("mismatch", f"엔진 {self.position} ≠ 잔고 {broker_position} — 신규 진입 중지", ts=ts.isoformat())


def _r(cur: dict[str, float] | None, key: str) -> Any:
    return "" if cur is None else round(cur[key], 4)


def params_dict(p: Params) -> dict[str, Any]:
    return asdict(p)
