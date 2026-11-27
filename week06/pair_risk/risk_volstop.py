"""변동성 비례 손절선 — k 를 데이터로 정하고(설계 기간), 고정 15만원과 비교(검증 기간).

손절선 = max(바닥, k × 진입 때 스프레드 σ × 삼성 다리 명목금액). 스프레드 손익 ≈ 명목금액 × 스프레드 변화이므로
「진입 때 평소 흔들림(σ)의 k 배만큼 밀리면 자른다」. z 손절(봉 마감)을 0.2초마다 판정하는 금액판에 가깝습니다.

k 를 정하는 원칙(결과를 보기 전에 정함): 설계 기간에서 위험관리 없음(R0)으로 이긴 포지션이 중간에 밀린 폭(MAE)을
σ × 명목금액 단위로 재고, 그 95% 지점을 올림한 값 — 이길 포지션은 거의 자르지 않는 가장 좁은 선.
바닥: 5만원(삼성 10계약 × 호가 500원 한 칸 — 호가 튕김 하나로는 잘리지 않게).

  python risk_volstop.py --data kis_spot --split 20260531      # 1년 현물: 설계 12~5월 · 검증 6~11월
  python risk_volstop.py --data hist_nov hist_dec               # (참고) 선물 24거래일 전체에 그대로 적용
결과: results/volstop_<자료>/report.txt
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

from pair_walkforward import bootstrap
from risk_analyze import COSTS, MAIN_COST, R0, days_and_betas, exit_cost, pct, run_day

HERE = Path(__file__).resolve().parent
FLOOR = 50_000
GRID = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0)


def evaluate(conf, days, slip):
    dv, pos = [], []
    for d, src, b, bs, bh in days:
        tr, _ = run_day(conf, b, bs, bh, slip)
        dv.append(sum(t["net_pl"] for t in tr))
        pos += [t["pos_net"] for t in tr if t.get("part") == "final"]
    lo, hi = bootstrap(dv)
    return {"n": len(pos), "day": statistics.mean(dv), "lo": lo, "hi": hi, "worst_day": min(dv),
            "worst_pos": min(pos) if pos else 0, "stops": None}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", nargs="+", default=["kis_spot"])
    ap.add_argument("--split", help="설계 기간 마지막 날 YYYYMMDD (없으면 k 를 정하지 않고 --k 로 받음)")
    ap.add_argument("--k", type=float, help="이미 정한 k (설계를 건너뜀)")
    a = ap.parse_args()
    cfg = json.loads((HERE / "strategy_config.json").read_text(encoding="utf-8"))
    base = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in ("shadows", "monitor")}
    days = days_and_betas([HERE / x for x in a.data], True, float(base["beta"]), 60)
    name = "_".join(a.data)
    out = HERE / "results" / f"volstop_{name}"
    out.mkdir(parents=True, exist_ok=True)
    L = [f"# 변동성 비례 손절선 — {name} ({len(days)}거래일 {days[0][0]}~{days[-1][0]})", ""]
    design = [d for d in days if a.split and d[0] <= a.split]
    valid = [d for d in days if not a.split or d[0] > a.split]

    k = a.k
    if a.split:
        paths = []
        for d, src, b, bs, bh in design:
            pp = []
            run_day({**base, **R0}, b, bs, bh, COSTS[MAIN_COST], pp)
            paths += pp
        unit = lambda x: x["sd"] * x["notional"]  # noqa: E731
        ok = [x for x in paths if x.get("sd") and x.get("notional")]
        wins = [x for x in ok if x["net"] > 0]
        mae_w = sorted(-min([0] + x["path"]) / unit(x) for x in wins)
        q95 = pct(mae_w, 0.95)
        k = math.ceil(q95)
        L += [f"## 1. k 정하기 — 설계 기간 {design[0][0]}~{design[-1][0]} ({len(design)}일), R0 포지션 {len(ok)}개 · 이긴 포지션 {len(wins)}개", "",
              "이긴 포지션이 중간에 밀린 폭(MAE) ÷ (진입 때 σ × 명목금액): "
              + " · ".join(f"{int(q * 100)}% {pct(mae_w, q):.2f}σ" for q in (.5, .75, .9, .95, .99)), "",
              f"→ 원칙대로 95% 지점({q95:.2f}σ)을 올림해 **k = {k}**. 바닥 {FLOOR:,}원.", "",
              "참고 — 손절선 kσ 에 닿은 포지션: 끝까지 들고 감 vs 그 자리 청산 (설계 기간, 실측 비용)", "",
              "| k | 닿은 포지션 | 그중 이김 | 자르면 포지션당 |", "| --- | --- | --- | --- |"]
        for kk in GRID:
            hit = []
            for x in ok:
                thr = max(FLOOR, kk * unit(x))
                idx = next((i for i, m in enumerate(x["path"]) if m <= -thr), None)
                if idx is not None:
                    hit.append((x, x["path"][idx] - exit_cost(x)))
            if hit:
                L.append(f"| {kk:g} | {len(hit)} | {sum(1 for x, _ in hit if x['net'] > 0)} | "
                         f"{statistics.mean(c - x['net'] for x, c in hit) / 1e4:+.1f}만 |")
        L.append("")
    if k is None:
        raise SystemExit("--split 또는 --k 가 필요합니다")

    sets = [("검증", valid)] + ([("설계", design)] if design else [])
    for label, ds in sets:
        if not ds:
            continue
        L += [f"## {'2' if label == '검증' else '3'}. {label} 기간 {ds[0][0]}~{ds[-1][0]} ({len(ds)}일) — 고정 15만원 vs 변동성 손절", "",
              "| 규칙 | 비용 | 포지션 | 하루 평균 (95%) | 최악의 날 | 포지션 최악 |", "| --- | --- | --- | --- | --- | --- |"]
        confs = [("R2 고정 15만원", base)] + [(f"R2v k={kk:g}" + (" ← 원칙으로 정한 값" if kk == k else ""),
                                            {**base, "stop_sigma_k": kk, "stop_floor_won": FLOOR}) for kk in sorted(set(GRID) | {k})]
        for cn, sl in (("0틱", COSTS["0틱"]), ("실측", COSTS[MAIN_COST])):
            for nm, c in confs:
                r = evaluate(c, ds, sl)
                L.append(f"| {nm} | {cn} | {r['n']} | {r['day'] / 1e4:+.1f}만 ({r['lo'] / 1e4:+.1f} ~ {r['hi'] / 1e4:+.1f}) | "
                         f"{r['worst_day'] / 1e4:+.0f}만 | {r['worst_pos'] / 1e4:+.0f}만 |")
        L.append("")
    (out / "report.txt").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
