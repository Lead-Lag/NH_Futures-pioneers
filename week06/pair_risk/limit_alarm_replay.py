"""다음 검증 ④ — 삼성 지정가 + 하이닉스 「경보」: 초 단위 선행을 지정가 주문 타이밍에 쓰면 역선택이 줄어드는가.

4주차 pair_limit_replay.py 의 「삼성 다리 지정가」에 규칙 하나만 더합니다.
  삼성 지정가를 걸어 둔 동안 하이닉스 호가 중간값이 내 주문에 불리한 쪽으로 alarm_bp 이상 움직이면 즉시 취소
    (삼성을 사려고 매수호가에 걸었는데 하이닉스가 떨어짐 → 삼성도 곧 떨어질 가능성 → 체결되면 바로 손해)
  취소되면 · 진입: 거래 안 함   · 청산: 삼성·하이닉스 모두 시장가로 정리
근거: 6-1절 — 하이닉스가 5초에 15bp 움직이면 삼성이 30초 안에 같은 방향으로 약 4bp 따라옴.

같은 체결 기록(ticks.csv) · 같은 pair_engine.py 판정 · 체결 판정 보수/낙관 두 가지(대기열 순서를 모르므로).
비교: 시장가(지금) · 지정가 · 지정가+경보(5 · 10 · 15bp).
품질 지표: 삼성 지정가가 체결된 뒤 30초 동안 삼성 중간값이 체결가 대비 내 쪽으로 움직인 폭(bp, +면 유리) = 역선택 측정.

  python limit_alarm_replay.py <실행폴더 또는 ticks.csv> ...  [--wait 30]
결과: results/limit_alarm/ (report.txt · summary.csv)
"""
from __future__ import annotations

import argparse
import bisect
import csv
import statistics
from datetime import timedelta
from pathlib import Path

from pair_engine import ABORTED, BUY, COMPLETE, PairEngine, PairParams, align_minutes
from pair_limit_replay import _Out, first_at, market_px
from pair_mid_replay import HERE, _J, load_ticks, minute_bars

import json


def mid(row):
    return (row[2] + row[3]) / 2


def try_limit_alarm(tS, TS, tH, TH, t0, side, wait, optimistic, alarm_bp):
    """→ (체결가 또는 None, 시각, 사유)  사유: fill · timeout · alarm"""
    i = first_at(tS, TS, t0)
    _, _, bid, ask, _ = tS[i]
    px = bid if side == BUY else ask
    end = t0 + timedelta(seconds=wait)
    h0 = mid(tH[first_at(tH, TH, t0)])
    jS = i + 1
    jH = first_at(tH, TH, t0) + 1
    while jS < len(tS) and tS[jS][0] <= end:
        ts = tS[jS][0]
        if alarm_bp:
            while jH < len(tH) and tH[jH][0] <= ts:
                mv = (mid(tH[jH]) / h0 - 1) * 1e4
                against = (side == BUY and mv <= -alarm_bp) or (side != BUY and mv >= alarm_bp)
                if against:
                    return None, tH[jH][0], "alarm"
                jH += 1
        _, last, b, a, _ = tS[jS]
        hit = (last < px or a <= px or (optimistic and last == px)) if side == BUY else \
              (last > px or b >= px or (optimistic and last == px))
        if hit:
            return px, ts, "fill"
        jS += 1
    return None, end, "timeout"


def run(ticks, cfg, how, wait, optimistic, alarm_bp, lag, st):
    p = PairParams.from_dict(cfg)
    j = _J()
    eng = PairEngine(p, j)
    eng.warmup = False
    T = {k: [x[0] for x in ticks[k]] for k in ticks}
    bs, bh = minute_bars(ticks["S"], "trade"), minute_bars(ticks["H"], "trade")
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
        if it.is_entry and ticks["S"][first_at(ticks["S"], T["S"], t0)][0] - t0 > timedelta(seconds=30):
            eng.on_pair_done(it, _Out(ABORTED, {"S": 0, "H": 0}, {}, "기록 공백"), t0)
            eng.fail_count = 0
            continue
        if how == "market":
            eng.on_pair_done(it, fill_market(it, t0), t0)
            continue
        ls = next(l for l in it.legs if l.name == "S")
        lh = next(l for l in it.legs if l.name == "H")
        px, tf, why = try_limit_alarm(ticks["S"], T["S"], ticks["H"], T["H"], t0, ls.side, wait, optimistic, alarm_bp)
        key = "entry" if it.is_entry else "exit"
        st[f"{key}_try"] = st.get(f"{key}_try", 0) + 1
        st[f"{key}_{why}"] = st.get(f"{key}_{why}", 0) + 1
        if px is not None:
            # 역선택 측정: 체결 뒤 30초 삼성 중간값 변화(내 쪽 +)
            k = first_at(ticks["S"], T["S"], tf + timedelta(seconds=30))
            d = 1 if ls.side == BUY else -1
            st.setdefault("markout_bp", []).append(d * (mid(ticks["S"][k]) / px - 1) * 1e4)
            rh = ticks["H"][first_at(ticks["H"], T["H"], tf)]
            eng.on_pair_done(it, _Out(COMPLETE, {"S": ls.qty, "H": lh.qty}, {"S": px, "H": market_px(rh, lh.side)}), tf)
        elif it.is_entry:
            eng.on_pair_done(it, _Out(ABORTED, {"S": 0, "H": 0}, {}, why), tf)
            eng.fail_count = 0
        else:
            eng.on_pair_done(it, fill_market(it, tf), tf)
    if eng.direction:
        it = eng.force_exit(upto, "기록 끝")
        q = {k: ticks[k][-1] for k in ("S", "H")}
        eng.on_pair_done(it, _Out(COMPLETE, {l.name: l.qty for l in it.legs},
                                  {l.name: market_px(q[l.name], l.side) for l in it.legs}), upto)
    return j.trades


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--wait", type=float, default=30)
    ap.add_argument("--lag", type=float, default=2.5)
    ap.add_argument("--out", default=str(HERE / "results" / "limit_alarm"))
    a = ap.parse_args()
    cfg = json.loads(Path(a.strategy).read_text(encoding="utf-8"))
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in ("shadows", "monitor")}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    variants = [("시장가(지금)", "market", None, 0)]
    for opt in (False, True):
        tag = "낙관" if opt else "보수"
        variants.append((f"지정가·{tag}", "limit", opt, 0))
        for k in (5, 10, 15):
            variants.append((f"지정가+경보{k}bp·{tag}", "limit", opt, k))
    rows = []
    for r in a.runs:
        r = Path(r)
        tp = r if r.suffix == ".csv" else r / "ticks.csv"
        ticks = load_ticks(tp)
        day = str(ticks["S"][0][0].date())
        for name, how, opt, al in variants:
            st: dict = {}
            tr = run(ticks, cfg, how, a.wait, bool(opt), al, a.lag, st)
            mk = st.get("markout_bp", [])
            rows.append({"day": day, "variant": name, "trades": sum(1 for t in tr if t.get("part") == "final"),
                         "pnl": sum(t["net_pl"] for t in tr),
                         "entry_try": st.get("entry_try", ""), "entry_fill": st.get("entry_fill", ""),
                         "entry_alarm": st.get("entry_alarm", ""), "exit_fill": st.get("exit_fill", ""),
                         "exit_try": st.get("exit_try", ""),
                         "markout30_bp": round(statistics.mean(mk), 2) if mk else "", "fills": len(mk)})
        print(f"{day} 끝")
    with (out / "summary.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    days = sorted({r["day"] for r in rows})
    L = [f"# 삼성 지정가 + 하이닉스 경보 — 체결 기록 재현 ({', '.join(days)}, 대기 {a.wait:g}초)", "",
         "| 방식 | " + " | ".join(days) + " | 합계 | 지정가 체결 뒤 30초 삼성 움직임(평균 bp, +면 유리) | 진입 체결/시도 (경보 취소) |",
         "| --- |" + " --- |" * (len(days) + 3)]
    for name, *_ in variants:
        sel = [r for r in rows if r["variant"] == name]
        by = {r["day"]: r for r in sel}
        tot = sum(r["pnl"] for r in sel)
        mk_all = [(r["markout30_bp"], r["fills"]) for r in sel if r["markout30_bp"] != ""]
        mk = (sum(m * n for m, n in mk_all) / sum(n for _, n in mk_all)) if mk_all else None
        ef = sum(int(r["entry_fill"] or 0) for r in sel)
        et = sum(int(r["entry_try"] or 0) for r in sel)
        ea = sum(int(r["entry_alarm"] or 0) for r in sel)
        L.append(f"| {name} | " + " | ".join(f"{by[d]['trades']}건 {by[d]['pnl'] / 1e4:+.0f}만" for d in days)
                 + f" | **{tot / 1e4:+.0f}만** | {'—' if mk is None else f'{mk:+.1f}'} | "
                 + ("—" if how_is_market(name) else f"{ef}/{et} ({ea})") + " |")
    L += ["", "보수 = 내 가격을 관통해야 체결 · 낙관 = 같은 가격 체결이 나면 체결(대기열 맨 앞). 실제는 둘 사이."]
    (out / "report.txt").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


def how_is_market(name: str) -> bool:
    return name.startswith("시장가")


if __name__ == "__main__":
    main()
