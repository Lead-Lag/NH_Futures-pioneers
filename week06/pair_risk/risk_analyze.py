"""6주차 위험관리 — 값의 근거 분석과 「위험관리 넣기 전·후」 비교 (같은 기간 · 같은 체결 가정 · 같은 엔진).

1) 근거: 위험관리를 끈 규칙(R0)으로 돌린 포지션의 보유 중 평가손익 경로로
   - 손절선 X 원: 「평가손실이 X 에 닿은 포지션을 끝까지 들고 갔을 때 평균 결과」 vs 「X 에서 자른 결과」
   - 시간 청산 N 봉: 「N 봉째에도 안 끝난 포지션의 N 봉 시점 손익」 vs 「끝까지 들고 간 결과」
   - 분할 진입 z: 진입 뒤 z 가 2.5 · 3.0 까지 더 벌어진 비율과 그 포지션의 결과
2) 비교: 같은 날들 · 같은 비용으로 R0(없음) · R1(4주차 규칙) · R2(6주차 위험관리) 와 후보들을 돌려
   하루 손익 · 최악의 날 · 최대 낙폭 · 거래당 최악 손실을 나란히.

예)
  python risk_analyze.py                                  # data_dec · data_nov (9거래일, β 0.59 고정)
  python risk_analyze.py --data hist_nov hist_dec --walk  # 24거래일 워크포워드 β (4주차 3-8절과 같은 날)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from pair_engine import PairEngine, PairParams, align_minutes, tick_size
from pair_walkforward import _Fill, _J, bootstrap, day_returns, ols_beta, pick_days

HERE = Path(__file__).resolve().parent

COSTS = {"0틱": {"S": 0, "H": 0}, "0.5틱": {"S": 0.5, "H": 0.5}, "실측(삼성1·하이닉스0.5)": {"S": 1, "H": 0.5}}
MAIN_COST = "실측(삼성1·하이닉스0.5)"

# R0 = 위험관리 없음(진입·수렴 청산·장 마감만), R1 = 4주차 규칙 그대로, R2 = 6주차(strategy_config.json)
R0 = {"risk_off": True}


def run_day(cfg: dict, beta: float, bs, bh, slip: dict, paths: list | None = None):
    """하루. paths 를 주면 포지션마다 {entry_z, path:[봉마다 평가손익], zs:[봉마다 z], net} 를 모음."""
    p = PairParams.from_dict({**cfg, "beta": beta})
    j = _J()
    eng = PairEngine(p, j)
    eng.warmup = False
    ticks = {"S": p.tick_s, "H": p.tick_h}
    last: dict[str, float] = {}
    upto = max(bs[-1]["start"], bh[-1]["start"]) + timedelta(minutes=1)
    cur = None
    n_tr = 0
    for pb in align_minutes(bs, bh, None, upto, last):
        ts = pb["start"] + timedelta(minutes=1)
        it = eng.on_bar_close(pb)
        if cur is not None and eng.direction:
            cur["path"].append(eng.mtm(pb["close_s"], pb["close_h"]))
            cur["zs"].append(eng.last_z)
        if it is None and eng.direction and not eng.pending:
            it = eng.on_prices(pb["close_s"], pb["close_h"], ts)
        if it is not None:
            eng.on_pair_done(it, _Fill(it, {"S": pb["close_s"], "H": pb["close_h"]}, ticks, slip), ts)
        if paths is not None:
            if eng.direction and cur is None:
                cur = {"entry_z": eng.entry_z, "dir": eng.direction, "path": [], "zs": [],
                       "px_s": eng.entry_px["S"], "px_h": eng.entry_px["H"],
                       "q_s": eng.open_qty["S"], "q_h": eng.open_qty["H"],
                       "sd": eng.entry_sd, "notional": eng.open_qty["S"] * eng.p.mult_s * eng.entry_px["S"]}
            if len(j.trades) > n_tr:
                n_tr = len(j.trades)
                t = j.trades[-1]
                if t.get("part") == "final" and cur is not None:
                    cur.update(net=t["pos_net"], reason=t["exit_reason"], held=t["held_bars"])
                    paths.append(cur)
                    cur = None
    if eng.direction:
        it = eng.force_exit(upto, "그날 마지막 봉")
        eng.on_pair_done(it, _Fill(it, dict(last), ticks, slip), upto)
        if paths is not None and cur is not None:
            t = j.trades[-1]
            cur.update(net=t["pos_net"], reason=t["exit_reason"], held=t["held_bars"])
            paths.append(cur)
    return j.trades, j.signals


def days_and_betas(folders: list[Path], walk: bool, beta_fixed: float, min_fresh: float):
    chosen, _ = pick_days(folders, min_fresh)
    out = []
    rets = {d: day_returns(bs, bh) for d, _, bs, bh, _ in chosen}
    for i, (d, src, bs, bh, _) in enumerate(chosen):
        if walk:
            hist = [rets[x[0]] for x in chosen[max(0, i - 20):i]]
            if len(hist) < 5:
                continue
            beta = ols_beta([x for h in hist for x in h[0]], [y for h in hist for y in h[1]])
            if math.isnan(beta):
                continue
        else:
            beta = beta_fixed
        out.append((d, src, beta, bs, bh))
    return out


def exit_cost(x: dict) -> float:
    """그 포지션을 시장가로 청산할 때의 비용(실측: 삼성 1칸 · 하이닉스 0.5칸, 진입 가격의 호가단위)."""
    return x["q_s"] * 10 * tick_size(x["px_s"], 100) * 1.0 + x["q_h"] * 10 * tick_size(x["px_h"], 1000) * 0.5


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def max_dd(vals):
    peak = cum = dd = 0.0
    for v in vals:
        cum += v
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def f0(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.0f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", nargs="+", default=["data_dec", "data_nov"])
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--walk", action="store_true", help="β 를 직전 20거래일로 날마다 다시(없으면 설정 β 고정)")
    ap.add_argument("--min-fresh", type=float, default=60.0)
    ap.add_argument("--out")
    a = ap.parse_args()

    cfg = json.loads(Path(a.strategy).read_text(encoding="utf-8"))
    base = {k: v for k, v in cfg.items() if not k.startswith("_") and k not in ("shadows", "monitor")}
    week4 = {**base, **cfg.get("shadows", {}).get("R1_4주차규칙", {})}
    configs = {
        "R0 위험관리 없음": {**base, **R0},
        "R1 4주차 규칙": week4,
        "R2 6주차 위험관리": base,
    }
    # 후보 — 규칙 하나씩만 4주차에 더해 효과를 따로 봄
    cands = {
        "C1 4주차+연속손실3": {**week4, "max_consec_losses": 3},
        "C1a 연속손실2": {**week4, "max_consec_losses": 2},
        "C1b 연속손실4": {**week4, "max_consec_losses": 4},
        "C2 4주차+시간청산30": {**week4, "max_hold_bars": 30},
        "C2a 시간청산20": {**week4, "max_hold_bars": 20},
        "C3 4주차+분할진입(2.5)": {**week4, "max_units": 2, "add_z": 2.5, "stop_per_unit": True},
        "C4 2단위+분할청산(1.0)": {**week4, "init_units": 2, "max_units": 2, "partial_exit_z": 1.0,
                               "stop_per_unit": True},
        "C5 금액손절10만": {**week4, "pair_stop_won": 100_000},
        "C5a 금액손절20만": {**week4, "pair_stop_won": 200_000},
    }
    for nb in (1, 3, 5):
        cands[f"S1 R2+하이닉스 선행({nb}봉)"] = {**base, "speed_filter": "lead_h", "speed_bars": nb}
        cands[f"S1x R2+삼성 선행({nb}봉)"] = {**base, "speed_filter": "lead_s", "speed_bars": nb}
    for nb, jz in ((1, 1.5), (3, 2.0)):
        cands[f"S2 R2+급변만({nb}봉 dz≥{jz})"] = {**base, "speed_filter": "fast", "speed_bars": nb, "jump_z": jz}
        cands[f"S2x R2+완만만({nb}봉 dz<{jz})"] = {**base, "speed_filter": "slow", "speed_bars": nb, "jump_z": jz}
    folders = [HERE / x if not Path(x).is_absolute() else Path(x) for x in a.data]
    days = days_and_betas(folders, a.walk, float(base.get("beta", 0.59)), a.min_fresh)
    out = Path(a.out) if a.out else HERE / "results" / f"risk_{'walk' if a.walk else 'fixed'}_{len(days)}d"
    out.mkdir(parents=True, exist_ok=True)
    L = [f"# 6주차 위험관리 — 근거와 전·후 비교 ({len(days)}거래일 {days[0][0]}~{days[-1][0]})", "",
         f"데이터 {', '.join(f.name for f in folders)} · β {'직전 20거래일 워크포워드' if a.walk else f'{base.get("beta")} 고정'} · "
         f"체결 = 판정 봉 종가 ± 다리마다 비용(가격별 호가단위) · 금액 손절은 봉 종가로 판정 · 같은 pair_engine.py", ""]

    # ---------------- 1) 근거: R0 포지션 경로
    paths: list[dict] = []
    for d, src, beta, bs, bh in days:
        pp: list[dict] = []
        run_day(configs["R0 위험관리 없음"], beta, bs, bh, COSTS[MAIN_COST], pp)
        for x in pp:
            x["date"] = d
        paths += pp
    nets = [x["net"] for x in paths]
    wins = [x for x in paths if x["net"] > 0]
    L += ["## 1. 값의 근거 — 위험관리 없음(R0) 포지션 " + f"{len(paths)}개 (비용 {MAIN_COST})", "",
          f"평균 {f0(statistics.mean(nets) if nets else 0)}원 · 이긴 포지션 {len(wins)} · "
          f"보유 봉 중앙 {f0(pct([x['held'] for x in paths], .5))} · 90% {f0(pct([x['held'] for x in paths], .9))} · "
          f"최대 {max([x['held'] for x in paths] or [0])}", ""]
    mae_w = [min([0] + x["path"]) for x in wins]
    L += ["**이긴 포지션이 중간에 밀렸던 폭(MAE, 봉 종가 기준)** — "
          + " · ".join(f"{int(q*100)}% {f0(pct(mae_w, 1-q))}원" for q in (.5, .75, .9, .95)), ""]
    L += ["**손절선 후보** — 평가손실이 X 에 닿은 포지션: 끝까지 들고 갔을 때 vs 닿은 봉에서 잘랐을 때(청산 비용 포함)", "",
          "| X(원) | 닿은 포지션 | 그중 결국 이김 | 끝까지 평균 | 그 자리 청산 평균 | 자르면 포지션당 |", "| --- | --- | --- | --- | --- | --- |"]
    stop_rows = []
    for X in (50_000, 75_000, 100_000, 125_000, 150_000, 200_000, 250_000):
        hit = [x for x in paths if min([0] + x["path"]) <= -X]
        if not hit:
            stop_rows.append((X, 0, 0, None, None, None))
            continue
        held = statistics.mean(x["net"] for x in hit)
        cut_vals = []
        for x in hit:
            k = next(i for i, m in enumerate(x["path"]) if m <= -X)
            cut_vals.append(x["path"][k] - exit_cost(x))
        cut = statistics.mean(cut_vals)
        stop_rows.append((X, len(hit), sum(1 for x in hit if x["net"] > 0), held, cut, cut - held))
        L.append(f"| {X:,} | {len(hit)} | {sum(1 for x in hit if x['net'] > 0)} | {f0(held)} | {f0(cut)} | **{f0(cut - held)}** |")
    L += ["", "(청산 비용 = 포지션마다 진입 가격의 호가단위로 삼성 1칸 + 하이닉스 0.5칸(4주차 실측) — 삼성 20만원 이상이면 5.5만원, 미만이면 1.5만원. 평가손익은 진입 체결가 기준이라 진입 비용은 이미 들어 있음. 봉 종가 경로라 실제 손절은 봉 안에서 더 일찍 닿음)", ""]

    L += ["**시간 청산 후보** — N 봉째에도 열려 있던 포지션: N 봉에서 잘랐을 때 vs 끝까지", "",
          "| N(봉) | 남은 포지션 | N 봉 청산 평균 | 끝까지 평균 | 자르면 포지션당 |", "| --- | --- | --- | --- | --- |"]
    time_rows = []
    for N in (10, 15, 20, 30, 45, 60, 90):
        left = [x for x in paths if len(x["path"]) > N]
        if not left:
            time_rows.append((N, 0, None, None, None))
            continue
        cut = statistics.mean(x["path"][N - 1] - exit_cost(x) for x in left)
        held = statistics.mean(x["net"] for x in left)
        time_rows.append((N, len(left), cut, held, cut - held))
        L.append(f"| {N} | {len(left)} | {f0(cut)} | {f0(held)} | **{f0(cut - held)}** |")
    L.append("")

    L += ["**분할 진입 근거** — 진입(|z|≥2) 뒤 z 가 진입 방향으로 더 벌어진 비율과 그 포지션의 결과", "",
          "| 더 벌어진 |z| | 포지션 | 비율 | 평균 결과 | 이긴 비율 |", "| --- | --- | --- | --- | --- |"]
    for k in (2.5, 3.0, 3.5):
        ext = [x for x in paths if any(z is not None and -x["dir"] * z >= k for z in x["zs"])]
        if ext:
            L.append(f"| {k} | {len(ext)} | {100*len(ext)/len(paths):.0f}% | {f0(statistics.mean(x['net'] for x in ext))} | "
                     f"{100*sum(1 for x in ext if x['net'] > 0)/len(ext):.0f}% |")
    L.append("")
    with (out / "r0_positions.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "dir", "entry_z", "held", "reason", "net", "mae_close", "mfe_close", "max_abs_z"])
        for x in paths:
            zs = [abs(z) for z in x["zs"] if z is not None]
            w.writerow([x["date"], x["dir"], x["entry_z"], x["held"], x["reason"], x["net"],
                        round(min([0] + x["path"])), round(max([0] + x["path"])), round(max(zs), 2) if zs else ""])

    # ---------------- 2) 전·후 비교
    allc = {**configs, **cands}
    rows = []
    daily_tbl = {}
    for cost_name, slip in COSTS.items():
        for name, c in allc.items():
            dv, trs = [], []
            for d, src, beta, bs, bh in days:
                tr, _ = run_day(c, beta, bs, bh, slip)
                dv.append(sum(t["net_pl"] for t in tr))
                trs += tr
                daily_tbl.setdefault((cost_name, name), []).append((d, dv[-1], len([t for t in tr if t.get("part") == "final"])))
            pos = [t["pos_net"] for t in trs if t.get("part") == "final"]
            lo, hi = bootstrap(dv)
            rows.append({"cost": cost_name, "config": name, "positions": len(pos), "orders_rt": len(trs),
                         "total": sum(dv), "day_mean": statistics.mean(dv), "lo": lo, "hi": hi,
                         "day_sd": statistics.pstdev(dv), "worst_day": min(dv), "mdd": max_dd(dv),
                         "worst_pos": min(pos) if pos else 0, "pos_mean": statistics.mean(pos) if pos else 0,
                         "win": sum(1 for x in pos if x > 0)})
    for cost_name in COSTS:
        L += [f"## 2. 전·후 비교 — 비용 {cost_name}", "",
              "| 규칙 | 포지션 | 순손익 | 하루 평균 (95%) | 하루 표준편차 | 최악의 날 | 최대 낙폭 | 포지션 최악 | 포지션 평균 | 이긴 포지션 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            if r["cost"] != cost_name:
                continue
            L.append(f"| {r['config']} | {r['positions']} | {f0(r['total'])} | {f0(r['day_mean'])} ({f0(r['lo'])} ~ {f0(r['hi'])}) | "
                     f"{f0(r['day_sd'])} | {f0(r['worst_day'])} | {f0(r['mdd'])} | {f0(r['worst_pos'])} | {f0(r['pos_mean'])} | {r['win']} |")
        L.append("")
    L += ["## 3. 날짜별 (비용 " + MAIN_COST + ")", "",
          "| 날짜 | " + " | ".join(allc) + " |", "| --- |" + " --- |" * len(allc)]
    for i, (d, *_rest) in enumerate(days):
        L.append(f"| {d} | " + " | ".join(f"{f0(daily_tbl[(MAIN_COST, n)][i][1])} ({daily_tbl[(MAIN_COST, n)][i][2]})"
                                          for n in allc) + " |")
    L += ["", "(괄호 = 그날 포지션 수)", ""]
    with (out / "compare.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (out / "risk_report.txt").write_text("\n".join(L), encoding="utf-8")
    json.dump({"stop": stop_rows, "time": time_rows}, (out / "evidence.json").open("w", encoding="utf-8"),
              ensure_ascii=False, default=str)
    print("\n".join(L))


if __name__ == "__main__":
    main()
