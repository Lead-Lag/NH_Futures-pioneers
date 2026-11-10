"""페어(스프레드) 판정과 포지션·위험 관리. 실시간 실행(run_pair.py)과 백테스트(backtest_pair.py)가
**같은 이 코드**를 씁니다. 2주차 engine.py 의 구조(봉 마감 판정 · Intent · 일일 한도 사전 차단)를 이어받았습니다.

전략: 삼성전자·SK하이닉스 주식선물 상대가치 페어 (1분봉, 평균회귀)
    스프레드    s = ln(삼성 종가) − β · ln(하이닉스 종가)        β 는 실행 전 데이터로 정하고 고정
    z           (s − 최근 window 봉 평균) ÷ 최근 window 봉 표준편차   (하루 안에서만, 2주차 볼린저와 같은 방식)
    진입        z 가 +entry_z 를 위로 넘는 봉 → 스프레드 매도 (삼성 매도 · 하이닉스 매수)
                z 가 −entry_z 를 아래로 넘는 봉 → 스프레드 매수 (삼성 매수 · 하이닉스 매도)
                (entry_mode="reentry" 면 밖으로 나갔다가 다시 안으로 들어온 봉 — 실행 전 분석에서 더 나빠 기본은 끔)
    수렴 청산   |z| ≤ exit_z                                     (봉 마감 판정)
    z 손절      |z| ≥ stop_z (진입 방향으로 더 벌어짐)            (봉 마감 판정)
    금액 손절   두 다리 합산 평가손실 ≥ pair_stop_won              (현재가로 수시 판정)
    시간 청산   보유 max_hold_bars 봉
    장 마감     flat_time 이후 전량 정리
진입은 **그 봉에 두 종목 모두 체결이 있었을 때만** 합니다(한쪽 가격이 멈춰 생긴 가짜 괴리 방지).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from itertools import count
from typing import Any, Protocol

BUY, SELL = "BUY", "SELL"
ENTRY_LONG, ENTRY_SHORT = "ENTRY_LONG_SPREAD", "ENTRY_SHORT_SPREAD"
EXIT_TP, EXIT_ZSTOP, EXIT_AMTSTOP = "EXIT_CONVERGE", "EXIT_ZSTOP", "EXIT_AMOUNT_STOP"
EXIT_TIME, EXIT_EOD, EXIT_MANUAL = "EXIT_TIME", "EXIT_EOD", "EXIT_MANUAL"
STOPS = (EXIT_ZSTOP, EXIT_AMTSTOP)

# 페어 주문 결과 (pair_broker.PairOutcome.status)
COMPLETE = "COMPLETE"   # 계획대로 두 다리 체결
ABORTED = "ABORTED"     # 첫 다리부터 체결 안 됨 → 노출 없음
UNWOUND = "UNWOUND"     # 한쪽만 체결 → 체결된 다리를 되돌려 노출 정리 완료
BROKEN = "BROKEN"       # 되돌리기·청산을 끝내지 못함 → 노출 남음, 신규 진입 중지


@dataclass
class PairParams:
    beta: float = 0.59
    window: int = 40
    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 3.5
    entry_mode: str = "immediate"     # immediate | reentry
    max_hold_bars: int = 60
    cooldown_bars_after_stop: int = 3
    session_start: str = "09:00"      # 이 시각 이전에 시작한 봉은 지표에 넣지 않음(실행 전 분석과 같게)
    entry_start: str = "09:10"        # 봉 마감 시각 기준
    entry_end: str = "15:00"
    flat_time: str = "15:15"
    require_fresh: bool = True
    qty_s: int = 10                   # 삼성 계약 수
    qty_h: int = 1                    # 하이닉스 계약 수
    mult_s: float = 10                # 거래승수(주)
    mult_h: float = 10
    tick_s: float = 100               # 호가단위(원) — 미끄러짐 계산용
    tick_h: float = 1000
    first_leg: str = "H"              # 먼저 보낼 다리 (체결이 덜 확실한 쪽)
    pair_stop_won: float = 150_000    # 페어 합산 평가손실 손절(원)
    daily_loss_limit: float = 500_000  # 남은 한도 < pair_stop_won 이면 신규 진입 막음
    max_trades_per_day: int = 20
    fee_per_contract: float = 0.0     # 원, 편도

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "PairParams":
        return PairParams(**{k: v for k, v in data.items() if k in PairParams.__dataclass_fields__})


class Journal(Protocol):
    def bar(self, row: dict[str, Any]) -> None: ...
    def signal(self, row: dict[str, Any]) -> None: ...
    def trade(self, row: dict[str, Any]) -> None: ...
    def event(self, kind: str, message: str, **extra: Any) -> None: ...


@dataclass
class Leg:
    name: str      # "S" 삼성 | "H" 하이닉스
    side: str      # BUY | SELL
    qty: int


@dataclass
class PairIntent:
    id: int
    ts: datetime
    purpose: str
    direction: int            # 이 주문 뒤(진입) 또는 이 주문 전(청산)의 스프레드 방향 +1 매수 / −1 매도
    reason: str
    legs: list[Leg]           # 보낼 순서대로
    ref: dict[str, float]     # 판정 때 가격(신호 봉 종가 또는 현재가) — 미끄러짐 계산용
    snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        return self.purpose in (ENTRY_LONG, ENTRY_SHORT)


_ids = count(1)


def _hm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def legs_for(direction: int, qty_s: int, qty_h: int, first: str, closing: bool = False) -> list[Leg]:
    """스프레드 방향 → 두 다리 주문. 스프레드 매수(+1) = 삼성 매수 · 하이닉스 매도. 청산은 반대."""
    d = -direction if closing else direction
    s = Leg("S", BUY if d > 0 else SELL, qty_s)
    h = Leg("H", SELL if d > 0 else BUY, qty_h)
    return [h, s] if first == "H" else [s, h]


class PairEngine:
    def __init__(self, params: PairParams, journal: Journal) -> None:
        self.p = params
        self.j = journal
        self.spreads: deque[float] = deque(maxlen=params.window)
        self.prev_z: float | None = None
        self.day = None
        self.direction = 0            # +1 스프레드 매수 보유, −1 매도 보유, 0 없음
        self.open_qty = {"S": 0, "H": 0}
        self.entry_px = {"S": 0.0, "H": 0.0}
        self.entry_time: datetime | None = None
        self.entry_z = 0.0
        self.held_bars = 0
        self.pending: PairIntent | None = None
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.cooldown = 0
        self.halted = ""
        self.fail_count = 0
        self.warmup = True
        self.trade_no = 0
        self.last_z: float | None = None
        self.last_mu = self.last_sd = None

    # ---------------------------------------------------------------- 하루 초기화
    def _new_day(self, d) -> None:
        self.day = d
        self.spreads.clear()
        self.prev_z = None
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.cooldown = 0
        if self.halted.startswith("일일"):
            self.halted = ""
        self.j.event("day", f"거래일 시작 {d}")

    def spread(self, ps: float, ph: float) -> float:
        return math.log(ps) - self.p.beta * math.log(ph)

    def expected_positions(self) -> dict[str, int]:
        """엔진이 생각하는 계좌 잔고(매수 +, 매도 −). 잔고 대조에 씁니다."""
        d = self.direction
        return {"S": d * self.open_qty["S"], "H": -d * self.open_qty["H"]}

    def mtm(self, ps: float, ph: float) -> float:
        """보유 페어의 평가손익(원, 비용 전)."""
        if not self.direction:
            return 0.0
        d = self.direction
        return (d * (ps - self.entry_px["S"]) * self.open_qty["S"] * self.p.mult_s
                - d * (ph - self.entry_px["H"]) * self.open_qty["H"] * self.p.mult_h)

    # ---------------------------------------------------------------- 봉 마감
    def on_bar_close(self, pb: dict[str, Any]) -> PairIntent | None:
        """pb = {start, close_s, close_h, fresh_s, fresh_h}. fresh = 그 분에 그 종목 체결이 있었는지."""
        start: datetime = pb["start"]
        end = start + timedelta(minutes=1)
        if self.day != start.date():
            self._new_day(start.date())
        if start.time() < _hm(self.p.session_start):
            return None
        ps, ph = float(pb["close_s"]), float(pb["close_h"])
        fresh = bool(pb.get("fresh_s")) and bool(pb.get("fresh_h"))
        s = self.spread(ps, ph)
        self.spreads.append(s)
        z = None
        if len(self.spreads) == self.p.window:
            mu = sum(self.spreads) / self.p.window
            sd = math.sqrt(sum((x - mu) ** 2 for x in self.spreads) / self.p.window)
            z = (s - mu) / sd if sd > 0 else None
            self.last_mu, self.last_sd = mu, sd
        prev_z, self.prev_z = self.prev_z, z
        self.last_z = z

        self.j.bar({
            "start": start.isoformat(), "close_s": ps, "close_h": ph,
            "fresh_s": int(bool(pb.get("fresh_s"))), "fresh_h": int(bool(pb.get("fresh_h"))),
            "spread": round(s, 6), "z": "" if z is None else round(z, 3),
            "direction": self.direction, "warmup": int(self.warmup),
        })

        if self.direction and not self.pending:
            self.held_bars += 1
        if self.cooldown > 0:
            self.cooldown -= 1
        if self.warmup or self.pending or z is None:
            return None

        snap = {"z": round(z, 3), "spread": round(s, 6), "mu": round(self.last_mu, 6),
                "sd_bp": round(self.last_sd * 1e4, 2), "close_s": ps, "close_h": ph, "fresh": int(fresh)}

        # ---- 보유 중: 청산 판정
        if self.direction:
            d = self.direction
            snap["held_bars"] = self.held_bars
            if end.time() >= _hm(self.p.flat_time):
                return self._exit(end, EXIT_EOD, f"장 마감 정리({self.p.flat_time})", ps, ph, snap)
            if (d < 0 and z <= self.p.exit_z) or (d > 0 and z >= -self.p.exit_z):
                return self._exit(end, EXIT_TP, f"수렴 z={z:.2f}", ps, ph, snap)
            if (d < 0 and z >= self.p.stop_z) or (d > 0 and z <= -self.p.stop_z):
                return self._exit(end, EXIT_ZSTOP, f"괴리 확대 z={z:.2f}", ps, ph, snap)
            if self.held_bars >= self.p.max_hold_bars:
                return self._exit(end, EXIT_TIME, f"보유 {self.held_bars}봉 경과", ps, ph, snap)
            return None

        # ---- 무포지션: 진입 판정
        if prev_z is None:
            return None
        direction, why = 0, ""
        k, x = self.p.entry_z, self.p.exit_z
        if self.p.entry_mode == "reentry":
            if prev_z > k >= z > x:
                direction, why = -1, f"z {prev_z:.2f}→{z:.2f} 상단 이탈 뒤 복귀"
            elif prev_z < -k <= z < -x:
                direction, why = +1, f"z {prev_z:.2f}→{z:.2f} 하단 이탈 뒤 복귀"
        else:
            if z >= k > prev_z:
                direction, why = -1, f"z {prev_z:.2f}→{z:.2f} 삼성 상대 고평가"
            elif z <= -k < prev_z:
                direction, why = +1, f"z {prev_z:.2f}→{z:.2f} 삼성 상대 저평가"
        if not direction:
            return None
        purpose = ENTRY_LONG if direction > 0 else ENTRY_SHORT
        blocked = self._entry_block(end, fresh)
        self.j.signal({"ts": end.isoformat(), "bar_start": start.isoformat(), "signal": purpose, "reason": why,
                       "prev_z": round(prev_z, 3), **snap, "taken": int(not blocked), "blocked_by": blocked})
        if blocked:
            return None
        self.pending = PairIntent(next(_ids), end, purpose, direction, why,
                                  legs_for(direction, self.p.qty_s, self.p.qty_h, self.p.first_leg),
                                  {"S": ps, "H": ph}, snap)
        return self.pending

    def _entry_block(self, end: datetime, fresh: bool) -> str:
        t = end.time()
        if self.halted:
            return f"중지:{self.halted}"
        if t < _hm(self.p.entry_start) or t > _hm(self.p.entry_end):
            return "진입시간 밖"
        if self.p.require_fresh and not fresh:
            return "한쪽 종목 그 분 체결 없음"
        if self.cooldown > 0:
            return f"손절 뒤 대기 {self.cooldown}봉"
        if self.daily_trades >= self.p.max_trades_per_day:
            return "하루 거래 횟수 한도"
        remain = self.p.daily_loss_limit + self.daily_pnl
        if remain < self.p.pair_stop_won:
            return f"남은 손실한도 {remain:,.0f} < 1회 손절액 {self.p.pair_stop_won:,.0f}"
        return ""

    def _exit(self, ts: datetime, purpose: str, reason: str, ps: float, ph: float, snap: dict[str, Any]) -> PairIntent:
        legs = legs_for(self.direction, self.open_qty["S"], self.open_qty["H"], self.p.first_leg, closing=True)
        self.pending = PairIntent(next(_ids), ts, purpose, self.direction, reason, legs, {"S": ps, "H": ph}, snap)
        return self.pending

    # ---------------------------------------------------------------- 현재가(금액 손절)
    def on_prices(self, ps: float, ph: float, ts: datetime) -> PairIntent | None:
        if not self.direction or self.pending or self.warmup or ps <= 0 or ph <= 0:
            return None
        pnl = self.mtm(ps, ph)
        if pnl <= -self.p.pair_stop_won:
            return self._exit(ts, EXIT_AMTSTOP, f"페어 평가손실 {pnl:,.0f}원 ≤ −{self.p.pair_stop_won:,.0f}",
                              ps, ph, {"mtm": round(pnl)})
        return None

    def on_clock(self, ts: datetime) -> PairIntent | None:
        if self.direction and not self.pending and ts.time() >= _hm(self.p.flat_time):
            return self._exit(ts, EXIT_EOD, f"장 마감 정리({self.p.flat_time})", 0.0, 0.0, {})
        return None

    def force_exit(self, ts: datetime, reason: str) -> PairIntent | None:
        if not self.direction or self.pending:
            return None
        return self._exit(ts, EXIT_MANUAL, reason, 0.0, 0.0, {})

    # ---------------------------------------------------------------- 주문 결과 반영
    def on_pair_done(self, intent: PairIntent, outcome: Any, ts: datetime) -> None:
        """outcome: pair_broker.PairOutcome (status · qty{S,H} · avg{S,H} · unwind_pnl)."""
        self.pending = None
        st = outcome.status
        if st == BROKEN:
            self.halted = "레그 불일치(노출 남음)"
            self.j.event("risk", f"{intent.purpose} 결과 BROKEN — 신규 진입 중지, 잔고 기준 정리 필요")
            return
        if intent.is_entry:
            if st == COMPLETE:
                self.fail_count = 0
                self.direction = intent.direction
                self.open_qty = {"S": int(outcome.qty["S"]), "H": int(outcome.qty["H"])}
                self.entry_px = {"S": float(outcome.avg["S"]), "H": float(outcome.avg["H"])}
                self.entry_time = ts
                self.entry_z = float(intent.snapshot.get("z", 0.0) or 0.0)
                self.held_bars = 0
                self.daily_trades += 1
                self.j.event("position", f"스프레드 {'매수' if self.direction > 0 else '매도'} 보유 — "
                                         f"삼성 {self.open_qty['S']}@{self.entry_px['S']:,.0f} · "
                                         f"하이닉스 {self.open_qty['H']}@{self.entry_px['H']:,.0f}")
                return
            # ABORTED / UNWOUND: 보유 없음. 되돌리며 난 손익은 하루 손익에 넣음
            self.daily_pnl += float(getattr(outcome, "unwind_pnl", 0.0) or 0.0)
            self.fail_count += 1
            self.j.event("order_failed", f"{intent.purpose} {st}: {outcome.message} "
                                         f"(되돌리기 손익 {getattr(outcome, 'unwind_pnl', 0):,.0f}원)")
            if self.fail_count >= 3:
                self.halted = "페어 진입 연속 실패 3회"
                self.j.event("risk", "페어 진입 연속 실패 3회 — 신규 진입 중지(청산 판정은 계속)")
            self._check_daily()
            return

        # 청산
        if st != COMPLETE:
            self.halted = f"청산 {st}"
            self.j.event("risk", f"청산이 {st} — 신규 진입 중지")
            return
        d = self.direction
        px_s, px_h = float(outcome.avg["S"]), float(outcome.avg["H"])
        pnl_s = d * (px_s - self.entry_px["S"]) * self.open_qty["S"] * self.p.mult_s
        pnl_h = -d * (px_h - self.entry_px["H"]) * self.open_qty["H"] * self.p.mult_h
        fee = 2 * self.p.fee_per_contract * (self.open_qty["S"] + self.open_qty["H"])
        net = pnl_s + pnl_h - fee
        self.daily_pnl += net
        self.trade_no += 1
        sp_in = self.spread(self.entry_px["S"], self.entry_px["H"])
        sp_out = self.spread(px_s, px_h)
        self.j.trade({
            "trade_no": self.trade_no, "direction": "LONG_SPREAD" if d > 0 else "SHORT_SPREAD",
            "qty_s": self.open_qty["S"], "qty_h": self.open_qty["H"],
            "entry_time": self.entry_time.isoformat() if self.entry_time else "",
            "entry_s": self.entry_px["S"], "entry_h": self.entry_px["H"], "entry_z": self.entry_z,
            "exit_time": ts.isoformat(), "exit_s": px_s, "exit_h": px_h, "exit_reason": intent.purpose,
            "spread_bp": round(d * (sp_out - sp_in) * 1e4, 2),
            "pnl_s": round(pnl_s), "pnl_h": round(pnl_h), "fee": round(fee), "net_pl": round(net),
            "held_bars": self.held_bars,
            "held_sec": round((ts - self.entry_time).total_seconds()) if self.entry_time else "",
            "daily_pnl_after": round(self.daily_pnl),
        })
        if intent.purpose in STOPS:
            self.cooldown = self.p.cooldown_bars_after_stop
        self.mark_flat()
        self._check_daily()

    def _check_daily(self) -> None:
        if self.daily_pnl <= -self.p.daily_loss_limit and not self.halted:
            self.halted = "일일 손실 한도 도달"
            self.j.event("risk", f"일일 손익 {self.daily_pnl:,.0f}원 — 오늘 신규 진입 중지")

    def mark_flat(self) -> None:
        self.direction = 0
        self.open_qty = {"S": 0, "H": 0}
        self.entry_px = {"S": 0.0, "H": 0.0}
        self.entry_time = None
        self.held_bars = 0

    def sync_positions(self, broker: dict[str, int], ts: datetime) -> bool:
        """잔고와 엔진 상태가 다르면 기록하고 신규 진입을 멈춥니다. 같으면 True."""
        exp = self.expected_positions()
        if broker.get("S", 0) != exp["S"] or broker.get("H", 0) != exp["H"]:
            self.halted = "잔고 불일치"
            self.j.event("mismatch", f"엔진 {exp} ≠ 잔고 {broker} — 신규 진입 중지", ts=ts.isoformat())
            return False
        return True


def params_dict(p: PairParams) -> dict[str, Any]:
    return asdict(p)


def align_minutes(bars_s: list[dict], bars_h: list[dict], after: datetime | None, upto: datetime,
                  last_close: dict[str, float]) -> list[dict]:
    """두 종목 분봉을 1분 격자에 맞춥니다(실행·백테스트 공통).

    봉 = {start, close, volume}. NH 분봉은 그 분에 체결이 없으면 봉 자체가 오지 않으므로,
    빈 분은 직전 종가를 쓰고 fresh=False 로 표시합니다. after 다음 분부터, 끝난 봉(start+1분 ≤ upto)만.
    last_close 는 호출 사이에 이어지는 직전 종가(같은 dict 를 계속 넘김).
    """
    one = timedelta(minutes=1)
    by = {"S": {b["start"]: b for b in bars_s}, "H": {b["start"]: b for b in bars_h}}
    starts = sorted(set(by["S"]) | set(by["H"]))
    if not starts:
        return []
    t = (after + one) if after else starts[0]
    out = []
    while t + one <= upto:
        if after is None or t.date() == after.date() or t in by["S"] or t in by["H"]:
            row: dict[str, Any] = {"start": t}
            for k in ("S", "H"):
                b = by[k].get(t)
                if b and b["close"] > 0:
                    last_close[k] = b["close"]
                    row[f"fresh_{k.lower()}"] = b.get("volume", 1) > 0
                else:
                    row[f"fresh_{k.lower()}"] = False
                row[f"close_{k.lower()}"] = last_close.get(k, 0.0)
            if row["close_s"] > 0 and row["close_h"] > 0:
                out.append(row)
        t += one
    return out
