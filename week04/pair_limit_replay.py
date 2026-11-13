"""개선 방향 검증 ② — 삼성 다리를 지정가로 내면 체결 비용이 얼마나 줄고, 얼마나 체결되는가.

실행 이틀의 실시간 체결 기록(ticks.csv: 체결가 + 그 순간 매수·매도 1호가)으로 하루를 다시 돌립니다.
판정은 pair_mid_replay.py 와 같은 pair_engine.py 규칙이고(봉 = 체결가 또는 호가 중간값), 바뀌는 것은 주문 방식뿐입니다.

주문 방식
  market : 지금 방식 — 봉 마감 lag 초 뒤 호가에서 두 다리 시장가(사면 매도호가, 팔면 매수호가).
  limit  : 삼성 다리를 「받는 쪽」 지정가로 먼저 냄 — 사면 그 순간 매수호가, 팔면 매도호가에 걸어 둠.
           wait 초 안에 체결되면 곧바로 하이닉스(1계약)를 시장가로. 안 되면
             · 진입: 취소(거래 안 함)   · 청산: 삼성·하이닉스 모두 시장가로 정리
체결 판정(보수) : 걸어 둔 가격보다 더 불리한 쪽으로 체결이 나거나(사는 주문이면 그보다 낮은 체결),
                  반대 호가가 내 가격까지 오면 체결 — 같은 가격 대기열 순서를 몰라도 확실히 체결된 경우만.
체결 판정(낙관) : 내 가격과 같은 가격의 체결이 한 번이라도 나면 체결(대기열 맨 앞 가정) — 위 결과의 상한.
※ 실제 실행은 하이닉스부터 보내지만, 여기서는 비용이 큰 삼성을 지정가로 먼저 거는 방식을 시험합니다.

    python pair_limit_replay.py results/pair_paper_20261111_090158/ticks.csv --out results/limit_1111
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
from datetime import timedelta
from pathlib import Path

from pair_engine import ABORTED, BUY, COMPLETE, PairEngine, PairParams, align_minutes
from pair_mid_replay import HERE, _J, load_ticks, minute_bars


class _Out:
    def __init__(self, status, qty, avg, msg=""):
        self.status, self.qty, self.avg, self.unwind_pnl, self.message = status, qty, avg, 0.0, msg


def first_at(ticks, times, t):
    i = min(bisect.bisect_left(times, t), len(ticks) - 1)
    return i


def market_px(row, side):
    return row[3] if side == BUY else row[2]     # (ts, last, bid, ask, tm)


def try_limit(ticks, times, t0, side, wait, optimistic):
    """t0 의 호가로 받는 쪽 지정가를 걸고 wait 초 동안 체결 여부. → (체결가 또는 None, 체결 시각, 건 가격)"""
    i = first_at(ticks, times, t0)
    _, _, bid, ask, _ = ticks[i]
    px = bid if side == BUY else ask
    end = t0 + timedelta(seconds=wait)
    j = i + 1
    while j < len(ticks) and ticks[j][0] <= end:
        ts, last, b, a, _ = ticks[j]
        if side == BUY:
            hit = last < px or a <= px or (optimistic and last == px)
        else:
            hit = last > px or b >= px or (optimistic and last == px)
        if hit:
            return px, ts, px
        j += 1
    return None, end, px


def run(ticks, mode, cfg, lag, how, wait, optimistic, stats):
    p = PairParams.from_dict(cfg)
    j = _J()
    eng = PairEngine(p, j)
    eng.warmup = False
    T = {k: [x[0] for x in ticks[k]] for k in ticks}
    bs, bh = minute_bars(ticks["S"], mode), minute_bars(ticks["H"], mode)
    upto = max(bs[-1]["start"], bh[-1]["start"]) + timedelta(minutes=1)
    last: dict[str, float] = {}

    def fill_market(it, t):
        avg, qty = {}, {}
        for leg in it.legs:
            row = ticks[leg.name][first_at(ticks[leg.name], T[leg.name], t)]
            avg[leg.name], qty[leg.name] = market_px(row, leg.side), leg.qty
        return _Out(COMPLETE, qty, avg)

    for pb in align_minutes(bs, bh, None, upto, last):
        end = pb["start"] + timedelta(minutes=1)
        it = eng.on_bar_close(pb)
        if it is None and eng.direction and not eng.pending:
            it = eng.on_prices(pb["close_s"], pb["close_h"], end)
        if it is None:
            continue
        t0 = end + timedelta(seconds=lag)
        i_s = first_at(ticks["S"], T["S"], t0)
        if it.is_entry and ticks["S"][i_s][0] - t0 > timedelta(seconds=30):   # 실행이 멈춘 공백
            eng.on_pair_done(it, _Out(ABORTED, {"S": 0, "H": 0}, {}, "기록 공백"), t0)
            eng.fail_count = 0
            continue
        if how == "market":
            eng.on_pair_done(it, fill_market(it, t0), t0)
            continue
        leg_s = next(l for l in it.legs if l.name == "S")
        leg_h = next(l for l in it.legs if l.name == "H")
        px, tf, posted = try_limit(ticks["S"], T["S"], t0, leg_s.side, wait, optimistic)
        key = "entry" if it.is_entry else "exit"
        stats[f"{key}_try"] = stats.get(f"{key}_try", 0) + 1
        if px is not None:
            stats[f"{key}_fill"] = stats.get(f"{key}_fill", 0) + 1
            stats.setdefault("wait_s", []).append((tf - t0).total_seconds())
            rh = ticks["H"][first_at(ticks["H"], T["H"], tf)]
            out = _Out(COMPLETE, {"S": leg_s.qty, "H": leg_h.qty}, {"S": px, "H": market_px(rh, leg_h.side)})
            eng.on_pair_done(it, out, tf)
        elif it.is_entry:
            eng.on_pair_done(it, _Out(ABORTED, {"S": 0, "H": 0}, {}, f"지정가 {wait:g}초 미체결 — 취소"), tf)
            eng.fail_count = 0
        else:
            eng.on_pair_done(it, fill_market(it, tf), tf)
    if eng.direction:
        it = eng.force_exit(upto, "기록 끝")
        q = {k: ticks[k][-1] for k in ("S", "H")}
        out = _Out(COMPLETE, {l.name: l.qty for l in it.legs}, {l.name: market_px(q[l.name], l.side) for l in it.legs})
        eng.on_pair_done(it, out, upto)
    return j.trades, j.signals


def cost_vs_mid(trades, ticks):
    """참고: 거래 손익을 그대로 합산(체결가 기준)."""
    return sum(t["net_pl"] for t in trades)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticks", type=Path)
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--lag", type=float, default=2.5)
    ap.add_argument("--waits", default="30,60")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cfg = json.loads(Path(a.strategy).read_text(encoding="utf-8"))
    ticks = load_ticks(a.ticks)
    rows, tr_out = [], []
    for mode, mname in (("last", "체결가"), ("mid", "중간값")):
        cases = [("market", 0, False, "시장가(지금)")]
        for w in [float(x) for x in a.waits.split(",")]:
            cases += [("limit", w, False, f"삼성 지정가 {w:g}초 · 보수"), ("limit", w, True, f"삼성 지정가 {w:g}초 · 낙관")]
        for how, w, opt, label in cases:
            st: dict = {}
            tr, sg = run(ticks, mode, cfg, a.lag, how, w, opt, st)
            pnl = sum(t["net_pl"] for t in tr)
            wait = st.get("wait_s", [])
            rows.append({"판단": mname, "주문": label, "거래": len(tr), "손익": pnl,
                         "진입 체결": f"{st.get('entry_fill', 0)}/{st.get('entry_try', 0)}" if how == "limit" else "",
                         "청산 지정가 체결": f"{st.get('exit_fill', 0)}/{st.get('exit_try', 0)}" if how == "limit" else "",
                         "평균 대기(초)": round(sum(wait) / len(wait), 1) if wait else ""})
            for t in tr:
                tr_out.append({"판단": mname, "주문": label, **t})
    L = [f"# 지정가 재현 — {a.ticks}", "", f"봉 마감 {a.lag}초 뒤 주문 · 규칙 strategy_config.json 그대로", "",
         "| 판단 | 주문 | 거래 | 손익 | 진입 체결 | 청산 지정가 체결 | 평균 대기(초) |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        L.append(f"| {r['판단']} | {r['주문']} | {r['거래']} | {r['손익']:,.0f}원 | {r['진입 체결']} | {r['청산 지정가 체결']} | {r['평균 대기(초)']} |")
    text = "\n".join(L)
    print(text)
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "limit_replay.md").write_text(text, encoding="utf-8")
        with open(a.out / "limit_summary.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        if tr_out:
            with open(a.out / "trades.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(tr_out[0].keys()))
                w.writeheader()
                w.writerows(tr_out)


if __name__ == "__main__":
    main()
