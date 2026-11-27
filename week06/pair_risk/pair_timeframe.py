"""개선 방향 검증 ① — 더 긴 시간 단위(N분봉)에서 되돌림 폭이 비용을 넘는가.

pair_walkforward.py 와 같은 데이터·같은 근월물 선택·같은 워크포워드 β(직전 20거래일 1분 수익률)·같은 pair_engine.py 규칙
(진입 ±2 · 청산 0.5 · 손절 3.5 / −15만 · 15:15 정리 · 하루 −50만)을 쓰고, 바꾸는 것은 봉 길이와 z 계산 창뿐입니다.

- N분봉: 1분봉을 09:00 부터 N분씩 묶어 마지막 종가를 씁니다. 그 N분 안에 한 번이라도 체결이 있으면 fresh.
  (봉의 start 를 묶음의 마지막 분으로 두어, 엔진이 보는 봉 마감 시각 = 묶음 끝)
- z 창(window)은 「봉 개수」라서 N이 커지면 줄여야 하루 안에 지표가 섭니다. 기본 조합은 1분×40(지금 규칙) · 3분×20 ·
  5분×12 · 5분×24 · 10분×12 · 15분×8 (창 길이 40~120분).
- 비용은 다리마다 0 · 0.5 · 1틱 · 실측(삼성 1 · 하이닉스 0.5틱), 체결은 판정 봉 종가 + 비용(지연 0).
- 핵심 지표: 비용 0 일 때 「거래당 평균 이익」 — 이것이 거래당 실측 비용(약 11만원)보다 커져야 시장가로도 남습니다.

    python pair_timeframe.py --data hist_nov hist_dec --out results/timeframe
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import timedelta
from pathlib import Path

from pair_engine import PairEngine, PairParams, align_minutes
from pair_walkforward import HERE, SESSION, _Fill, _J, bootstrap, day_returns, ols_beta, pick_days

COMBOS = [(1, 40), (3, 20), (5, 12), (5, 24), (10, 12), (15, 8)]
SCEN = {"0틱": {"S": 0, "H": 0}, "0.5틱": {"S": 0.5, "H": 0.5}, "1틱": {"S": 1, "H": 1}, "실측": {"S": 1, "H": 0.5}}


def bucket(rows: list[dict], n: int) -> list[dict]:
    """1분 격자(align_minutes 결과)를 N분봉으로."""
    if n == 1:
        return rows
    out, cur, key = [], [], None
    for r in rows:
        t = r["start"]
        k = ((t.hour * 60 + t.minute) - 9 * 60) // n
        if key is not None and k != key and cur:
            out.append(_close(cur))
            cur = []
        key = k
        cur.append(r)
    if cur:
        out.append(_close(cur))
    return out


def _close(cur: list[dict]) -> dict:
    last = cur[-1]
    return {"start": last["start"], "close_s": last["close_s"], "close_h": last["close_h"],
            "fresh_s": any(x["fresh_s"] for x in cur), "fresh_h": any(x["fresh_h"] for x in cur)}


def run_day_n(cfg: dict, beta: float, bs, bh, slip: dict, n: int, window: int):
    p = PairParams.from_dict({**cfg, "beta": beta, "window": window})
    j = _J()
    eng = PairEngine(p, j)
    eng.warmup = False
    ticks = {"S": p.tick_s, "H": p.tick_h}
    last: dict[str, float] = {}
    upto = max(bs[-1]["start"], bh[-1]["start"]) + timedelta(minutes=1)
    rows = [r for r in align_minutes(bs, bh, None, upto, last)
            if SESSION[0] <= r["start"].strftime("%H%M%S") <= SESSION[1]]
    for pb in bucket(rows, n):
        ts = pb["start"] + timedelta(minutes=1)
        it = eng.on_bar_close(pb)
        if it is None and eng.direction and not eng.pending:
            it = eng.on_prices(pb["close_s"], pb["close_h"], ts)
        if it is not None:
            eng.on_pair_done(it, _Fill(it, {"S": pb["close_s"], "H": pb["close_h"]}, ticks, slip), ts)
    if eng.direction:
        it = eng.force_exit(upto, "그날 마지막 봉")
        eng.on_pair_done(it, _Fill(it, dict(last), ticks, slip), upto)
    return j.trades


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", nargs="+", required=True)
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--min-fresh", type=float, default=60.0)
    ap.add_argument("--beta-days", type=int, default=20)
    ap.add_argument("--min-hist", type=int, default=5)
    ap.add_argument("--out", type=Path, default=HERE / "results" / "timeframe")
    a = ap.parse_args()
    cfg = json.loads(Path(a.strategy).read_text(encoding="utf-8"))
    chosen, _ = pick_days([Path(x) for x in a.data], a.min_fresh)
    rets = {d: day_returns(bs, bh) for d, _, bs, bh, _ in chosen}

    res: dict[tuple, dict[str, list]] = {}
    tr_rows = []
    days_used = []
    for i, (d, src, bs, bh, _) in enumerate(chosen):
        hist = [rets[x[0]] for x in chosen[max(0, i - a.beta_days):i]]
        if len(hist) < a.min_hist:
            continue
        beta = ols_beta([x for h in hist for x in h[0]], [y for h in hist for y in h[1]])
        if math.isnan(beta):
            continue
        days_used.append(d)
        for n, w in COMBOS:
            for name, slip in SCEN.items():
                tr = run_day_n(cfg, beta, bs, bh, slip, n, w)
                r = res.setdefault((n, w), {}).setdefault(name, [])
                r.append((d, sum(t["net_pl"] for t in tr), len(tr), [t["net_pl"] for t in tr]))
                if name == "0틱":
                    for t in tr:
                        tr_rows.append({"bar_min": n, "window": w, "date": d, **t})

    a.out.mkdir(parents=True, exist_ok=True)
    if tr_rows:
        with (a.out / "trades_cost0.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(tr_rows[0].keys()))
            wr.writeheader()
            wr.writerows(tr_rows)
    L = [f"# 시간 단위별 워크포워드 — {len(days_used)}일 ({days_used[0]}~{days_used[-1]})", "",
         "| 봉 · z 창 | 거래 | 거래당 이익(비용 0) | 하루 평균 · 비용 0 (95%) | 반 칸 | 한 칸 | 실측 (95%) |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    rows_csv = []
    for (n, w), by in res.items():
        def mean_ci(name):
            vals = [x[1] for x in by[name]]
            lo, hi = bootstrap(vals)
            return sum(vals) / len(vals), lo, hi
        n0 = sum(x[2] for x in by["0틱"])
        per0 = sum(x[1] for x in by["0틱"]) / n0 if n0 else float("nan")
        m0 = mean_ci("0틱")
        mh = mean_ci("0.5틱")
        m1 = mean_ci("1틱")
        mr = mean_ci("실측")
        L.append(f"| {n}분 × {w}봉 ({n * w}분) | {n0} | {per0 / 1e4:+.1f}만 | {m0[0] / 1e4:+.1f}만 ({m0[1] / 1e4:+.1f}~{m0[2] / 1e4:+.1f}) | "
                 f"{mh[0] / 1e4:+.1f}만 | {m1[0] / 1e4:+.1f}만 | {mr[0] / 1e4:+.1f}만 ({mr[1] / 1e4:+.1f}~{mr[2] / 1e4:+.1f}) |")
        rows_csv.append({"bar_min": n, "window": w, "trades": n0, "per_trade_cost0": round(per0),
                         "daily_cost0": round(m0[0]), "daily_cost0_lo": round(m0[1]), "daily_cost0_hi": round(m0[2]),
                         "daily_half": round(mh[0]), "daily_one": round(m1[0]),
                         "daily_real": round(mr[0]), "daily_real_lo": round(mr[1]), "daily_real_hi": round(mr[2])})
    with (a.out / "timeframe.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows_csv[0].keys()))
        wr.writeheader()
        wr.writerows(rows_csv)
    text = "\n".join(L)
    (a.out / "timeframe.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
