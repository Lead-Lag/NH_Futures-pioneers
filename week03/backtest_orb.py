"""현물 개장 범위 돌파(Opening Range Breakout) 백테스트.

    python backtest_orb.py bars_SFC_SFCX26.csv --product SFC
    python backtest_orb.py bars_MHI_MHIX26.csv --product MHI --grid

봉 시각은 한국시간(check_overseas.py 로 받은 CSV, exch_tm = 봉 시작 시각).
체결 가정: 신호 봉 마감 → 다음 봉 시가 진입, 불리하게 slip 틱. 손절은 봉 고가·저가로 판정,
시가가 이미 넘었으면 시가. 같은 봉에서 손절·익절이 둘 다 닿으면 손절로 본다(보수적).
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

PRODUCTS = {
    # tick, tick 가치, 통화, 세션 시작 / 세션별 시간 청산(기초 현물이 쉬거나 닫기 5분 전)
    "SFC": dict(tick=1.0, tick_value=1.0, currency="USD", session_start="100000",
                exit={"am": "122500", "pm": "155500"}),   # 본토: 점심 12:30, 마감 16:00 KST
    "MHI": dict(tick=1.0, tick_value=10.0, currency="HKD", session_start="101500",
                exit={"am": "125500", "pm": "165500"}),   # 홍콩: 점심 13:00, 마감 17:00 KST
}
# 세션: 기준 범위 시작(현물 개장·재개 시각), 마지막 신호 봉
SESSIONS = {"am": dict(open_t="103000", last_entry="112900"),
            "pm": dict(open_t="140000", last_entry="145900")}


@dataclass
class Params:
    k: float = 2.0            # 손절 = ATR × k
    m: float | None = 1.5     # 익절 = 손절폭 × m (None = 익절 없음)
    range_min: int = 15       # 기준 범위 길이(분), 세션 개장부터
    session: str = "am"       # am = 10:30 현물 개장, pm = 14:00 점심 뒤 재개
    slip: int = 1             # 진입·청산 때 불리한 틱
    min_range_ticks: int = 8  # 범위 폭 < 왕복 비용(2틱) × 4 이면 거래 안 함
    atr_n: int = 14
    min_stop_ticks: int = 2


def load(path: str) -> pd.DataFrame:
    d = pd.read_csv(path, dtype={"ref_dt": str, "exch_tm": str, "glb_biz_dt": str})
    d["t"] = d["exch_tm"].str.zfill(6)
    d["day"] = d["ref_dt"]
    return d.sort_values(["day", "t"]).reset_index(drop=True)


def liquid_days(d: pd.DataFrame, min_bars: int = 110, min_vol: int = 0) -> list[str]:
    """10:30~12:30 봉이 거의 다 있고 거래량도 충분한 날만 (근월물 전환 전 한산한 날 제외)."""
    w = d[(d.t >= "103000") & (d.t < "123000")]
    g = w.groupby("day").agg(n=("t", "size"), vol=("volume", "sum"))
    return sorted(g[(g.n >= min_bars) & (g.vol >= min_vol)].index)


def add_time(t: str, minutes: int) -> str:
    h, mi = int(t[:2]), int(t[2:4])
    tot = h * 60 + mi + minutes
    return f"{tot // 60:02d}{tot % 60:02d}00"


def run_day(day: pd.DataFrame, prd: dict, p: Params) -> dict | None:
    tick = prd["tick"]
    day = day[day.t >= prd["session_start"]].reset_index(drop=True)
    ses = SESSIONS[p.session]
    r_end = add_time(ses["open_t"], p.range_min)
    rng = day[(day.t >= ses["open_t"]) & (day.t < r_end)]
    if len(rng) < p.range_min - 1:
        return None
    hi, lo = rng.high.max(), rng.low.min()
    width = round((hi - lo) / tick)
    base = dict(day=day.day.iloc[0], session=p.session, range_hi=hi, range_lo=lo, range_ticks=width)
    if width < p.min_range_ticks:
        return dict(base, side="", reason="range_narrow")
    # ATR (그날 봉만)
    prev = day.close.shift(1)
    tr = pd.concat([day.high - day.low, (day.high - prev).abs(), (day.low - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(p.atr_n, min_periods=p.atr_n).mean()
    exit_t = prd["exit"][p.session]
    idx = day.index[(day.t >= r_end) & (day.t <= ses["last_entry"])]
    for i in idx:
        c = day.close[i]
        side = 1 if c > hi else (-1 if c < lo else 0)
        if side == 0 or i + 1 >= len(day) or pd.isna(atr[i]):
            continue
        e = day.iloc[i + 1]
        entry = e.open + side * p.slip * tick
        stop_ticks = max(p.min_stop_ticks, math.ceil(p.k * atr[i] / tick))
        stop = entry - side * stop_ticks * tick
        target = entry + side * p.m * stop_ticks * tick if p.m else None
        exit_px, reason, exit_t_hit = None, "", ""
        for j in range(i + 1, len(day)):
            b = day.iloc[j]
            if b.t >= exit_t:
                exit_px, reason = b.open - side * p.slip * tick, "time"
            elif (side == 1 and b.low <= stop) or (side == -1 and b.high >= stop):
                gap = (side == 1 and b.open < stop) or (side == -1 and b.open > stop)
                exit_px, reason = (b.open if gap else stop) - side * p.slip * tick, "stop"
            elif target is not None and ((side == 1 and b.high >= target) or (side == -1 and b.low <= target)):
                exit_px, reason = target, "target"   # 지정가 익절: 미끄러짐 없음
            if exit_px is not None:
                exit_t_hit = b.t
                break
        if exit_px is None:
            b = day.iloc[-1]
            exit_px, reason, exit_t_hit = b.close - side * p.slip * tick, "last", b.t
        ticks = round(side * (exit_px - entry) / tick)
        return dict(base, side="long" if side == 1 else "short", signal_t=day.t[i], entry_t=e.t,
                    entry=entry, stop_ticks=stop_ticks, exit_t=exit_t_hit, exit=exit_px,
                    reason=reason, ticks=ticks, pnl=ticks * prd["tick_value"])
    return dict(base, side="", reason="no_breakout")


def backtest(d: pd.DataFrame, product: str, p: Params, days: list[str]) -> pd.DataFrame:
    prd = PRODUCTS[product]
    rows = [run_day(d[d.day == x], prd, p) for x in days]
    return pd.DataFrame([r for r in rows if r])


def summarize(t: pd.DataFrame) -> dict:
    tr = t[t.side != ""] if len(t) else t
    if len(tr) == 0:
        return dict(days=len(t), trades=0)
    cum = tr.pnl.cumsum()
    best = tr.pnl.max()
    return dict(days=len(t), trades=len(tr), win_rate=round((tr.ticks > 0).mean(), 2),
                avg_ticks=round(tr.ticks.mean(), 1), total_ticks=int(tr.ticks.sum()),
                total_pnl=round(tr.pnl.sum(), 1), max_dd=round((cum.cummax() - cum).max(), 1),
                total_ex_best_day=round(tr.pnl.sum() - best, 1),
                reasons=tr.reason.value_counts().to_dict())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--product", required=True, choices=list(PRODUCTS))
    ap.add_argument("--grid", action="store_true", help="손절 k × 익절 m 격자")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    d = load(a.csv)
    days = liquid_days(d)
    print(f"{a.product}: 유동성 충분한 날 {len(days)}일 {days[0] if days else ''}~{days[-1] if days else ''}")
    out = Path(a.out) / f"backtest_orb_{a.product}"
    out.mkdir(parents=True, exist_ok=True)
    for ses in ("am", "pm"):
        for slip in (0, 1):
            t = backtest(d, a.product, Params(slip=slip, session=ses), days)
            t.to_csv(out / f"trades_{ses}_slip{slip}.csv", index=False)
            print(f"  [{ses}] 기준 설정(k=2, m=1.5, 범위 15분) 미끄러짐 {slip}틱: {summarize(t)}")
    if a.grid:
        rows = []
        for k in (1.5, 2.0, 3.0):
            for m in (1.0, 1.5, 2.0, None):
                for rm in (15, 30):
                    s = summarize(backtest(d, a.product, Params(k=k, m=m, range_min=rm, slip=1), days))
                    rows.append(dict(k=k, m=m or "none", range_min=rm, **{x: s.get(x) for x in
                                     ("trades", "win_rate", "avg_ticks", "total_ticks", "max_dd")}))
        g = pd.DataFrame(rows)
        g.to_csv(out / "grid_slip1.csv", index=False)
        print(g.to_string(index=False))


if __name__ == "__main__":
    main()
