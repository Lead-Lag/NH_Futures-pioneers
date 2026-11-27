"""다음 검증 ③ — 같은 가설을 다른 주식선물 페어로: 「1분 괴리는 되돌아오지만, 호가 한 칸이 되돌림보다 크면 진다」.

가설에서 나오는 예측: 호가 한 칸(가격 대비 bp)이 되돌림 폭보다 작은 페어에서는 결과가 달라야 한다.
그래서 페어마다 (1) 신호 자체의 되돌림 = 비용 0 포지션당 손익(bp) 과 (2) 왕복 비용 = 다리마다 1틱(bp) 를 나란히 봅니다.

같은 pair_engine.py · 같은 규칙(진입 ±2 · 청산 0.5 · 40봉 z) · 날마다 직전 20거래일 β(워크포워드) · 판정 봉 종가 체결.
페어마다 바뀌는 것: β, 계약 수(β × 명목금액이 맞게 자동), 금액 손절·하루 한도(삼성 페어 대비 명목금액 비율로 조정).

데이터: pair_analyze.py fetch 로 받은 폴더(bars_S.csv · bars_H.csv). 「S」= 첫 종목, 「H」= 둘째 종목.
  python pair_analyze.py find 현대차 기아 KB금융 신한지주
  python pair_analyze.py fetch --sym-s <현대차 12월물> --sym-h <기아 12월물> --days 60 --data pair_hyundai_kia
  python pair_crosscheck.py hist_nov+hist_dec pair_hyundai_kia pair_kb_shinhan ...
  (폴더를 + 로 이으면 한 페어의 여러 월물로 봄 — 날마다 더 활발한 월물을 고름)
결과: results/pair_crosscheck/ (report.txt · pairs.csv)
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

from pair_engine import tick_size
from pair_walkforward import bootstrap
from risk_analyze import R0, days_and_betas, run_day

HERE = Path(__file__).resolve().parent
REF_NOTIONAL_S = 10 * 10 * 200_000      # 삼성 페어 기준: 10계약 × 10주 × 약 20만원


def pick_qty(ps: float, ph: float, beta: float, mult: float = 10, max_q: int = 20) -> tuple[int, int, float]:
    """β × S 명목 ≈ H 명목 이 되는 가장 작은 계약 수 조합(오차 5% 안), 없으면 오차 최소."""
    best = None
    for qs in range(1, max_q + 1):
        for qh in range(1, max_q + 1):
            err = abs(beta * qs * ps * mult - qh * ph * mult) / (qh * ph * mult)
            key = (err > 0.05, qs + qh, err)
            if best is None or key < best[0]:
                best = (key, qs, qh, err)
    return best[1], best[2], best[3]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pairs", nargs="+", help="페어 폴더(여러 월물은 + 로 잇기)")
    ap.add_argument("--min-fresh", type=float, default=60.0)
    a = ap.parse_args()
    cfg = json.loads((HERE / "strategy_config.json").read_text(encoding="utf-8"))
    base = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in ("shadows", "monitor")}
    out = HERE / "results" / "pair_crosscheck"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for spec in a.pairs:
        folders = [HERE / x for x in spec.split("+")]
        meta = {}
        for f in folders:
            m = f / "meta.json"
            if m.exists():
                meta = json.loads(m.read_text(encoding="utf-8"))
                break
        names = {k: (meta.get("legs", {}).get(k, {}) or {}).get("sym_nm", k) for k in ("S", "H")} \
            if isinstance(meta.get("legs", {}).get("S"), dict) else {"S": "S", "H": "H"}
        days = days_and_betas(folders, True, float(base["beta"]), a.min_fresh)
        if len(days) < 3:
            print(f"{spec}: 쓸 수 있는 날 {len(days)}일 — 두 종목이 함께 체결된 분이 {a.min_fresh:.0f}% 넘는 날이 부족(거래가 얇음)")
            rows.append({"pair": spec, "days": len(days)})
            continue
        ps = statistics.median(b["close"] for d in days for b in d[3])
        ph = statistics.median(b["close"] for d in days for b in d[4])
        beta = statistics.median(d[2] for d in days)
        qs, qh, err = pick_qty(ps, ph, beta)
        scale = qs * ps * 10 / REF_NOTIONAL_S
        c = {**base, "qty_s": qs, "qty_h": qh, "pair_stop_won": round(base["pair_stop_won"] * scale, -3),
             "daily_loss_limit": round(base["daily_loss_limit"] * scale, -3)}
        notional = qs * ps * 10                      # S 다리 명목(원) — bp 환산 기준
        tick_bp = {"S": 1e4 * tick_size(ps, 0) / ps, "H": 1e4 * tick_size(ph, 0) / ph}
        res = {}
        for cname, conf in (("R0", {**c, **R0}), ("R2", c)):
            for sname, slip in (("0틱", {"S": 0, "H": 0}), ("1틱", {"S": 1, "H": 1})):
                dv, pos = [], []
                for d, src, b, bs, bh in days:
                    tr, _ = run_day(conf, b, bs, bh, slip)
                    dv.append(sum(t["net_pl"] for t in tr))
                    pos += [t["pos_net"] for t in tr if t.get("part") == "final"]
                lo, hi = bootstrap(dv)
                res[(cname, sname)] = {"n": len(pos), "day": statistics.mean(dv), "lo": lo, "hi": hi,
                                       "pos_bp": 1e4 * statistics.mean(pos) / notional if pos else 0.0}
        # 왕복 비용(다리마다 1틱, 진입·청산) — S 명목 대비 bp
        cost_bp = 2 * (qs * tick_size(ps, 0) * 10 + qh * tick_size(ph, 0) * 10) / notional * 1e4
        gross = res[("R0", "0틱")]["pos_bp"]
        rows.append({"pair": spec, "S": names["S"], "H": names["H"], "days": len(days),
                     "first": days[0][0], "last": days[-1][0], "px_s": round(ps), "px_h": round(ph),
                     "beta": round(beta, 2), "qty": f"{qs}:{qh}", "tick_bp_s": round(tick_bp["S"], 1),
                     "tick_bp_h": round(tick_bp["H"], 1), "gross_bp": round(gross, 1), "cost_bp": round(cost_bp, 1),
                     "ratio": round(gross / cost_bp, 2) if cost_bp else "",
                     **{f"{cn}_{sn}_day": round(v["day"]) for (cn, sn), v in res.items()},
                     **{f"{cn}_{sn}_n": v["n"] for (cn, sn), v in res.items()},
                     "R2_1틱_lo": round(res[("R2", "1틱")]["lo"]), "R2_1틱_hi": round(res[("R2", "1틱")]["hi"])})
        print(f"{spec}: {len(days)}일 · 되돌림 {gross:.1f}bp vs 비용 {cost_bp:.1f}bp")
    keys = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0].keys()).index(k) if k in rows[0] else 99)
    with (out / "pairs.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    L = ["# 같은 가설, 다른 페어 — 되돌림 폭 vs 호가 한 칸", "",
         "되돌림 = 위험관리 없음(R0)·비용 0 의 포지션당 손익 · 비용 = 다리마다 1틱 왕복. 둘 다 첫 종목 명목금액 대비 bp.", "",
         "| 페어 | 날 | 가격(중앙) | β · 계약 | 호가 한 칸 S · H | 되돌림 | 왕복 비용 | 되돌림 ÷ 비용 | R2 비용 0 하루 | R2 1틱 하루 (95%) |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        if "gross_bp" not in r:
            L.append(f"| {r['pair']} | {r['days']} | 거래가 얇아 검증 불가 | | | | | | | |")
            continue
        L.append(f"| {r['S']} – {r['H']} | {r['days']} | {r['px_s']:,} · {r['px_h']:,} | {r['beta']} · {r['qty']} | "
                 f"{r['tick_bp_s']} · {r['tick_bp_h']}bp | {r['gross_bp']}bp | {r['cost_bp']}bp | **{r['ratio']}** | "
                 f"{r['R2_0틱_day'] / 1e4:+.1f}만 | {r['R2_1틱_day'] / 1e4:+.1f}만 ({r['R2_1틱_lo'] / 1e4:+.1f} ~ {r['R2_1틱_hi'] / 1e4:+.1f}) |")
    L += ["", "되돌림 ÷ 비용 > 1 이면 「비용 0 에서 생기는 이익이 시장가 왕복 비용보다 크다」. 단, 비용 0 이익에는 호가 튕김이 섞여 있음(6절)."]
    (out / "report.txt").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
