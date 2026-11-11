"""과거 분봉으로 가설 검증 — 근월물 이어 붙이기 + 워크포워드 + 날짜 단위 부트스트랩.

실시간 실행(run_pair.py)·백테스트(backtest_pair.py)와 **같은 pair_engine.py** 를 씁니다. 규칙은 strategy_config.json 그대로이고,
바꾸는 것은 β 하나뿐입니다 — 날마다 「그날 전 beta_days 거래일」의 1분 수익률로 다시 정합니다(미래 정보 없음).

1) 근월물 이어 붙이기: 여러 월물 폴더(pair_analyze.py fetch 결과)를 주면, 날마다 두 종목이 함께 체결된 분이 가장 많은
   폴더(월물)를 고릅니다. z 를 하루 안에서만 계산하므로 월물이 바뀌어도 가격을 이어 붙이는 조정이 필요 없습니다.
   함께 체결된 분이 min_fresh(기본 60%) 미만인 날은 뺍니다.
2) 워크포워드: 날 d 의 β = d 전 beta_days(기본 20) 거래일의 r_S = a + β·r_H 회귀. 앞쪽 min_hist(기본 5)일은 β 준비 기간.
3) 비용 민감도: 다리마다 0 · 0.5 · 1틱, 그리고 실측(삼성 1틱 · 하이닉스 0.5틱 — 11/11 모의계좌 14개 페어 주문의 평균). 틱은 가격 수준별 호가단위(삼성 20만원 이상 500원).
4) 날짜 단위 부트스트랩: 거래별이 아니라 날짜별 손익을 다시 뽑아(1만 번) 하루 평균 손익의 95% 구간.
5) 구간 비교: 기본은 「두 종목 괴리 확대기 9/21~10/21」 vs 그 밖.
6) 체결 지연(--delays): 신호·청산 판정 뒤 N봉(분) 뒤 종가에 체결. 0 은 「판정 봉 종가에 바로 체결」.
   1분봉 종가는 마지막 체결가라 호가 차 안에서 튕기므로(bid-ask bounce), 지연 0 의 이익이 실제로 잡을 수 있는
   되돌림인지 가려 보는 점검입니다. 지연 1봉에서도 이익이 남으면 1분 넘게 이어지는 되돌림입니다.

예)
  python pair_walkforward.py --data hist_nov hist_dec
  python pair_walkforward.py --data hist_dec --min-fresh 70 --beta-days 10
결과: results/walkforward_<시각>/ (walkforward.md · daily.csv · trades.csv · days.csv)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pair_engine import BUY, COMPLETE, PairEngine, PairParams, align_minutes, tick_size

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
SESSION = ("090000", "152900")


class _J:
    """기록을 모아 두기만 하는 journal."""

    def __init__(self):
        self.trades, self.signals = [], []

    def bar(self, r): pass
    def signal(self, r): self.signals.append(r)
    def trade(self, r): self.trades.append(r)
    def event(self, *a, **k): pass


class _Fill:
    def __init__(self, it, prices, ticks, slip):
        self.status, self.qty, self.avg, self.unwind_pnl, self.message = COMPLETE, {}, {}, 0.0, ""
        for leg in it.legs:
            d = 1 if leg.side == BUY else -1
            self.qty[leg.name] = leg.qty
            self.avg[leg.name] = prices[leg.name] + d * slip[leg.name] * tick_size(prices[leg.name], ticks[leg.name])


def load(folder: Path, leg: str) -> dict[str, list[dict]]:
    days: dict[str, list[dict]] = {}
    p = folder / f"bars_{leg}.csv"
    if not p.exists():
        return days
    with p.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            t = datetime.strptime(r["date"] + r["time"].zfill(6), "%Y%m%d%H%M%S").replace(tzinfo=KST)
            days.setdefault(r["date"], []).append({"start": t, "close": float(r["close"]), "volume": float(r["volume"])})
    return days


def fresh_share(bs: list[dict], bh: list[dict]) -> float:
    s = {b["start"] for b in bs if b["volume"] > 0 and SESSION[0] <= b["start"].strftime("%H%M%S") <= SESSION[1]}
    h = {b["start"] for b in bh if b["volume"] > 0 and SESSION[0] <= b["start"].strftime("%H%M%S") <= SESSION[1]}
    return 100 * len(s & h) / 390


def pick_days(folders: list[Path], min_fresh: float):
    """날마다 함께 체결된 분이 가장 많은 월물을 고름."""
    data = {f: (load(f, "S"), load(f, "H"), json.loads((f / "meta.json").read_text(encoding="utf-8"))
                if (f / "meta.json").exists() else {}) for f in folders}
    all_days = sorted(set().union(*[set(S) & set(H) for S, H, _ in data.values()]))
    chosen, table = [], []
    for d in all_days:
        best, best_f = None, -1.0
        row = {"date": d}
        for f, (S, H, meta) in data.items():
            if d in S and d in H:
                fs = fresh_share(S[d], H[d])
                row[f.name] = round(fs, 1)
                if fs > best_f:
                    best, best_f = f, fs
        row["chosen"] = best.name if best and best_f >= min_fresh else ""
        row["fresh"] = round(best_f, 1)
        table.append(row)
        if best and best_f >= min_fresh:
            S, H, meta = data[best]
            chosen.append((d, best.name, S[d], H[d], meta))
    return chosen, table


def day_returns(bs, bh) -> tuple[list[float], list[float]]:
    """두 봉 연속 두 종목 모두 체결된 분의 1분 로그수익률 쌍."""
    rows = align_minutes(bs, bh, None, max(bs[-1]["start"], bh[-1]["start"]) + timedelta(minutes=1), {})
    rows = [r for r in rows if SESSION[0] <= r["start"].strftime("%H%M%S") <= SESSION[1]]
    xs, ys = [], []
    for a, b in zip(rows, rows[1:]):
        if a["fresh_s"] and a["fresh_h"] and b["fresh_s"] and b["fresh_h"] and b["start"] - a["start"] == timedelta(minutes=1):
            ys.append(math.log(b["close_s"] / a["close_s"]))
            xs.append(math.log(b["close_h"] / a["close_h"]))
    return xs, ys


def ols_beta(xs, ys) -> float:
    n = len(xs)
    if n < 30:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx if vx else float("nan")


def run_day(cfg: dict, beta: float, bs, bh, slip: dict, qs_override=None, delay: int = 0):
    """하루 실행. delay = 판정 뒤 몇 봉 뒤 종가에 체결할지(0 = 판정 봉 종가). 그사이 엔진은 주문 대기 상태."""
    p = PairParams.from_dict({**cfg, "beta": beta})
    if qs_override:
        p.qty_s, p.qty_h = qs_override
    j = _J()
    eng = PairEngine(p, j)
    eng.warmup = False
    ticks = {"S": p.tick_s, "H": p.tick_h}
    last: dict[str, float] = {}
    upto = max(bs[-1]["start"], bh[-1]["start"]) + timedelta(minutes=1)
    queue: list = []   # (체결할 봉 번호, intent)
    for i, pb in enumerate(align_minutes(bs, bh, None, upto, last)):
        ts = pb["start"] + timedelta(minutes=1)
        if queue and queue[0][0] == i:
            _, it = queue.pop(0)
            eng.on_pair_done(it, _Fill(it, {"S": pb["close_s"], "H": pb["close_h"]}, ticks, slip), ts)
        it = eng.on_bar_close(pb)
        if it is None and eng.direction and not eng.pending:
            it = eng.on_prices(pb["close_s"], pb["close_h"], ts)
        if it is not None:
            if delay <= 0:
                eng.on_pair_done(it, _Fill(it, {"S": pb["close_s"], "H": pb["close_h"]}, ticks, slip), ts)
            else:
                queue.append((i + delay, it))
    if queue:
        _, it = queue.pop(0)
        eng.on_pair_done(it, _Fill(it, dict(last), ticks, slip), upto)
    if eng.direction:
        it = eng.force_exit(upto, "그날 마지막 봉")
        eng.on_pair_done(it, _Fill(it, dict(last), ticks, slip), upto)
    return j.trades, j.signals


def bootstrap(vals: list[float], n=10000, seed=7) -> tuple[float, float]:
    if len(vals) < 2:
        return float("nan"), float("nan")
    rnd = random.Random(seed)
    means = sorted(statistics.mean(rnd.choices(vals, k=len(vals))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def fmt(x, d=0):
    if isinstance(x, float) and math.isnan(x):
        return "—"
    return f"{x:,.{d}f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", nargs="+", required=True, help="월물별 폴더(pair_analyze.py fetch 결과) 여러 개")
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--min-fresh", type=float, default=60.0, help="두 종목이 함께 체결된 분 %% 하한")
    ap.add_argument("--beta-days", type=int, default=20)
    ap.add_argument("--min-hist", type=int, default=5)
    ap.add_argument("--regime", default="20260921-20261021", help="구간 비교용 기간 YYYYMMDD-YYYYMMDD")
    ap.add_argument("--delays", default="0,1,2", help="체결 지연(봉) 목록. 첫 값이 본문 결과의 기준")
    ap.add_argument("--out")
    a = ap.parse_args()

    cfg = json.loads(Path(a.strategy).read_text(encoding="utf-8"))
    delays = [int(x) for x in a.delays.split(",") if x.strip() != ""] or [0]
    folders = [Path(x) for x in a.data]
    chosen, table = pick_days(folders, a.min_fresh)
    out = Path(a.out) if a.out else HERE / "results" / f"walkforward_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)

    # 날짜별 수익률(β 추정용)
    rets = {d: day_returns(bs, bh) for d, _, bs, bh, _ in chosen}
    scenarios = {"0틱": {"S": 0, "H": 0}, "0.5틱": {"S": 0.5, "H": 0.5}, "1틱": {"S": 1, "H": 1},
                 "실측(11/11 삼성1·하이닉스0.5)": {"S": 1, "H": 0.5}}
    daily: list[dict] = []
    trades_all: list[dict] = []
    signals_n: dict[str, int] = {}
    for i, (d, src, bs, bh, meta) in enumerate(chosen):
        hist = [rets[x[0]] for x in chosen[max(0, i - a.beta_days):i]]
        if len(hist) < a.min_hist:
            continue
        xs = [x for h in hist for x in h[0]]
        ys = [y for h in hist for y in h[1]]
        beta = ols_beta(xs, ys)
        if math.isnan(beta):
            continue
        row = {"date": d, "contract": src, "beta": round(beta, 3), "hist_days": len(hist)}
        for dl in delays:
            for name, slip in scenarios.items():
                tr, sig = run_day(cfg, beta, bs, bh, slip, delay=dl)
                key = name if dl == delays[0] else f"d{dl}_{name}"
                row[f"pnl_{key}"] = sum(t["net_pl"] for t in tr)
                row[f"n_{key}"] = len(tr)
                if dl == delays[0] and name == "1틱":
                    signals_n[d] = len(sig)
                    for t in tr:
                        trades_all.append({"date": d, "contract": src, "beta": round(beta, 3), **t})
        daily.append(row)

    with (out / "days.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=sorted({k for r in table for k in r}, key=lambda k: (k != "date", k)))
        w.writeheader()
        w.writerows(table)
    if daily:
        with (out / "daily.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(daily[0].keys()))
            w.writeheader()
            w.writerows(daily)
    if trades_all:
        with (out / "trades.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(trades_all[0].keys()))
            w.writeheader()
            w.writerows(trades_all)

    # ---------------- 보고서
    r0, r1 = a.regime.split("-")
    L = ["# 과거 분봉 가설 검증 — 근월물 · 워크포워드", "",
         f"만든 시각 {datetime.now():%Y-%m-%d %H:%M} · 폴더 {', '.join(f.name for f in folders)} · "
         f"함께 체결된 분 ≥ {a.min_fresh:g}% · β = 직전 {a.beta_days}거래일(최소 {a.min_hist}일) · 규칙은 strategy_config.json 그대로", ""]
    used = [r for r in table if r["chosen"]]
    L += ["## 1. 쓴 날", "",
          f"전체 {len(table)}일 중 유동성 기준을 넘은 날 {len(used)}일, 그중 β 준비 기간을 뺀 검증일 **{len(daily)}일**"
          + (f" ({daily[0]['date']}~{daily[-1]['date']})" if daily else ""), ""]
    by_src: dict[str, int] = {}
    for r in used:
        by_src[r["chosen"]] = by_src.get(r["chosen"], 0) + 1
    L.append("월물별 선택 일수: " + " · ".join(f"{k} {v}일" for k, v in by_src.items()))
    L.append("")
    if not daily:
        (out / "walkforward.md").write_text("\n".join(L + ["검증일이 없습니다."]), encoding="utf-8")
        print("\n".join(L))
        return

    L += ["## 2. 비용별 결과 (삼성 10 · 하이닉스 1)", "",
          "| 비용(다리마다) | 거래 | 하루 평균 거래 | 순손익 | 하루 평균 | 하루 평균 95% 구간(날짜 부트스트랩) | 이긴 날 |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for name in scenarios:
        vals = [r[f"pnl_{name}"] for r in daily]
        n = sum(r[f"n_{name}"] for r in daily)
        lo, hi = bootstrap(vals)
        L.append(f"| {name} | {n} | {n / len(daily):.1f} | {fmt(sum(vals))}원 | {fmt(statistics.mean(vals))}원 | "
                 f"{fmt(lo)} ~ {fmt(hi)}원 | {sum(1 for v in vals if v > 0)}/{len(vals)} |")
    L += ["", "95% 구간이 0 을 포함하면 「하루 평균 손익이 0 과 다르다」고 말할 수 없습니다.", ""]

    if len(delays) > 1:
        L += ["## 2-1. 체결 지연별 하루 평균 손익 (95% 구간)", "",
              "| 체결 시점 | " + " | ".join(scenarios) + " |", "| --- | " + " | ".join("---" for _ in scenarios) + " |"]
        for dl in delays:
            cells = []
            for name in scenarios:
                key = name if dl == delays[0] else f"d{dl}_{name}"
                vals = [r[f"pnl_{key}"] for r in daily]
                lo, hi = bootstrap(vals)
                cells.append(f"{fmt(statistics.mean(vals))} ({fmt(lo)} ~ {fmt(hi)})")
            label = "판정 봉 종가" if dl == 0 else f"{dl}봉 뒤 종가"
            L.append(f"| {label} | " + " | ".join(cells) + " |")
        L += ["", "1분봉 종가는 마지막 체결가라 호가 차 안에서 튕깁니다. 지연을 두면 사라지는 이익은 "
              "1분 안에 끝나는 되돌림이거나 호가 튕김이라, 1분봉 판단·시장가 주문으로는 잡기 어렵습니다.", ""]

    tr = trades_all
    L += ["## 3. 거래 내용 (비용 1틱 기준)", ""]
    if tr:
        reasons: dict[str, list[float]] = {}
        for t in tr:
            reasons.setdefault(t["exit_reason"], []).append(t["net_pl"])
        conv = [t for t in tr if t["exit_reason"] == "EXIT_CONVERGE"]
        L += ["| 청산 사유 | 건수 | 비율 | 평균 손익 | 합계 |", "| --- | --- | --- | --- | --- |"]
        for k, v in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            L.append(f"| {k} | {len(v)} | {100 * len(v) / len(tr):.0f}% | {fmt(statistics.mean(v))}원 | {fmt(sum(v))}원 |")
        sp = [t["spread_bp"] for t in tr]
        L += ["", f"- 체결가 기준 스프레드 변화 평균 {statistics.mean(sp):+.1f}bp (이익 방향 +)",
              f"- 수렴 청산 중 손실로 끝난 비율 {100 * sum(1 for t in conv if t['net_pl'] < 0) / max(1, len(conv)):.0f}% "
              "(rolling 평균이 따라와 생기는 「수렴인데 손실」 + 비용)",
              f"- 보유 봉 수 중앙 {statistics.median([t['held_bars'] for t in tr]):.0f}봉", ""]

    L += ["## 4. 구간 비교 (비용 1틱)", "", "| 구간 | 검증일 | 거래 | 순손익 | 하루 평균 | 95% 구간 |", "| --- | --- | --- | --- | --- | --- |"]
    for label, cond in ((f"{r0}~{r1}", lambda d: r0 <= d <= r1), ("그 밖", lambda d: not (r0 <= d <= r1))):
        rows = [r for r in daily if cond(r["date"])]
        if not rows:
            L.append(f"| {label} | 0 | — | — | — | — |")
            continue
        vals = [r["pnl_1틱"] for r in rows]
        lo, hi = bootstrap(vals)
        L.append(f"| {label} | {len(rows)} | {sum(r['n_1틱'] for r in rows)} | {fmt(sum(vals))}원 | "
                 f"{fmt(statistics.mean(vals))}원 | {fmt(lo)} ~ {fmt(hi)}원 |")
    L.append("")

    L += ["## 5. 날짜별", "", "| 날짜 | 월물 | β | 거래 | 0틱 | 1틱 | 실측 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in daily:
        L.append(f"| {r['date']} | {r['contract']} | {r['beta']} | {r['n_1틱']} | {fmt(r['pnl_0틱'])} | "
                 f"{fmt(r['pnl_1틱'])} | {fmt(r['pnl_실측(11/11 삼성1·하이닉스0.5)'])} |")
    betas = [r["beta"] for r in daily]
    L += ["", f"워크포워드 β: 중앙 {statistics.median(betas):.2f} · 범위 {min(betas):.2f}~{max(betas):.2f}", "",
          "## 파일", "", "`days.csv` 날짜별 월물 유동성·선택 · `daily.csv` 날짜별 손익(비용별) · `trades.csv` 거래(1틱) · "
          "다시 만들기: `python pair_walkforward.py --data " + " ".join(a.data) + "`", ""]
    (out / "walkforward.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
