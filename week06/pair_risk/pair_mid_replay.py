"""실시간 체결 기록(ticks.csv)으로 하루를 다시 돌려 「체결가 판단 vs 중간값 판단」을 비교합니다.

배경(11/11): 1분봉 종가 = 그 분의 마지막 체결가라 매수·매도호가 사이를 튕깁니다(bid-ask bounce).
삼성 선물은 20만원 이상에서 호가 한 칸이 500원(약 24bp)으로 스프레드의 1분 표준편차(약 18bp)보다 커서,
종가가 한 칸 튕기기만 해도 z 가 1 넘게 움직여 가짜 신호가 납니다. 중간값 (매수호가+매도호가)/2 는
호가가 실제로 움직일 때만 바뀌므로 이 튕김이 없습니다.

ticks.csv 의 한 줄 = WebSocket 시세체결(KA) 한 건(체결가 · 그 순간 매수·매도 1호가).
같은 pair_engine.py 로 두 가지 봉을 판단합니다.
  last : 1분 동안 마지막 체결가 (지금 실행 방식)
  mid  : 1분 동안 마지막 호가의 중간값
체결 가정(두 판단 모두 같음): 봉 마감 + lag 초 뒤 첫 호가에서 시장가 — 사면 매도호가, 팔면 매수호가.
비교용으로 「그 순간 중간값에 체결(비용 0)」도 같이 계산합니다.

    python pair_mid_replay.py results/pair_paper_20261111_090158/ticks.csv --lag 2.5
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from pair_engine import ABORTED, BUY, COMPLETE, PairEngine, PairParams, align_minutes

HERE = Path(__file__).resolve().parent


class _J:
    def __init__(self):
        self.trades, self.signals = [], []

    def bar(self, r): pass
    def signal(self, r): self.signals.append(r)
    def trade(self, r): self.trades.append(r)
    def event(self, *a, **k): pass


def load_ticks(path: Path):
    rows = {"S": [], "H": []}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ts = datetime.fromisoformat(r["recv_ts"])
            bid, ask, last = float(r["bid"] or 0), float(r["ask"] or 0), float(r["last"] or 0)
            if bid <= 0 or ask <= 0 or last <= 0:
                continue
            rows[r["leg"]].append((ts, last, bid, ask, r["exch_tm"]))
    for k in rows:
        rows[k].sort(key=lambda x: x[0])
    return rows


def minute_bars(ticks, mode: str):
    """거래소 체결시각(분) 기준 1분봉. mode=last → 마지막 체결가, mid → 마지막 호가 중간값."""
    out = {}
    for ts, last, bid, ask, tm in ticks:
        start = ts.replace(hour=int(tm[:2]), minute=int(tm[2:4]), second=0, microsecond=0)
        out[start] = {"start": start, "close": last if mode == "last" else (bid + ask) / 2, "volume": 1}
    return [out[k] for k in sorted(out)]


class _QFill:
    def __init__(self, it, quotes, at_mid: bool):
        self.status, self.qty, self.avg, self.unwind_pnl, self.message = COMPLETE, {}, {}, 0.0, ""
        for leg in it.legs:
            bid, ask = quotes[leg.name]
            self.qty[leg.name] = leg.qty
            self.avg[leg.name] = (bid + ask) / 2 if at_mid else (ask if leg.side == BUY else bid)


def quote_at(ticks, times, t):
    """t 이후 첫 호가와 그 시각."""
    i = bisect.bisect_left(times, t)
    i = min(i, len(ticks) - 1)
    return (ticks[i][2], ticks[i][3]), ticks[i][0]


class _Abort:
    status, qty, avg, unwind_pnl, message = ABORTED, {"S": 0, "H": 0}, {}, 0.0, "기록 공백(실행 멈춤) — 진입 안 함"


def run(ticks, mode: str, cfg: dict, lag: float, at_mid: bool):
    p = PairParams.from_dict(cfg)
    j = _J()
    eng = PairEngine(p, j)
    eng.warmup = False
    times = {k: [x[0] for x in ticks[k]] for k in ticks}
    bs, bh = minute_bars(ticks["S"], mode), minute_bars(ticks["H"], mode)
    upto = max(bs[-1]["start"], bh[-1]["start"]) + timedelta(minutes=1)
    last: dict[str, float] = {}
    for pb in align_minutes(bs, bh, None, upto, last):
        end = pb["start"] + timedelta(minutes=1)
        it = eng.on_bar_close(pb)
        if it is None and eng.direction and not eng.pending:
            it = eng.on_prices(pb["close_s"], pb["close_h"], end)
        if it is not None:
            t = end + timedelta(seconds=lag)
            got = {k: quote_at(ticks[k], times[k], t) for k in ("S", "H")}
            stale = any(at - t > timedelta(seconds=30) for _, at in got.values())
            if stale and it.is_entry:      # 실행이 멈춘 공백(12:36~13:12 등) — 실제로도 주문이 나갈 수 없음
                eng.on_pair_done(it, _Abort(), t)
                eng.fail_count = 0
                continue
            eng.on_pair_done(it, _QFill(it, {k: q for k, (q, _) in got.items()}, at_mid), t)
    if eng.direction:
        it = eng.force_exit(upto, "기록 끝")
        q = {k: (ticks[k][-1][2], ticks[k][-1][3]) for k in ("S", "H")}
        eng.on_pair_done(it, _QFill(it, q, at_mid), upto)
    return j.trades, j.signals


def spread_stats(ticks):
    out = {}
    for k, rows in ticks.items():
        hi = [r for r in rows if r[2] >= 200_000] if k == "S" else rows
        lo = [r for r in rows if r[2] < 200_000] if k == "S" else []
        def one(rs, tick):
            if not rs:
                return None
            w = [round((r[3] - r[2]) / tick) for r in rs]
            return {"n": len(rs), "one_tick": sum(1 for x in w if x == 1) / len(w), "mean_ticks": sum(w) / len(w)}
        out[k] = {"hi": one(hi, 500 if k == "S" else 1000), "lo": one(lo, 100)}
        # 체결가 튕김: 직전 체결과 같은 호가인데 체결가가 반대쪽으로 바뀐 비율
        flips = same = 0
        for a, b in zip(rows, rows[1:]):
            if a[2] == b[2] and a[3] == b[3]:
                same += 1
                if a[1] != b[1]:
                    flips += 1
        out[k]["bounce_share"] = flips / same if same else 0
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticks", type=Path)
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--lag", type=float, default=2.5, help="봉 마감 뒤 몇 초 뒤 호가에서 체결할지(실행은 약 2~3초)")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cfg = json.loads(Path(a.strategy).read_text(encoding="utf-8"))
    ticks = load_ticks(a.ticks)
    res = {}
    for mode in ("last", "mid"):
        for at_mid in (False, True):
            tr, sg = run(ticks, mode, cfg, a.lag, at_mid)
            res[(mode, at_mid)] = (tr, sg)
    L = [f"# 체결가 판단 vs 중간값 판단 — {a.ticks}", "",
         f"체결 가정: 봉 마감 {a.lag}초 뒤 첫 호가 · 규칙 strategy_config.json 그대로", "",
         "| 판단 기준 | 신호 | 진입 | 거래 | 시장가 체결 손익 | 중간값 체결 손익(비용 0) | 차이(체결 비용) |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for mode, name in (("last", "체결가(지금)"), ("mid", "중간값")):
        tr, sg = res[(mode, False)]
        tr0, _ = res[(mode, True)]
        pnl, pnl0 = sum(t["net_pl"] for t in tr), sum(t["net_pl"] for t in tr0)
        L.append(f"| {name} | {len(sg)} | {sum(1 for s in sg if s.get('taken'))} | {len(tr)} | {pnl:,.0f}원 | "
                 f"{pnl0:,.0f}원 | {pnl - pnl0:,.0f}원 |")
    st = spread_stats(ticks)
    L += ["", "## 호가 차와 튕김", "",
          "| 종목 | 구간 | 체결 건수 | 호가 차가 딱 한 칸인 비율 | 평균 호가 차(칸) |", "| --- | --- | --- | --- | --- |"]
    for k, nm in (("S", "삼성"), ("H", "하이닉스")):
        for part, lab in (("hi", "20만원 이상" if k == "S" else "전체"), ("lo", "20만원 미만")):
            v = st[k][part]
            if v:
                L.append(f"| {nm} | {lab} | {v['n']:,} | {v['one_tick']:.0%} | {v['mean_ticks']:.2f} |")
    L += ["", f"- 호가가 그대로인데 체결가만 매수↔매도호가로 바뀐 비율(튕김): 삼성 {st['S']['bounce_share']:.0%} · "
              f"하이닉스 {st['H']['bounce_share']:.0%}", "", "## 거래 (시장가 체결)", ""]
    for mode, name in (("last", "체결가"), ("mid", "중간값")):
        L += [f"**{name} 판단**", "", "| 진입 | 방향 | 진입 z | 청산 | 사유 | 손익 |", "| --- | --- | --- | --- | --- | --- |"]
        for t in res[(mode, False)][0]:
            L.append(f"| {t['entry_time'][11:16]} | {t['direction']} | {t['entry_z']} | {t['exit_time'][11:16]} | "
                     f"{t['exit_reason']} | {t['net_pl']:,.0f} |")
        L.append("")
    text = "\n".join(L)
    print(text)
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "mid_replay.md").write_text(text, encoding="utf-8")
        for (mode, at_mid), (tr, _) in res.items():
            if tr and not at_mid:
                with open(a.out / f"trades_{mode}.csv", "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.DictWriter(f, fieldnames=list(tr[0].keys()))
                    w.writeheader()
                    w.writerows(tr)


if __name__ == "__main__":
    main()
