"""6주차 「속도」 보완 점검 — 속도 진입 필터의 우위가 체결을 1분 늦춰도 남는가 (호가 튕김 가려내기, 4주차 3-8절과 같은 방법).

  python risk_speed_check.py                                   # data_dec · data_nov 9거래일, β 고정
  python risk_speed_check.py --data hist_nov hist_dec --walk   # 24거래일 워크포워드 β
결과: results/risk_<fixed|walk>_<N>d/speed_delay.txt
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pair_walkforward as W
from risk_analyze import days_and_betas

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", nargs="+", default=["data_dec", "data_nov"])
    ap.add_argument("--walk", action="store_true")
    a = ap.parse_args()
    cfg = json.loads((HERE / "strategy_config.json").read_text(encoding="utf-8"))
    base = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in ("shadows", "monitor")}
    days = days_and_betas([HERE / x for x in a.data], a.walk, float(base["beta"]), 60)
    C = {"R2 6주차": base,
         "+하이닉스 선행(3봉)": {**base, "speed_filter": "lead_h", "speed_bars": 3},
         "+삼성 선행(3봉)": {**base, "speed_filter": "lead_s", "speed_bars": 3},
         "+급변만(1봉 dz≥1.5)": {**base, "speed_filter": "fast", "speed_bars": 1, "jump_z": 1.5},
         "+완만만(1봉 dz<1.5)": {**base, "speed_filter": "slow", "speed_bars": 1, "jump_z": 1.5}}
    L = [f"# 속도 필터 — 체결 지연 점검 ({len(days)}거래일 {days[0][0]}~{days[-1][0]}, β {'워크포워드' if a.walk else '고정'})", "",
         "| 비용 | 체결 | " + " | ".join(C) + " |", "| --- | --- |" + " --- |" * len(C)]
    for cn, slip in (("0틱", {"S": 0, "H": 0}), ("실측(삼성1·하이닉스0.5)", {"S": 1, "H": 0.5})):
        for dl in (0, 1):
            cells = []
            for c in C.values():
                tot = npos = 0
                for d, src, beta, bs, bh in days:
                    tr, _ = W.run_day(c, beta, bs, bh, slip, delay=dl)
                    tot += sum(t["net_pl"] for t in tr)
                    npos += sum(1 for t in tr if t.get("part") == "final")
                cells.append(f"{npos}건 · 하루 {tot / len(days) / 1e4:+.1f}만 · 건당 {tot / max(npos, 1) / 1e4:+.1f}만")
            L.append(f"| {cn} | {'판정 봉 종가' if dl == 0 else '1분 뒤 종가'} | " + " | ".join(cells) + " |")
    out = HERE / "results" / f"risk_{'walk' if a.walk else 'fixed'}_{len(days)}d"
    out.mkdir(parents=True, exist_ok=True)
    (out / "speed_delay.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
