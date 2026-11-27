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

6주차 위험관리 (설정을 켜지 않으면 4주차 동작 그대로)
    분할 진입   첫 진입 init_units 단위(1단위 = qty_s : qty_h), z 가 add_z 까지 더 벌어지면 1단위 추가 (최대 max_units)
    분할 청산   2단위 이상 보유 중 |z| ≤ partial_exit_z 이면 절반 단위 먼저 청산, 나머지는 exit_z 에서
    금액 손절   stop_per_unit 이면 pair_stop_won × 보유 단위
    변동성 손절 stop_sigma_k > 0 이면 금액 손절선 = k × 진입 때 스프레드 σ × 삼성 다리 명목금액 (바닥 stop_floor_won · 천장 stop_cap_won)
                (스프레드 손익 ≈ 명목금액 × 스프레드 변화이므로 「평소 흔들림의 k 배 밀리면 자름」. 0.2초마다 판정)
    연속 손실   포지션 단위 손실이 max_consec_losses 번 이어지면 그날 신규 진입 중지
    평가손익    open_pnl_in_limit 이면 남은 손실한도 계산에 보유 평가손익도 넣음
    모니터링    monitor_block(실행 쪽이 채움: 실시간 끊김 등)이 있으면 신규 진입 안 함
    risk_off    「위험관리 없음」 비교용 — 진입과 수렴 청산·장 마감 정리만 남기고 손절·시간 청산·한도·대기를 모두 끔
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
ENTRY_ADD, EXIT_PARTIAL, EXIT_MONITOR = "ENTRY_ADD", "EXIT_PARTIAL", "EXIT_MONITOR"
EXIT_BREAK = "EXIT_BREAK"
STOPS = (EXIT_ZSTOP, EXIT_AMTSTOP, EXIT_BREAK)

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
    min_window_fresh: float = 0.8     # z 계산 창(40봉) 중 두 종목 모두 체결된 봉 비율 하한 — 끊김 뒤 평평한 봉으로 σ 가 작아져 z 가 튀는 것 방지(11/11)
    qty_s: int = 10                   # 삼성 계약 수
    qty_h: int = 1                    # 하이닉스 계약 수
    mult_s: float = 10                # 거래승수(주)
    mult_h: float = 10
    tick_s: float = 100               # 호가단위(원) 기본값 — 실제 계산은 tick_size(가격)
    tick_h: float = 1000
    first_leg: str = "H"              # 먼저 보낼 다리 (체결이 덜 확실한 쪽)
    pair_stop_won: float = 150_000    # 페어 합산 평가손실 손절(원)
    daily_loss_limit: float = 500_000  # 남은 한도 < pair_stop_won 이면 신규 진입 막음
    max_trades_per_day: int = 20
    fee_per_contract: float = 0.0     # 원, 편도
    # ---- 6주차 위험관리 (기본값 = 4주차와 같음)
    risk_off: bool = False            # 비교용: 손절·시간 청산·한도·대기 모두 끔
    init_units: int = 1               # 첫 진입 단위 수
    max_units: int = 1                # 보유 최대 단위 (2 = 삼성 20 · 하이닉스 2)
    add_z: float = 0.0                # 0 = 분할 진입 끔. 예 2.5 → z 가 ±2.5 를 넘으면 1단위 추가
    partial_exit_z: float = 0.0       # 0 = 분할 청산 끔. 예 1.0 → |z| ≤ 1.0 에서 절반 단위 청산
    stop_per_unit: bool = False       # 금액 손절선을 보유 단위에 비례
    max_consec_losses: int = 0        # 0 = 끔. 포지션 단위 연속 손실 횟수 한도
    open_pnl_in_limit: bool = False   # 남은 손실한도에 보유 평가손익 포함
    stop_sigma_k: float = 0.0         # 0 = 끔(고정 pair_stop_won). 예 4.0 → 진입 때 σ × 명목금액 × 4
    stop_floor_won: float = 0.0       # 변동성 손절선의 바닥(원) — 조용한 때 호가 튕김에 잘리지 않게
    stop_cap_won: float = 0.0         # 변동성 손절선의 천장(원, 0 = 없음) — 거친 때 한 포지션 손실이 너무 커지지 않게
    # ---- 6주차 「속도」 진입 필터 (기본 끔)
    speed_filter: str = ""            # "" | lead_h(하이닉스가 먼저 움직인 괴리만) | lead_s | fast(z 급변만) | slow(z 완만만)
    speed_bars: int = 3               # 속도를 재는 봉 수
    jump_z: float = 1.5               # fast/slow 경계: speed_bars 봉 동안 z 변화 크기
    # ---- 6주차 「관계 붕괴」 손절 (기본 끔)
    break_corr: float = -9.0          # -9 = 끔. 보유 중 최근 break_bars 봉 1분 수익률 상관이 이 값보다 낮으면 청산
    break_bars: int = 30

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
        return self.purpose in (ENTRY_LONG, ENTRY_SHORT, ENTRY_ADD)


_ids = count(1)


# KRX 주식·주식선물 호가가격단위(2023.1~). 삼성 선물은 20만원을 넘으면 100원 → 500원으로 바뀜(11/11 실행에서 확인).
_TICK_TABLE = ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500))


def tick_size(price: float, default: float = 0.0) -> float:
    """가격 수준에 맞는 호가단위(원). price 가 없으면 default."""
    if not price or price <= 0:
        return default
    for limit, tick in _TICK_TABLE:
        if price < limit:
            return float(tick)
    return 1000.0


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
        self.freshes: deque[bool] = deque(maxlen=params.window)
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
        self.units = 0                # 보유 단위 수
        self.partial_done = False
        self.pos_realized = 0.0       # 이 포지션에서 분할 청산으로 이미 확정한 손익
        self.worst_mtm = 0.0          # 보유 중 가장 나빴던 평가손익(MAE)
        self.best_mtm = 0.0
        self.consec_losses = 0
        self.monitor_block = ""       # 실행 쪽 모니터링이 채움(실시간 끊김 등). 비면 정상
        self.entry_sd = 0.0           # 진입 때 스프레드 σ(변동성 손절용)
        self._last_ps = 0.0
        self.hist: deque = deque(maxlen=60)   # (ln 삼성, ln 하이닉스, z) — 속도 필터용, 하루 안에서만

    # ---------------------------------------------------------------- 하루 초기화
    def _new_day(self, d) -> None:
        self.day = d
        self.spreads.clear()
        self.freshes.clear()
        self.prev_z = None
        self.hist.clear()
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.cooldown = 0
        self.consec_losses = 0
        if self.halted.startswith("일일") or self.halted.startswith("연속 손실"):
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
        self.freshes.append(fresh)
        z = None
        if len(self.spreads) == self.p.window:
            mu = sum(self.spreads) / self.p.window
            sd = math.sqrt(sum((x - mu) ** 2 for x in self.spreads) / self.p.window)
            z = (s - mu) / sd if sd > 0 else None
            self.last_mu, self.last_sd = mu, sd
            self._last_ps = ps
        prev_z, self.prev_z = self.prev_z, z
        self.last_z = z
        self.hist.append((math.log(ps), math.log(ph), z))

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
            self._track(ps, ph)
            snap["held_bars"] = self.held_bars
            snap["units"] = self.units
            if end.time() >= _hm(self.p.flat_time):
                return self._exit(end, EXIT_EOD, f"장 마감 정리({self.p.flat_time})", ps, ph, snap)
            if (d < 0 and z <= self.p.exit_z) or (d > 0 and z >= -self.p.exit_z):
                return self._exit(end, EXIT_TP, f"수렴 z={z:.2f}", ps, ph, snap)
            if self.p.risk_off:
                return None
            if (d < 0 and z >= self.p.stop_z) or (d > 0 and z <= -self.p.stop_z):
                return self._exit(end, EXIT_ZSTOP, f"괴리 확대 z={z:.2f}", ps, ph, snap)
            if self.p.break_corr > -1:
                rc = self.recent_corr(self.p.break_bars)
                if rc is not None and rc < self.p.break_corr:
                    snap["corr"] = round(rc, 3)
                    return self._exit(end, EXIT_BREAK, f"관계 붕괴 — 최근 {self.p.break_bars}봉 상관 {rc:.2f}", ps, ph, snap)
            if self.held_bars >= self.p.max_hold_bars:
                return self._exit(end, EXIT_TIME, f"보유 {self.held_bars}봉 경과", ps, ph, snap)
            # 분할 청산: 2단위 이상이면 절반 단위를 먼저 (z 가 partial_exit_z 안으로)
            pz = self.p.partial_exit_z
            if pz > 0 and self.units >= 2 and not self.partial_done and abs(z) <= pz:
                n = self.units // 2
                return self._exit(end, EXIT_PARTIAL, f"분할 청산 {n}/{self.units}단위 z={z:.2f}", ps, ph, snap,
                                  units=n)
            # 분할 진입: z 가 add_z 를 진입 방향으로 넘으면 1단위 추가
            az = self.p.add_z
            if (az > 0 and self.units < self.p.max_units and not self.partial_done and prev_z is not None
                    and ((d < 0 and z >= az > prev_z) or (d > 0 and z <= -az < prev_z))):
                blocked = self._entry_block(end, fresh, add=True)
                self.j.signal({"ts": end.isoformat(), "bar_start": start.isoformat(), "signal": ENTRY_ADD,
                               "reason": f"z {prev_z:.2f}→{z:.2f} 분할 진입 {self.units + 1}단위째",
                               "prev_z": round(prev_z, 3), **snap, "taken": int(not blocked), "blocked_by": blocked})
                if blocked:
                    return None
                self.pending = PairIntent(next(_ids), end, ENTRY_ADD, d, f"분할 진입 z={z:.2f}",
                                          legs_for(d, self.p.qty_s, self.p.qty_h, self.p.first_leg),
                                          {"S": ps, "H": ph}, snap)
                return self.pending
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
        speed_info = self.speed()
        snap.update(speed_info)
        blocked = self._entry_block(end, fresh) or self._speed_block(speed_info)
        self.j.signal({"ts": end.isoformat(), "bar_start": start.isoformat(), "signal": purpose, "reason": why,
                       "prev_z": round(prev_z, 3), **snap, "taken": int(not blocked), "blocked_by": blocked})
        if blocked:
            return None
        n = max(1, min(self.p.init_units, self.p.max_units))
        self.pending = PairIntent(next(_ids), end, purpose, direction, why,
                                  legs_for(direction, n * self.p.qty_s, n * self.p.qty_h, self.p.first_leg),
                                  {"S": ps, "H": ph}, snap)
        return self.pending

    def recent_corr(self, n: int) -> float | None:
        """오늘 최근 n 봉의 1분 로그수익률 상관(삼성 ↔ 하이닉스). 봉이 모자라거나 한쪽이 안 움직였으면 None."""
        if len(self.hist) <= n:
            return None
        h = list(self.hist)[-n - 1:]
        rs = [b[0] - a[0] for a, b in zip(h, h[1:])]
        rh = [b[1] - a[1] for a, b in zip(h, h[1:])]
        ms, mh = sum(rs) / n, sum(rh) / n
        cov = sum((x - ms) * (y - mh) for x, y in zip(rs, rh))
        vs = sum((x - ms) ** 2 for x in rs)
        vh = sum((y - mh) ** 2 for y in rh)
        return cov / math.sqrt(vs * vh) if vs > 0 and vh > 0 else None

    def speed(self) -> dict[str, Any]:
        """최근 speed_bars 봉 동안의 괴리 속도와 원인. dz = z 변화, share_h = 스프레드 변화 중 하이닉스 몫."""
        k = self.p.speed_bars
        if len(self.hist) <= k:
            return {}
        s1, h1, z1 = self.hist[-1]
        s0, h0, z0 = self.hist[-1 - k]
        c_s = s1 - s0                       # 삼성이 만든 스프레드 변화
        c_h = -self.p.beta * (h1 - h0)      # 하이닉스가 만든 스프레드 변화
        tot = abs(c_s) + abs(c_h)
        out = {"dz": round(z1 - z0, 3) if (z1 is not None and z0 is not None) else "",
               "share_h": round(abs(c_h) / tot, 3) if tot > 0 else ""}
        return out

    def _speed_block(self, info: dict[str, Any]) -> str:
        f = self.p.speed_filter
        if not f:
            return ""
        if not info or info.get("dz") == "" or info.get("share_h") == "":
            return "속도: 기록 부족"
        sh, dz = float(info["share_h"]), abs(float(info["dz"]))
        if f == "lead_h" and sh < 0.5:
            return f"속도: 삼성이 만든 괴리(하이닉스 몫 {sh:.0%})"
        if f == "lead_s" and sh >= 0.5:
            return f"속도: 하이닉스가 만든 괴리(하이닉스 몫 {sh:.0%})"
        if f == "fast" and dz < self.p.jump_z:
            return f"속도: 완만한 괴리 |dz| {dz:.2f} < {self.p.jump_z}"
        if f == "slow" and dz >= self.p.jump_z:
            return f"속도: 급한 괴리 |dz| {dz:.2f} ≥ {self.p.jump_z}"
        return ""

    def stop_won(self, units: int | None = None) -> float:
        """보유 단위에 맞는 금액 손절선(원). stop_sigma_k > 0 이면 진입 때 σ × 명목금액에 비례(보유 전이면 지금 σ·가격으로 추정)."""
        u = self.units if units is None else units
        mult_u = max(1, u) if self.p.stop_per_unit else 1
        if self.p.stop_sigma_k > 0:
            sd = self.entry_sd if (self.direction and self.entry_sd) else (self.last_sd or 0.0)
            px = self.entry_px.get("S", 0.0) if self.direction else self._last_ps
            notional = self.p.qty_s * self.p.mult_s * (px or 0.0)
            base = self.p.stop_sigma_k * sd * notional
            if base <= 0:
                base = self.p.pair_stop_won
            amt = max(self.p.stop_floor_won, base)
            if self.p.stop_cap_won > 0:
                amt = min(amt, self.p.stop_cap_won)
            return amt * mult_u
        return self.p.pair_stop_won * mult_u

    def _entry_block(self, end: datetime, fresh: bool, add: bool = False) -> str:
        t = end.time()
        if self.halted:
            return f"중지:{self.halted}"
        if self.monitor_block:
            return f"모니터링:{self.monitor_block}"
        if t < _hm(self.p.entry_start) or t > _hm(self.p.entry_end):
            return "진입시간 밖"
        if self.p.require_fresh and not fresh:
            return "한쪽 종목 그 분 체결 없음"
        if self.freshes and sum(self.freshes) / len(self.freshes) < self.p.min_window_fresh:
            return f"z 계산 창의 실제 체결 봉 {sum(self.freshes)}/{len(self.freshes)} — 끊김 뒤 지표 회복 대기"
        if self.p.risk_off:
            return ""
        if self.cooldown > 0:
            return f"손절 뒤 대기 {self.cooldown}봉"
        if not add and self.daily_trades >= self.p.max_trades_per_day:
            return "하루 거래 횟수 한도"
        if not add and self.p.max_consec_losses and self.consec_losses >= self.p.max_consec_losses:
            return f"연속 손실 {self.consec_losses}회"
        remain = self.p.daily_loss_limit + self.daily_pnl
        if self.p.open_pnl_in_limit and self.direction:
            remain += min(0.0, self._last_mtm)
        units_after = (self.units + 1) if add else max(1, min(self.p.init_units, self.p.max_units))
        need = self.stop_won(units_after)
        if remain < need:
            return f"남은 손실한도 {remain:,.0f} < 손절액 {need:,.0f}"
        return ""

    def _exit(self, ts: datetime, purpose: str, reason: str, ps: float, ph: float, snap: dict[str, Any],
              units: int | None = None) -> PairIntent:
        if units is None or units >= self.units:
            qs, qh = self.open_qty["S"], self.open_qty["H"]
        else:
            qs, qh = units * self.p.qty_s, units * self.p.qty_h
        legs = legs_for(self.direction, qs, qh, self.p.first_leg, closing=True)
        self.pending = PairIntent(next(_ids), ts, purpose, self.direction, reason, legs, {"S": ps, "H": ph}, snap)
        return self.pending

    _last_mtm = 0.0

    def _track(self, ps: float, ph: float) -> None:
        """보유 중 평가손익의 최저·최고(MAE·MFE) 기록 — 손절선 근거 분석에 씀."""
        if not self.direction or ps <= 0 or ph <= 0:
            return
        m = self.mtm(ps, ph)
        self._last_mtm = m
        self.worst_mtm = min(self.worst_mtm, m)
        self.best_mtm = max(self.best_mtm, m)

    # ---------------------------------------------------------------- 현재가(금액 손절)
    def on_prices(self, ps: float, ph: float, ts: datetime) -> PairIntent | None:
        if not self.direction or self.pending or self.warmup or ps <= 0 or ph <= 0:
            return None
        self._track(ps, ph)
        if self.p.risk_off:
            return None
        pnl = self.mtm(ps, ph)
        lim = self.stop_won()
        if pnl <= -lim:
            return self._exit(ts, EXIT_AMTSTOP, f"페어 평가손실 {pnl:,.0f}원 ≤ −{lim:,.0f} ({self.units}단위)",
                              ps, ph, {"mtm": round(pnl), "units": self.units})
        return None

    def on_clock(self, ts: datetime) -> PairIntent | None:
        if self.direction and not self.pending and ts.time() >= _hm(self.p.flat_time):
            return self._exit(ts, EXIT_EOD, f"장 마감 정리({self.p.flat_time})", 0.0, 0.0, {})
        return None

    def force_exit(self, ts: datetime, reason: str, purpose: str = EXIT_MANUAL) -> PairIntent | None:
        if not self.direction or self.pending:
            return None
        return self._exit(ts, purpose, reason, 0.0, 0.0, {})

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
            if st == COMPLETE and intent.purpose == ENTRY_ADD and self.direction:
                self.fail_count = 0
                for k in ("S", "H"):
                    q0, q1 = self.open_qty[k], int(outcome.qty[k])
                    if q0 + q1 > 0:
                        self.entry_px[k] = (self.entry_px[k] * q0 + float(outcome.avg[k]) * q1) / (q0 + q1)
                    self.open_qty[k] = q0 + q1
                self.units += 1
                self.peak_units = max(self.peak_units, self.units)
                self.j.event("position", f"분할 진입 → {self.units}단위 — 평균 삼성 {self.open_qty['S']}@{self.entry_px['S']:,.0f} · "
                                         f"하이닉스 {self.open_qty['H']}@{self.entry_px['H']:,.0f} · 손절선 −{self.stop_won():,.0f}원")
                return
            if st == COMPLETE:
                self.fail_count = 0
                self.direction = intent.direction
                self.open_qty = {"S": int(outcome.qty["S"]), "H": int(outcome.qty["H"])}
                self.entry_px = {"S": float(outcome.avg["S"]), "H": float(outcome.avg["H"])}
                self.entry_time = ts
                self.entry_z = float(intent.snapshot.get("z", 0.0) or 0.0)
                self.held_bars = 0
                self.units = max(1, round(self.open_qty["S"] / self.p.qty_s)) if self.p.qty_s else 1
                self.entry_sd = float(self.last_sd or 0.0)
                self.peak_units = self.units
                self.partial_done = False
                self.pos_realized = 0.0
                self.worst_mtm = self.best_mtm = self._last_mtm = 0.0
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
        cq = {"S": int(outcome.qty["S"]), "H": int(outcome.qty["H"])}
        partial = intent.purpose == EXIT_PARTIAL and (cq["S"] < self.open_qty["S"] or cq["H"] < self.open_qty["H"])
        pnl_s = d * (px_s - self.entry_px["S"]) * cq["S"] * self.p.mult_s
        pnl_h = -d * (px_h - self.entry_px["H"]) * cq["H"] * self.p.mult_h
        fee = 2 * self.p.fee_per_contract * (cq["S"] + cq["H"])
        net = pnl_s + pnl_h - fee
        self.daily_pnl += net
        self.trade_no += 1
        pos_net = self.pos_realized + net
        sp_in = self.spread(self.entry_px["S"], self.entry_px["H"])
        sp_out = self.spread(px_s, px_h)
        self.j.trade({
            "trade_no": self.trade_no, "direction": "LONG_SPREAD" if d > 0 else "SHORT_SPREAD",
            "qty_s": cq["S"], "qty_h": cq["H"],
            "entry_time": self.entry_time.isoformat() if self.entry_time else "",
            "entry_s": self.entry_px["S"], "entry_h": self.entry_px["H"], "entry_z": self.entry_z,
            "exit_time": ts.isoformat(), "exit_s": px_s, "exit_h": px_h, "exit_reason": intent.purpose,
            "spread_bp": round(d * (sp_out - sp_in) * 1e4, 2),
            "pnl_s": round(pnl_s), "pnl_h": round(pnl_h), "fee": round(fee), "net_pl": round(net),
            "held_bars": self.held_bars,
            "held_sec": round((ts - self.entry_time).total_seconds()) if self.entry_time else "",
            "daily_pnl_after": round(self.daily_pnl),
            "part": "partial" if partial else "final", "units_max": self._units_max(),
            "pos_net": "" if partial else round(pos_net),
            "mae": round(self.worst_mtm), "mfe": round(self.best_mtm),
        })
        if partial:
            self.pos_realized = pos_net
            for k in ("S", "H"):
                self.open_qty[k] -= cq[k]
            self.units = max(1, self.units - max(1, round(cq["S"] / self.p.qty_s)) if self.p.qty_s else 1)
            self.partial_done = True
            self.j.event("position", f"분할 청산 뒤 {self.units}단위 남음 — 삼성 {self.open_qty['S']} · "
                                     f"하이닉스 {self.open_qty['H']} (확정 {pos_net:,.0f}원)")
            self._check_daily()
            return
        if intent.purpose in STOPS:
            self.cooldown = self.p.cooldown_bars_after_stop
        self.consec_losses = self.consec_losses + 1 if pos_net < 0 else 0
        if (self.p.max_consec_losses and not self.p.risk_off and self.consec_losses >= self.p.max_consec_losses
                and not self.halted):
            self.halted = f"연속 손실 {self.consec_losses}회"
            self.j.event("risk", f"포지션 단위 연속 손실 {self.consec_losses}회 — 오늘 신규 진입 중지(청산 판정은 계속)")
        self.mark_flat()
        self._check_daily()

    peak_units = 0

    def _units_max(self) -> int:
        return max(self.peak_units, self.units, 1)

    def _check_daily(self) -> None:
        if self.p.risk_off:
            return
        if self.daily_pnl <= -self.p.daily_loss_limit and not self.halted:
            self.halted = "일일 손실 한도 도달"
            self.j.event("risk", f"일일 손익 {self.daily_pnl:,.0f}원 — 오늘 신규 진입 중지")

    def mark_flat(self) -> None:
        self.direction = 0
        self.open_qty = {"S": 0, "H": 0}
        self.entry_px = {"S": 0.0, "H": 0.0}
        self.entry_time = None
        self.held_bars = 0
        self.units = 0
        self.partial_done = False
        self.pos_realized = 0.0
        self.worst_mtm = self.best_mtm = self._last_mtm = 0.0

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
