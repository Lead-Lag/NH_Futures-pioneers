"""week02 원형을 유지한 YAML 기반 공통 판정과 해외 통화 진입 필터.

전략 판정과 포지션·위험 관리. 실시간 실행(run_live.py)과 백테스트(backtest.py)가 **같은 이 코드**를 씁니다.

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
from datetime import datetime, time, timedelta, timezone
from itertools import count
from typing import Any, Protocol, Callable
from bisect import bisect_right
from statistics import mean, stdev

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
    # ---- 선택 규칙 (기본값 = 꺼짐 → 기존 동작 그대로). 데이터로 비교한 뒤 켤지 정합니다.
    min_reward_ticks: float = 0  # 신호 봉 종가→중심선 거리(틱)가 이보다 작으면 진입 안 함 (0=끔)
    trend_ma_period: int = 0  # 추세 필터용 긴 이동평균 봉 수 (0=끔)
    trend_slope_bars: int = 10  # 긴 이동평균 기울기를 몇 봉 전과 비교할지
    trend_max_slope_ticks: float = 3  # 기울기가 이 틱보다 가파르면 역방향 진입 막음
    stop_mode: str = "fixed"  # fixed: 체결가±stop_ticks / swing: 신호 직전 극값 너머 + stop_buffer_ticks
    stop_buffer_ticks: int = 1
    stop_min_ticks: int = 3  # swing 손절 거리 하한·상한(틱)
    stop_max_ticks: int = 8

    @property
    def multiplier(self) -> float:
        return self.tick_value / self.tick_size

    @staticmethod
    def from_config(data: dict[str, Any]) -> "Params":
        return Params.from_dict(data)

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
    def __init__(self, params: Params, journal: Journal, entry_filter: Callable | None = None) -> None:
        self.p = params
        self.j = journal
        self.entry_filter = entry_filter
        self.closes: deque[float] = deque(maxlen=params.bb_period)
        self.long_closes: deque[float] = deque(maxlen=max(params.trend_ma_period, 1))
        self.long_ma_hist: deque[float] = deque(maxlen=max(params.trend_slope_bars, 1) + 1)
        self.prev_bar: dict[str, Any] | None = None
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
        self.long_closes.clear()
        self.long_ma_hist.clear()
        self.prev_bar = None
        self.prev = None
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.cooldown = 0
        if self.halted.startswith("일일"):
            self.halted = ""
        self.j.event("day", f"거래일 시작 {d}")

    @property
    def stop_amount(self) -> float:
        ticks = self.p.stop_max_ticks if self.p.stop_mode == "swing" else self.p.stop_ticks
        return ticks * self.p.tick_value * self.p.qty + 2 * self.p.fee_per_contract * self.p.qty

    def _slope_ticks(self) -> float | None:
        """긴 이동평균이 trend_slope_bars 봉 동안 움직인 틱 수. 준비 전이면 None."""
        if self.p.trend_ma_period <= 0 or len(self.long_ma_hist) <= self.p.trend_slope_bars:
            return None
        return (self.long_ma_hist[-1] - self.long_ma_hist[0]) / self.p.tick_size

    # ---------------------------------------------------------------- 봉 마감
    def on_bar_close(self, bar: dict[str, Any]) -> Intent | None:
        start: datetime = bar["start"]
        end = start + timedelta(minutes=self.p.bar_minutes)
        if self.day != start.date():
            self._new_day(start.date())

        close = float(bar["close"])
        self.closes.append(close)
        if self.p.trend_ma_period > 0:
            self.long_closes.append(close)
            if len(self.long_closes) == self.p.trend_ma_period:
                self.long_ma_hist.append(sum(self.long_closes) / self.p.trend_ma_period)
        prev_bar, self.prev_bar = self.prev_bar, bar
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
        reward_ticks = round(abs(cur["mid"] - close) / self.p.tick_size, 1)
        slope = self._slope_ticks()
        lows = [float(bar["low"])] + ([float(prev_bar["low"])] if prev_bar else [])
        highs = [float(bar["high"])] + ([float(prev_bar["high"])] if prev_bar else [])
        swing = min(lows) if side == BUY else max(highs)  # 이탈했다 돌아온 두 봉의 극값
        snap = {
            **{k: round(v, 4) for k, v in cur.items()},
            "prev_close": prev["close"], "prev_upper": round(prev["upper"], 4), "prev_lower": round(prev["lower"], 4),
            "width_ticks": width_ticks, "reward_ticks": reward_ticks,
            "slope_ticks": "" if slope is None else round(slope, 1), "swing": swing,
        }
        blocked = self._entry_block(end, width_ticks, side, reward_ticks, slope)
        eligible_before_fx = not blocked
        fx_details: dict[str, Any] = {}
        if self.entry_filter is not None:
            allowed, fx_reason, fx_details = self.entry_filter(end, side)
            fx_details = dict(fx_details)
            fx_details.update({"fx_allowed": int(allowed), "fx_reason": fx_reason,
                               "eligible_before_fx": int(eligible_before_fx),
                               "filtered_by_fx": int(eligible_before_fx and not allowed)})
            if eligible_before_fx and not allowed:
                blocked = fx_reason
            if hasattr(self.j, "row"):
                self.j.row("fx_diagnostics", ts=end.isoformat(), side=side, **fx_details)
        self.j.signal(
            {
                "ts": end.isoformat(), "bar_start": start.isoformat(), "signal": purpose, "reason": why,
                **snap, **fx_details, "taken": int(not blocked), "blocked_by": blocked,
            }
        )
        if blocked:
            return None
        self.pending = Intent(next(_ids), end, side, self.p.qty, purpose, why, close, snap)
        return self.pending

    def _entry_block(self, end: datetime, width_ticks: float, side: str = BUY,
                     reward_ticks: float = 0.0, slope: float | None = None) -> str:
        t = end.time()
        if self.halted:
            return f"중지:{self.halted}"
        if t < _hm(self.p.entry_start) or t > _hm(self.p.entry_end):
            return "진입시간 밖"
        if width_ticks < self.p.min_band_width_ticks:
            return f"밴드폭 {width_ticks}틱 < {self.p.min_band_width_ticks}"
        if self.p.min_reward_ticks and reward_ticks < self.p.min_reward_ticks:
            return f"중심선까지 {reward_ticks}틱 < {self.p.min_reward_ticks}"
        if self.p.trend_ma_period > 0:
            if slope is None:
                return "추세 이동평균 준비 중"
            if side == BUY and slope < -self.p.trend_max_slope_ticks:
                return f"하락 추세(기울기 {slope:.1f}틱) — 매수 막음"
            if side == SELL and slope > self.p.trend_max_slope_ticks:
                return f"상승 추세(기울기 {slope:.1f}틱) — 매도 막음"
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
            self.stop_price = self._stop_for(intent, price, d)
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

    def _stop_for(self, intent: Intent, price: float, d: int) -> float:
        t = self.p.tick_size
        if self.p.stop_mode == "swing" and intent.snapshot.get("swing") not in (None, ""):
            raw = float(intent.snapshot["swing"]) - d * self.p.stop_buffer_ticks * t
            dist = (price - raw) * d / t  # 체결가에서 손절가까지 틱
            dist = min(max(dist, self.p.stop_min_ticks), self.p.stop_max_ticks)
            return round(price - d * round(dist) * t, 4)
        return round(price - d * self.p.stop_ticks * t, 4)

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


KST = timezone(timedelta(hours=9))


def as_datetime(value: Any) -> datetime:
    """Canonical bars use start time, not the end time or exchange trade date."""
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=KST)
    return result.astimezone(KST)


class FXShockFilter:
    """Only restricts entries; it never places an overseas hedge order.

    Each feature uses traded bars at the two exact horizon endpoints; missing
    intermediate minutes do not invalidate the return. Calibration
    uses completed previous dates only, in the same KST clock bucket. A rolling
    contract jump is rejected when the CSV carries a symbol change. Missing
    observations are not forward filled and never mean 'no shock'.
    """

    def __init__(self, config: dict[str, Any], eur_bars=None, jpy_bars=None):
        self.config = dict(config)
        self.enabled = bool(config.get("enabled", True))
        self.require_coverage = bool(config.get("require_coverage", self.enabled))
        self.horizon = int(config.get("horizon_minutes", 5))
        self.lookback = int(config.get("lookback_days", 20))
        self.min_days = int(config.get("min_history_days", self.lookback))
        self.bucket = int(config.get("bucket_minutes", 30))
        self.min_samples = int(config.get("min_samples", 100))
        self.threshold = float(config.get("threshold", 2.0))
        self.max_age = float(config.get("max_age_seconds", 90))
        self.min_sigma = float(config.get("min_sigma", 0.000001))
        self.min_move_ticks = float(config.get("min_move_ticks", 2))
        if self.horizon < 1 or self.lookback < 1 or self.bucket < 1:
            raise ValueError("horizon_minutes/lookback_days/bucket_minutes must be positive")
        self._bars: dict[str, dict[datetime, dict]] = {"eur": {}, "jpy": {}}
        self._features: dict[str, dict[datetime, dict]] = {"eur": {}, "jpy": {}}
        self._ends: dict[str, list[datetime]] = {"eur": [], "jpy": []}
        self._history_cache: dict[tuple, tuple[float, float, int, int] | None] = {}
        self._history_reasons: dict[tuple, str] = {}
        for name, bars in (("eur", eur_bars), ("jpy", jpy_bars)):
            if bars is not None:
                if hasattr(bars, "to_dict"):
                    bars = bars.to_dict("records")
                for bar in bars:
                    self._store_bar(name, bar)
                self._rebuild(name)

    def _store_bar(self, name: str, bar: dict) -> None:
        item = dict(bar)
        item["start"] = as_datetime(item["start"])
        for field in ("open", "high", "low", "close"):
            item[field] = float(item[field])
        if item.get("available_at"):
            item["available_at"] = as_datetime(item["available_at"])
        self._bars[name][item["start"]] = item

    def _rebuild(self, name: str) -> None:
        bars = self._bars[name]
        features = {}
        for start in sorted(bars):
            item = self._feature(name, start)
            if item is not None:
                features[start + timedelta(minutes=1)] = item
        self._features[name] = features
        self._ends[name] = sorted(features)
        self._history_cache.clear()

    def _feature(self, name: str, start: datetime) -> dict | None:
        bars = self._bars[name]
        current = bars[start]
        segment = []
        for offset in (0, self.horizon):
            row = bars.get(start - timedelta(minutes=offset))
            if row is None or row["close"] <= 0:
                return None
            volume = row.get("volume", "")
            if self.config.get("require_positive_volume", True):
                try:
                    if float(volume) <= 0:
                        return None
                except (TypeError, ValueError):
                    return None
            segment.append(row)
        symbols = {str(x.get("symbol", "")) for x in segment if x.get("symbol")}
        if len(symbols) > 1:
            return None
        availability = [x["available_at"] for x in segment if x.get("available_at")]
        return {
            "change": -math.log(current["close"] / segment[-1]["close"]),
            "move": abs(current["close"] - segment[-1]["close"]),
            "end": start + timedelta(minutes=1),
            "available_at": max(availability) if availability else None,
            "symbol": current.get("symbol", ""),
        }

    def add_bar(self, symbol: str, bar: dict) -> None:
        """Add a completed bar from the feed; symbol is eur/jpy or the product code."""
        token = str(symbol).lower()
        name = "eur" if token == "eur" or token.startswith("6e") else "jpy" if token == "jpy" or token.startswith("6j") else None
        if name is None:
            raise ValueError(f"Unknown observation symbol: {symbol}")
        self._store_bar(name, bar)
        start = as_datetime(bar["start"])
        # Updating a recent completed bar also updates subsequent affected features.
        for offset in range(self.horizon + 1):
            target = start + timedelta(minutes=offset)
            if target in self._bars[name]:
                feature = self._feature(name, target)
                end = target + timedelta(minutes=1)
                if feature is None:
                    self._features[name].pop(end, None)
                else:
                    self._features[name][end] = feature
        self._ends[name] = sorted(self._features[name])
        # Current-day additions cannot alter prior-day calibration. Historical
        # backfill can, and therefore invalidates its relevant cached calibration.
        self._history_cache.clear()

    def _calibration(self, name: str, current: datetime):
        bucket_id = (current.hour * 60 + current.minute) // self.bucket
        key = (name, current.date(), bucket_id)
        if key in self._history_cache:
            return self._history_cache[key], self._history_reasons.get(key, "")
        features = self._features[name]
        candidates: dict[Any, list[float]] = {}
        for end in self._ends[name]:
            if end.date() >= current.date():
                break
            if (end.hour * 60 + end.minute) // self.bucket != bucket_id:
                continue
            f = features[end]
            # A historical observation with known late availability is not made
            # retroactively available to an earlier decision.
            if f.get("available_at") and f["available_at"] > current:
                continue
            candidates.setdefault(end.date(), []).append(f["change"])
        days = sorted(candidates)[-self.lookback:]
        values = [value for day in days for value in candidates[day]]
        reason = ""
        result = None
        if len(days) < self.min_days:
            reason = f"{name}: history days {len(days)} < {self.min_days}"
        elif len(values) < self.min_samples:
            reason = f"{name}: samples {len(values)} < {self.min_samples}"
        elif len(values) < 2:
            reason = f"{name}: cannot estimate standard deviation"
        else:
            mu, sigma = mean(values), stdev(values)
            if not math.isfinite(sigma) or sigma < self.min_sigma:
                reason = f"{name}: sigma below minimum"
            else:
                result = (mu, sigma, len(days), len(values))
        self._history_cache[key] = result
        self._history_reasons[key] = reason
        return result, reason

    def evaluate(self, ts: datetime, side: str):
        ts = as_datetime(ts)
        details: dict[str, Any] = {
            "fx_state": "UNAVAILABLE", "fx_valid": 0, "fx_end": "",
            "eur_change": "", "jpy_change": "", "eur_z": "", "jpy_z": "",
            "eur_history_days": 0, "jpy_history_days": 0,
            "eur_samples": 0, "jpy_samples": 0,
            "eur_age_seconds": "", "jpy_age_seconds": "",
        }
        missing = []
        observed = {}
        for name in ("eur", "jpy"):
            ends = self._ends[name]
            index = bisect_right(ends, ts) - 1
            if index < 0:
                missing.append(f"{name}: no completed traded endpoint pair")
                continue
            feature = self._features[name][ends[index]]
            age = (ts - feature["end"]).total_seconds()
            details[f"{name}_age_seconds"] = age
            if age > self.max_age:
                missing.append(f"{name}: stale by {age:.0f}s")
                continue
            if feature.get("available_at") and feature["available_at"] > ts:
                missing.append(f"{name}: not received at decision time")
                continue
            calibration, reason = self._calibration(name, ts)
            details[f"{name}_change"] = feature["change"]
            if calibration is None:
                missing.append(reason)
                continue
            mu, sigma, n_days, n_samples = calibration
            z = (feature["change"] - mu) / sigma
            details.update({f"{name}_z": round(z, 6),
                            f"{name}_history_days": n_days, f"{name}_samples": n_samples,
                            f"{name}_mean": mu, f"{name}_sigma": sigma})
            observed[name] = (feature, z)
        if len(observed) == 2 and self.config.get("require_same_end", True):
            if observed["eur"][0]["end"] != observed["jpy"][0]["end"]:
                missing.append("EUR/JPY feature endpoints are not synchronized")
        if missing:
            reason = "FX_DATA_UNAVAILABLE: " + "; ".join(missing)
            if self.enabled or self.require_coverage:
                return False, reason, details
            details["fx_state"] = "DISABLED_DATA_UNAVAILABLE"
            return True, "", details
        details["fx_valid"] = 1
        details["fx_end"] = observed["eur"][0]["end"].isoformat()
        strong = weak = True
        for name, (feature, z) in observed.items():
            tick = float(self.config.get(f"{name}_tick_size", 0))
            enough_move = feature["move"] >= tick * self.min_move_ticks
            strong = strong and enough_move and feature["change"] > 0 and z >= self.threshold
            weak = weak and enough_move and feature["change"] < 0 and z <= -self.threshold
        state = "USD_STRONG" if strong else "USD_WEAK" if weak else "NO_COMMON_SHOCK"
        details["fx_state"] = state
        if self.enabled and state == "USD_STRONG" and side == SELL:
            return False, "FX_USD_STRONG: overseas common move blocks USD/KRW short", details
        if self.enabled and state == "USD_WEAK" and side == BUY:
            return False, "FX_USD_WEAK: overseas common move blocks USD/KRW long", details
        return True, "", details

    __call__ = evaluate


if __name__ == "__main__":
    # All executable decisions still go through the same Engine/FXShockFilter.
    from backtest import main
    raise SystemExit(main())
