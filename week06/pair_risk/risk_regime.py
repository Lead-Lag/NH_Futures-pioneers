"""6주차 — 국면별 결과와 「흔들리는 값」의 안정성.

1) 국면: 날마다 미리 정한 기준으로 이름을 붙이고(결과를 보기 전에 정함), 국면별로 R0·R1·R2 하루 손익을 비교
   - 추세: 그날 시가 기준 직전 20거래일 삼성 선물 수익률의 부호 → 상승 / 하락
   - 변동성: 그날 스프레드 1분 수익률의 표준편차 → 전체 날의 3분위 저 / 중 / 고
2) 값 안정성: 20거래일 창을 하루씩 굴리며, 창마다 「위험관리 없음(R0) 포지션을 손절선 X 에서 잘랐을 때 − 끝까지 들고 갔을 때」
   를 X 마다 계산 → 자르는 쪽이 나아지는 가장 작은 X(손익분기 손절선)와 시간 청산 N 의 같은 값이 창마다 어떻게 움직이는지

  python risk_regime.py --data hist_nov hist_dec --walk          # 지금 24거래일
  python risk_regime.py --data bbg_2026* --walk                  # 블룸버그 분봉(bbg_import.py 로 변환한 폴더)
결과: results/regime_<N>d/ (regime_report.txt · days.csv · windows.csv)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path

from risk_analyze import COSTS, MAIN_COST, R0, days_and_betas, exit_cost, f0, run_day

HERE = Path(__file__).resolve().parent
STOPS = (50_000, 75_000, 100_000, 125_000, 150_000, 200_000, 250_000, 300_000)
HOLDS = (10, 15, 20, 30, 45, 60)


def spread_vol(bs, bh, beta) -> float:
    s = {b["start"]: b["close"] for b in bs}
    h = {b["start"]: b["close"] for b in bh}
    ts = sorted(set(s) & set(h))
    sp = [math.log(s[t]) - beta * math.log(h[t]) for t in ts]
    r = [b - a for a, b in zip(sp, sp[1:])]
    return statistics.pstdev(r) * 1e4 if len(r) > 10 else float("nan")


def breakeven(paths, values, kind):
    """창 안 포지션들로 X(또는 N)마다 「자름 − 들고 감」 포지션당 평균. 처음으로 ≥0 이 되는 값(없으면 None)."""
    out = {}
    for v in values:
        if kind == "stop":
            hit = [x for x in paths if min([0] + x["path"]) <= -v]
            if not hit:
                continue
            cut = [x["path"][next(i for i, m in enumerate(x["path"]) if m <= -v)] - exit_cost(x) for x in hit]
        else:
            hit = [x for x in paths if len(x["path"]) > v]
            if not hit:
                continue
            cut = [x["path"][v - 1] - exit_cost(x) for x in hit]
        out[v] = (len(hit), statistics.mean(c - x["net"] for c, x in zip(cut, hit)))
    be = next((v for v in values if v in out and out[v][1] >= 0), None)
    return be, out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", nargs="+", default=["hist_nov", "hist_dec"])
    ap.add_argument("--walk", action="store_true")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--min-fresh", type=float, default=60.0)
    a = ap.parse_args()
    cfg = json.loads((HERE / "strategy_config.json").read_text(encoding="utf-8"))
    base = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in ("shadows", "monitor")}
    week4 = {**base, **cfg["shadows"]["R1_4주차규칙"]}
    configs = {"R0": {**base, **R0}, "R1": week4, "R2": base}
    folders = [HERE / x if not Path(x).is_absolute() else Path(x) for x in a.data]
    days = days_and_betas(folders, a.walk, float(base["beta"]), a.min_fresh)
    # 추세 라벨용: 고른 날 전체(워크포워드로 빠진 앞쪽 날 포함)의 삼성 종가
    from pair_walkforward import pick_days
    chosen, _ = pick_days(folders, a.min_fresh)
    first_px = {d: bs[0]["close"] for d, _, bs, _, _ in chosen}
    last_px = {d: bs[-1]["close"] for d, _, bs, _, _ in chosen}
    order = [d for d, *_ in chosen]

    rows, paths_by_day = [], {}
    for d, src, beta, bs, bh in days:
        i = order.index(d)
        prev = order[max(0, i - 20):i]
        trend = (math.log(first_px[d] / last_px[prev[0]]) if prev else float("nan"))
        r = {"date": d, "trend20_pct": round(100 * trend, 2) if not math.isnan(trend) else "",
             "spread_vol_bp": round(spread_vol(bs, bh, beta), 2)}
        for cn, slip in (("0틱", COSTS["0틱"]), ("실측", COSTS[MAIN_COST])):
            for name, c in configs.items():
                tr, _ = run_day(c, beta, bs, bh, slip)
                r[f"{name}_{cn}"] = sum(t["net_pl"] for t in tr)
        pp: list[dict] = []
        run_day(configs["R0"], beta, bs, bh, COSTS[MAIN_COST], pp)
        paths_by_day[d] = pp
        rows.append(r)
    vols = sorted(r["spread_vol_bp"] for r in rows)
    q1, q2 = vols[len(vols) // 3], vols[2 * len(vols) // 3]
    for r in rows:
        v = r["spread_vol_bp"]
        r["vol_regime"] = "저" if v < q1 else ("중" if v < q2 else "고")
        r["trend_regime"] = "" if r["trend20_pct"] == "" else ("상승" if r["trend20_pct"] > 0 else "하락")

    out = HERE / "results" / f"regime_{len(rows)}d"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "days.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    L = [f"# 국면별 결과와 값 안정성 ({len(rows)}거래일 {rows[0]['date']}~{rows[-1]['date']})", "",
         f"국면 기준(결과를 보기 전에 정함): 추세 = 직전 20거래일 삼성 선물 수익률 부호 · 변동성 = 그날 스프레드 1분 수익률 표준편차 3분위"
         f"(경계 {q1:.1f} · {q2:.1f}bp)", "",
         "## 1. 국면별 하루 평균 손익 (만원)", "",
         "| 국면 | 날 수 | R0 0틱 | R1 0틱 | R2 0틱 | R0 실측 | R1 실측 | R2 실측 | R2−R1 실측 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]

    def line(label, sel):
        if not sel:
            return
        m = lambda k: statistics.mean(r[k] for r in sel) / 1e4  # noqa: E731
        L.append(f"| {label} | {len(sel)} | {m('R0_0틱'):+.1f} | {m('R1_0틱'):+.1f} | {m('R2_0틱'):+.1f} | "
                 f"{m('R0_실측'):+.1f} | {m('R1_실측'):+.1f} | {m('R2_실측'):+.1f} | {m('R2_실측') - m('R1_실측'):+.1f} |")

    line("전체", rows)
    for t in ("상승", "하락"):
        line(f"추세 {t}", [r for r in rows if r["trend_regime"] == t])
    for v in ("저", "중", "고"):
        line(f"변동성 {v}", [r for r in rows if r["vol_regime"] == v])
    L += ["", "(0틱 = 신호 자체의 우위, 실측 = 삼성 1틱·하이닉스 0.5틱. R2−R1 = 6주차 규칙이 4주차보다 아낀 금액)", ""]

    # 2) 굴리는 창
    W = a.window
    dates = [r["date"] for r in rows]
    wrows = []
    if len(dates) >= W:
        for k in range(W, len(dates) + 1):
            win = dates[k - W:k]
            pp = [x for d in win for x in paths_by_day[d]]
            be_s, det_s = breakeven(pp, STOPS, "stop")
            be_t, det_t = breakeven(pp, HOLDS, "time")
            wrows.append({"start": win[0], "end": win[-1], "positions": len(pp),
                          "breakeven_stop": be_s or "", "breakeven_hold": be_t or "",
                          **{f"stop_{v}": round(det_s[v][1]) if v in det_s else "" for v in STOPS},
                          **{f"hold_{v}": round(det_t[v][1]) if v in det_t else "" for v in HOLDS}})
        with (out / "windows.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(wrows[0].keys()))
            w.writeheader()
            w.writerows(wrows)
        bs_vals = [r["breakeven_stop"] for r in wrows if r["breakeven_stop"] != ""]
        bt_vals = [r["breakeven_hold"] for r in wrows if r["breakeven_hold"] != ""]
        L += [f"## 2. 값 안정성 — {W}거래일 창 {len(wrows)}개 (하루씩 이동, 비용 실측)", "",
              "손익분기 손절선 = 「그 선에서 자르는 쪽이 끝까지 들고 가는 쪽보다 나아지는」 가장 작은 X. 시간 청산도 같은 방식.", "",
              f"- 손익분기 손절선: 창 {len(bs_vals)}/{len(wrows)}개에서 존재 · 값 분포 "
              + (" · ".join(f"{v // 10000}만 {bs_vals.count(v)}개" for v in sorted(set(bs_vals))) if bs_vals else "없음"),
              f"- 손익분기 보유 봉: 창 {len(bt_vals)}/{len(wrows)}개에서 존재 · 값 분포 "
              + (" · ".join(f"{v}봉 {bt_vals.count(v)}개" for v in sorted(set(bt_vals))) if bt_vals else "없음"), "",
              "| 창 | 포지션 | 손익분기 손절선 | 15만에서 자르면(포지션당) | 손익분기 보유 | 30봉에서 자르면 |",
              "| --- | --- | --- | --- | --- | --- |"]
        step = max(1, len(wrows) // 12)
        for r in wrows[::step] + ([wrows[-1]] if (len(wrows) - 1) % step else []):
            L.append(f"| {r['start']}~{r['end']} | {r['positions']} | {f0(r['breakeven_stop']) if r['breakeven_stop'] != '' else '없음'} | "
                     f"{f0(r['stop_150000']) if r['stop_150000'] != '' else '—'} | {r['breakeven_hold'] or '없음'} | "
                     f"{f0(r['hold_30']) if r['hold_30'] != '' else '—'} |")
        L.append("")
    (out / "regime_report.txt").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
