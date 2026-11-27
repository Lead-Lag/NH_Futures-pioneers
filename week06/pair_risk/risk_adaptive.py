"""8절 개선 후보 두 가지 검증 — ③ 되돌림 속도에 맞춘 시간 청산 · ④ 「관계 붕괴」 때만 자르는 손절.

③ 시간 청산 = clamp(m × 반감기, 10, 60) 봉.
   반감기: 그날 이전 5거래일의 1분봉 스프레드에서, 40봉 이동평균과의 괴리 d 가 다음 봉에 얼마나 남는지(d_t+1 = φ d_t)
   를 재고 반감기 = ln 0.5 / ln φ. 그날 정보는 쓰지 않음.
   m = 2 (결과를 보기 전에 정함): 반감기 2배면 괴리의 75% 가 돌아올 시간 — 그때까지 안 돌아오면 「이번엔 안 돌아오는」 쪽으로 봄.
④ 관계 붕괴 = 보유 중 최근 30봉 1분 수익률 상관이 c 보다 낮아짐.
   c = 설계 기간 모든 봉의 30봉 상관 분포에서 하위 5% (결과를 보기 전에 정함): 「평소의 하위 5% 면 관계가 깨졌다」.
   두 가지로 시험: R2 에 더하기(+붕괴) · 금액 손절 15만원을 붕괴 손절로 바꾸고 큰 사고용 30만원만 남기기(붕괴로 교체).

설계 기간에서는 「가설의 근거」(R0 포지션에서 그 순간 자르는 게 나았는가)만 보고, 비교는 검증 기간에서.

  python risk_adaptive.py --data kis_spot --split 20260531      # 1년 현물: 설계 12~5월 · 검증 6~11월
  python risk_adaptive.py --data hist_nov hist_dec --m 2 --c <값>  # (참고) 선물 24거래일에 같은 값 그대로
결과: results/adaptive_<자료>/report.txt
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import timedelta
from pathlib import Path

import numpy as np

from pair_engine import PairEngine, PairParams, align_minutes
from pair_walkforward import bootstrap
from risk_analyze import COSTS, MAIN_COST, R0, _Fill, _J, days_and_betas, exit_cost, pct, run_day

HERE = Path(__file__).resolve().parent
M_MAIN, M_GRID = 2.0, (1.0, 1.5, 2.0, 3.0, 4.0)
HOLD_MIN, HOLD_MAX = 10, 60
CORR_BARS = 30
CAT_STOP = 300_000


def closes(bs, bh):
    s = {b["start"]: b["close"] for b in bs}
    h = {b["start"]: b["close"] for b in bh}
    ts = sorted(set(s) & set(h))
    return np.log([s[t] for t in ts]), np.log([h[t] for t in ts])


def half_life(prev_days, beta, window=40) -> float:
    xs, ys = [], []
    for bs, bh in prev_days:
        ls, lh = closes(bs, bh)
        sp = ls - beta * lh
        if len(sp) <= window + 2:
            continue
        mu = np.convolve(sp, np.ones(window) / window, mode="valid")      # mu[k] = 평균(sp[k..k+w-1])
        d = sp[window - 1:] - mu
        xs.append(d[:-1]); ys.append(d[1:])
    if not xs:
        return float("nan")
    x, y = np.concatenate(xs), np.concatenate(ys)
    phi = float((x @ y) / (x @ x))
    return math.log(0.5) / math.log(phi) if 0 < phi < 1 else float("inf")


def rolling_corrs(bs, bh, n=CORR_BARS):
    ls, lh = closes(bs, bh)
    rs, rh = np.diff(ls), np.diff(lh)
    out = []
    for i in range(n, len(rs) + 1):
        a, b = rs[i - n:i], rh[i - n:i]
        if a.std() > 0 and b.std() > 0:
            out.append(float(np.corrcoef(a, b)[0, 1]))
    return out


def r0_paths(base, beta, bs, bh, slip):
    """R0 로 하루를 돌리며 포지션마다 봉별 평가손익 · 30봉 상관을 모음."""
    p = PairParams.from_dict({**base, **R0, "beta": beta})
    j = _J()
    eng = PairEngine(p, j)
    eng.warmup = False
    ticks = {"S": p.tick_s, "H": p.tick_h}
    last: dict[str, float] = {}
    upto = max(bs[-1]["start"], bh[-1]["start"]) + timedelta(minutes=1)
    cur, out, n_tr = None, [], 0
    for pb in align_minutes(bs, bh, None, upto, last):
        ts = pb["start"] + timedelta(minutes=1)
        it = eng.on_bar_close(pb)
        if cur is not None and eng.direction:
            cur["path"].append(eng.mtm(pb["close_s"], pb["close_h"]))
            cur["corr"].append(eng.recent_corr(CORR_BARS))
        if it is not None:
            eng.on_pair_done(it, _Fill(it, {"S": pb["close_s"], "H": pb["close_h"]}, ticks, slip), ts)
        if eng.direction and cur is None:
            cur = {"path": [], "corr": [], "px_s": eng.entry_px["S"], "px_h": eng.entry_px["H"],
                   "q_s": eng.open_qty["S"], "q_h": eng.open_qty["H"]}
        if len(j.trades) > n_tr:
            n_tr = len(j.trades)
            t = j.trades[-1]
            if t.get("part") == "final" and cur is not None:
                cur["net"] = t["pos_net"]; out.append(cur); cur = None
    if eng.direction and cur is not None:
        it = eng.force_exit(upto, "그날 마지막 봉")
        eng.on_pair_done(it, _Fill(it, dict(last), ticks, slip), upto)
        cur["net"] = j.trades[-1]["pos_net"]; out.append(cur)
    return out


def cut_vs_hold(paths, first_idx):
    """first_idx(x) = 자를 봉 번호(없으면 None). (자른 포지션 수, 그중 끝까지 들면 이겼을 수, 자름−들고 감 포지션당 평균)."""
    rows = []
    for x in paths:
        i = first_idx(x)
        if i is not None:
            rows.append((x["path"][i] - exit_cost(x)) - x["net"])
    wins = sum(1 for x in paths if first_idx(x) is not None and x["net"] > 0)
    return len(rows), wins, (statistics.mean(rows) if rows else float("nan"))


def evaluate(conf_of_day, days, slip):
    dv, pos, reasons = [], [], {}
    for d, src, b, bs, bh, hl in days:
        tr, _ = run_day(conf_of_day(hl), b, bs, bh, slip)
        dv.append(sum(t["net_pl"] for t in tr))
        for t in tr:
            if t.get("part") == "final":
                pos.append(t["pos_net"])
                reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    lo, hi = bootstrap(dv)
    return {"n": len(pos), "day": statistics.mean(dv), "lo": lo, "hi": hi, "worst_day": min(dv),
            "worst_pos": min(pos) if pos else 0, "reasons": reasons}


def hold_of(m, hl):
    if not math.isfinite(hl):
        return HOLD_MAX
    return int(min(HOLD_MAX, max(HOLD_MIN, round(m * hl))))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", nargs="+", default=["kis_spot"])
    ap.add_argument("--split", help="설계 기간 마지막 날 YYYYMMDD")
    ap.add_argument("--m", type=float, default=M_MAIN)
    ap.add_argument("--c", type=float, help="이미 정한 상관 기준(설계를 건너뜀)")
    a = ap.parse_args()
    cfg = json.loads((HERE / "strategy_config.json").read_text(encoding="utf-8"))
    base = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in ("shadows", "monitor")}
    raw = days_and_betas([HERE / x for x in a.data], True, float(base["beta"]), 60)
    days = []
    for i, (d, src, b, bs, bh) in enumerate(raw):
        if i < 5:
            continue
        hl = half_life([(x[3], x[4]) for x in raw[i - 5:i]], b)
        days.append((d, src, b, bs, bh, hl))
    name = "_".join(a.data)
    out = HERE / "results" / f"adaptive_{name}"
    out.mkdir(parents=True, exist_ok=True)
    design = [x for x in days if a.split and x[0] <= a.split]
    valid = [x for x in days if not a.split or x[0] > a.split]
    hls = [x[5] for x in days if math.isfinite(x[5])]
    L = [f"# 개선 후보 ③ 적응형 시간 청산 · ④ 관계 붕괴 손절 — {name} ({len(days)}거래일 {days[0][0]}~{days[-1][0]})", "",
         f"반감기(직전 5거래일, 봉): 중앙 {statistics.median(hls):.1f} · 10% {pct(sorted(hls), .1):.1f} · 90% {pct(sorted(hls), .9):.1f}"
         f" → 시간 청산 m={a.m:g} 이면 중앙 {hold_of(a.m, statistics.median(hls))}봉 (범위 {HOLD_MIN}~{HOLD_MAX})", ""]

    c = a.c
    slip = COSTS[MAIN_COST]
    if a.split:
        allc = sorted(v for x in design for v in rolling_corrs(x[3], x[4]))
        c = round(pct(allc, 0.05), 2)
        L += [f"## 1. 설계 기간 {design[0][0]}~{design[-1][0]} ({len(design)}일) — 기준 정하기와 가설의 근거", "",
              f"30봉 1분 수익률 상관 분포: 5% {pct(allc, .05):.2f} · 25% {pct(allc, .25):.2f} · 중앙 {pct(allc, .5):.2f} · 75% {pct(allc, .75):.2f}"
              f" → **c = {c}** (하위 5%)", ""]
        paths = []
        for d, src, b, bs, bh, hl in design:
            for x in r0_paths(base, b, bs, bh, slip):
                x["hl"] = hl
                paths.append(x)
        touch = lambda x: next((i for i, v in enumerate(x["path"]) if v <= -150_000), None)  # noqa: E731
        brk = lambda x: next((i for i, v in enumerate(x["corr"]) if v is not None and v < c), None)  # noqa: E731
        L += [f"R0(위험관리 없음) 포지션 {len(paths)}개 · 실측 비용 · 「그 순간 잘랐다면 − 끝까지 들고 갔다면」 포지션당", "",
              "| 그 순간 | 해당 포지션 | 그중 끝까지 들면 이김 | 자르면 포지션당 |", "| --- | --- | --- | --- |"]
        def row(label, f):
            n, w, m = cut_vs_hold(paths, f)
            L.append(f"| {label} | {n} | {w} | {m / 1e4:+.1f}만 |")
        row("15만원 손실에 닿음 (지금 규칙)", touch)
        row("· 그때 상관 < c (관계 깨짐)", lambda x: (i if (i := touch(x)) is not None and x["corr"][i] is not None and x["corr"][i] < c else None))
        row("· 그때 상관 ≥ c (관계 유지)", lambda x: (i if (i := touch(x)) is not None and (x["corr"][i] is None or x["corr"][i] >= c) else None))
        row(f"상관이 처음 c 아래로 (④ 규칙)", brk)
        row("30봉 보유 (지금 규칙)", lambda x: 30 if len(x["path"]) > 30 else None)
        for m in M_GRID:
            row(f"m={m:g} × 반감기 보유 (③ 규칙)", lambda x, m=m: (k if len(x["path"]) > (k := hold_of(m, x["hl"])) else None))
        L.append("")
    if c is None:
        raise SystemExit("--split 또는 --c 가 필요합니다")

    R2 = lambda hl: base  # noqa: E731
    main_confs = [
        ("R2 (고정 30봉 · 15만원)", R2),
        (f"R2h 시간 청산 {a.m:g}×반감기", lambda hl: {**base, "max_hold_bars": hold_of(a.m, hl)}),
        (f"R2+붕괴 (상관 < {c})", lambda hl: {**base, "break_corr": c, "break_bars": CORR_BARS}),
        (f"R2붕괴교체 (붕괴 손절 + 큰 사고 {CAT_STOP // 10000}만원)", lambda hl: {**base, "break_corr": c, "break_bars": CORR_BARS, "pair_stop_won": CAT_STOP}),
        ("R2h+붕괴", lambda hl: {**base, "max_hold_bars": hold_of(a.m, hl), "break_corr": c, "break_bars": CORR_BARS}),
    ]
    sets = [("검증", valid)] + ([("설계", design)] if design else [])
    for k, (label, ds) in enumerate(sets):
        if not ds:
            continue
        L += [f"## {k + 2}. {label} 기간 {ds[0][0]}~{ds[-1][0]} ({len(ds)}일)", "",
              "| 규칙 | 비용 | 포지션 | 하루 평균 (95%) | 최악의 날 | 포지션 최악 | 청산 이유 |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for cn, sl in (("0틱", COSTS["0틱"]), ("실측", COSTS[MAIN_COST])):
            for nm, f in main_confs:
                r = evaluate(f, ds, sl)
                rs = " · ".join(f"{kk.replace('EXIT_', '').lower()} {v}" for kk, v in sorted(r["reasons"].items(), key=lambda t: -t[1]))
                L.append(f"| {nm} | {cn} | {r['n']} | {r['day'] / 1e4:+.1f}만 ({r['lo'] / 1e4:+.1f} ~ {r['hi'] / 1e4:+.1f}) | "
                         f"{r['worst_day'] / 1e4:+.0f}만 | {r['worst_pos'] / 1e4:+.0f}만 | {rs} |")
        if label == "검증":
            L += ["", "값 민감도 (실측, 하루 평균 · 최악의 날) — 고르는 데 쓰지 않음", ""]
            for m in M_GRID:
                r = evaluate(lambda hl, m=m: {**base, "max_hold_bars": hold_of(m, hl)}, ds, COSTS[MAIN_COST])
                L.append(f"- R2h m={m:g}: {r['day'] / 1e4:+.1f}만 · {r['worst_day'] / 1e4:+.0f}만")
            for cc in sorted({0.0, 0.2, 0.3, 0.4, c}):
                r = evaluate(lambda hl, cc=cc: {**base, "break_corr": cc, "break_bars": CORR_BARS}, ds, COSTS[MAIN_COST])
                L.append(f"- R2+붕괴 c={cc:g}: {r['day'] / 1e4:+.1f}만 · {r['worst_day'] / 1e4:+.0f}만")
        L.append("")
    (out / "report.txt").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
