"""실제 1분봉으로 전략 숫자의 근거를 재고, 규칙 변형을 같은 봉으로 비교합니다.

  python analyze.py results/backtest_xxx/bars_input.csv [--split 3]

출력: results/analysis_<시각>/analysis.md
  1) 데이터 개요 — 날짜별 봉 수·일중 고저폭
  2) 1분봉 변동폭(고가−저가) 분포 — 손절 5틱이 「잡음」보다 넓은가
  3) 밴드폭 분포(전체·시간대별) — 8틱 필터가 하루 중 얼마를 거르나
  4) 신호 시점의 중심선까지 거리 — 이길 때 먹을 수 있는 폭
  5) 규칙 변형 비교 — **앞 N일(설계용)과 뒤 날짜(검증용)를 나눠서** 봅니다.
     설계용에서 좋아 보인 변형이 검증용에서도 유지되는지가 핵심입니다(과최적화 점검).
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest import load_bars, run_backtest
from engine import Params, bollinger
from summarize import _read

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def by_day(bars: list[dict]) -> dict:
    d = defaultdict(list)
    for b in bars:
        d[b["start"].date()].append(b)
    return dict(sorted(d.items()))


def describe(bars: list[dict], p: Params) -> list[str]:
    t = p.tick_size
    L = ["## 1. 데이터\n", "| 날짜 | 봉 | 시작~끝 | 일중 고가−저가 | 시가→종가 |", "| --- | --- | --- | --- | --- |"]
    days = by_day(bars)
    for d, bs in days.items():
        hi, lo = max(b["high"] for b in bs), min(b["low"] for b in bs)
        L.append(f"| {d} | {len(bs)} | {bs[0]['start']:%H:%M}~{bs[-1]['start']:%H:%M} | {hi-lo:.1f}원 ({(hi-lo)/t:.0f}틱) | "
                 f"{bs[-1]['close']-bs[0]['open']:+.1f}원 |")

    rng = [(b["high"] - b["low"]) / t for b in bars]
    mv = [abs(b["close"] - b["open"]) / t for b in bars]
    L += ["\n## 2. 1분봉 하나의 움직임 (틱)\n",
          "| | 중앙값 | 75% | 90% | 95% |", "| --- | --- | --- | --- | --- |",
          f"| 고가−저가 | {pct(rng,.5):.1f} | {pct(rng,.75):.1f} | {pct(rng,.9):.1f} | {pct(rng,.95):.1f} |",
          f"| |종가−시가| | {pct(mv,.5):.1f} | {pct(mv,.75):.1f} | {pct(mv,.9):.1f} | {pct(mv,.95):.1f} |",
          f"\n1분봉 고저폭이 손절 {p.stop_ticks}틱 **이상**인 봉: **{sum(round(r) >= p.stop_ticks for r in rng)/len(rng)*100:.1f}%** "
          f"(초과는 {sum(round(r) > p.stop_ticks for r in rng)/len(rng)*100:.1f}%, 정확히 {p.stop_ticks}틱은 "
          f"{sum(round(r) == p.stop_ticks for r in rng)/len(rng)*100:.1f}%). 손절은 체결가에서 정확히 {p.stop_ticks}틱 불리해지면 걸리므로 "
          f"「이상」 쪽이 판단 기준입니다 — 이 비율이 높으면 손절이 평소 출렁임에 걸리기 쉽습니다."]

    width, width_h = [], defaultdict(list)
    rewards = []
    for d, bs in days.items():
        closes = []
        prev = None
        for b in bs:
            closes.append(b["close"])
            if len(closes) < p.bb_period:
                continue
            mid, up, lo = bollinger(closes[-p.bb_period:], p.bb_k)
            w = (up - lo) / t
            width.append(w)
            width_h[b["start"].hour].append(w)
            if prev:
                c = b["close"]
                if (prev[0] < prev[3] and c >= lo) or (prev[0] > prev[2] and c <= up):
                    rewards.append((abs(mid - c) / t, w))
            prev = (b["close"], mid, up, lo)

    L += ["\n## 3. 볼린저 밴드폭 (상단−하단, 틱)\n",
          f"전체 {len(width)}봉 — 중앙값 **{pct(width,.5):.1f}**, 25% {pct(width,.25):.1f}, 75% {pct(width,.75):.1f}, 90% {pct(width,.9):.1f}\n",
          "| 기준 | 밴드폭이 그보다 좁은 봉 비율 |", "| --- | --- |"]
    for th in (6, 8, 10, 12, 15):
        L.append(f"| {th}틱 | {sum(w < th for w in width)/len(width)*100:.0f}% |")
    L += ["\n| 시간 | 봉 | 밴드폭 중앙값 | 8틱 미만 비율 |", "| --- | --- | --- | --- |"]
    for h, ws in sorted(width_h.items()):
        L.append(f"| {h:02d}시 | {len(ws)} | {pct(ws,.5):.1f} | {sum(w < 8 for w in ws)/len(ws)*100:.0f}% |")

    if rewards:
        rw = [r for r, _ in rewards]
        L += ["\n## 4. 신호 봉 종가 → 중심선 거리 (먹을 수 있는 최대 폭, 틱)\n",
              f"신호(필터 전) {len(rewards)}건 — 중앙값 **{pct(rw,.5):.1f}틱**, 25% {pct(rw,.25):.1f}, 75% {pct(rw,.75):.1f}",
              f"\n여기서 진입 미끄러짐 약 1틱을 빼면 이기는 거래의 기대 폭이고, 지는 거래는 손절 {p.stop_ticks}틱 + 약 1틱입니다. "
              f"손익분기 승률 ≈ 손실폭 ÷ (이익폭 + 손실폭) = {(p.stop_ticks+1)/((pct(rw,.5)-1)+(p.stop_ticks+1))*100:.0f}% (중앙값 기준)."]
    return L


def stats(folder: Path) -> dict:
    tr = _read(folder / "trades.csv")
    sg = _read(folder / "signals.csv")
    pl = [float(t["net_pl"]) for t in tr]
    cum = peak = mdd = 0.0
    for x in pl:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    wins = [x for x in pl if x > 0]
    loss = [x for x in pl if x <= 0]
    return {
        "signals": len(sg), "trades": len(tr),
        "win": len(wins) / len(tr) * 100 if tr else 0,
        "net": sum(pl), "mdd": mdd,
        "avg_win": st.mean(wins) if wins else 0, "avg_loss": st.mean(loss) if loss else 0,
        "pf": sum(wins) / -sum(loss) if loss and sum(loss) < 0 else float("inf") if wins else 0,
        "stop": sum(t["exit_reason"] == "EXIT_STOP" for t in tr),
    }


VARIANTS = {
    "V0 기준(현재 설정)": {},
    "V1 밴드폭 6틱": {"min_band_width_ticks": 6},
    "V2 밴드폭 10틱": {"min_band_width_ticks": 10},
    "V3 밴드폭 12틱": {"min_band_width_ticks": 12},
    "V4 손절 3틱": {"stop_ticks": 3},
    "V5 손절 7틱": {"stop_ticks": 7},
    "V6 극값 손절(3~8틱)": {"stop_mode": "swing"},
    "V7 보상 ≥ 4틱": {"min_reward_ticks": 4},
    "V8 추세 필터(MA60, 10봉 3틱)": {"trend_ma_period": 60, "trend_slope_bars": 10, "trend_max_slope_ticks": 3},
    "V9 V7+V8": {"min_reward_ticks": 4, "trend_ma_period": 60, "trend_slope_bars": 10, "trend_max_slope_ticks": 3},
    "V10 V6+V7+V8": {"stop_mode": "swing", "min_reward_ticks": 4, "trend_ma_period": 60, "trend_slope_bars": 10,
                     "trend_max_slope_ticks": 3},
}


def compare(bars: list[dict], base: Params, out: Path, split: int) -> list[str]:
    days = list(by_day(bars))
    design = [b for b in bars if b["start"].date() in days[:split]]
    check = [b for b in bars if b["start"].date() in days[split:]]
    L = ["\n## 5. 규칙 변형 비교\n",
         f"설계용 {', '.join(map(str, days[:split]))} / 검증용 {', '.join(map(str, days[split:])) or '없음'} — 슬리피지 1틱, 수수료 0원\n",
         "| 변형 | 구간 | 신호 | 거래 | 승률 | 순손익 | 비용 0 일 때 순손익 | 평균 이익 / 손실 | PF | 손절 | 최대낙폭 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    rows = {}
    for name, change in VARIANTS.items():
        p = replace(base, **change)
        for label, data in (("설계", design), ("검증", check)):
            if not data:
                continue
            f = run_backtest(data, p, out / f"{name.split()[0]}_{label}", slippage_ticks=1, meta={"variant": name})
            s = stats(f)
            s0 = stats(run_backtest(data, p, out / f"{name.split()[0]}_{label}_cost0", slippage_ticks=0,
                                    meta={"variant": name + " (slippage 0)"}))
            s["net_cost0"] = s0["net"]
            rows[(name, label)] = s
            pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            L.append(f"| {name} | {label} | {s['signals']} | {s['trades']} | {s['win']:.0f}% | {s['net']:+,.0f} | {s['net_cost0']:+,.0f} | "
                     f"{s['avg_win']:+,.0f} / {s['avg_loss']:+,.0f} | {pf} | {s['stop']} | {s['mdd']:,.0f} |")
    (out / "variants.json").write_text(json.dumps({f"{k[0]}|{k[1]}": v for k, v in rows.items()}, ensure_ascii=False, indent=1,
                                                   default=str), encoding="utf-8")
    L.append("\n「비용 0」 칸은 진입·청산 미끄러짐을 0틱으로 둔 값입니다. 이 칸과 순손익의 차이가 거래 비용(왕복 약 2틱)이 "
             "먹은 몫이고, 비용 0 에서도 손실이면 신호 자체에 우위가 없다는 뜻입니다.")
    L.append("\n읽는 법: 설계 구간에서 좋아 보여도 검증 구간에서 무너지면 그 변형은 지난 데이터에만 맞춘 것입니다. "
             "거래가 몇 건 안 되는 칸은 결론을 내리지 않습니다.")
    return L


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bars_csv", nargs="+")
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--split", type=int, default=0, help="앞에서 몇 거래일을 설계용으로 쓸지(기본: 전체의 60%)")
    ap.add_argument("--results", default=str(HERE / "results"))
    a = ap.parse_args()

    base = Params.from_dict(json.loads(Path(a.strategy).read_text(encoding="utf-8")))
    merged = {}
    for f in a.bars_csv:
        for b in load_bars(Path(f)):
            merged[b["start"]] = b
    bars = [merged[k] for k in sorted(merged)]
    n_days = len(by_day(bars))
    split = a.split or max(1, round(n_days * 0.6))
    out = Path(a.results) / f"analysis_{datetime.now(KST):%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)

    L = [f"# 미국달러 선물 1분봉 분석 — {bars[0]['start']:%Y-%m-%d} ~ {bars[-1]['start']:%Y-%m-%d}\n",
         f"입력: {', '.join(a.bars_csv)} · 봉 {len(bars)}개 · 기준 설정 `{Path(a.strategy).name}`\n"]
    L += describe(bars, base)
    L += compare(bars, base, out, split)
    (out / "analysis.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out / "base_params.json").write_text(json.dumps(asdict(base), ensure_ascii=False, indent=1), encoding="utf-8")
    print(out / "analysis.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
