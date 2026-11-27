"""「속도」 — 초 단위 선행·후행 분석 (4주차 실시간 체결 기록 ticks.csv).

질문: 한 종목의 호가 중간값이 몇 초 안에 크게 움직이면, 다른 종목이 그 뒤 몇 초 안에 따라오는가?
     따라온다면 그 폭이 호가 차(시장가로 사고팔 때 내는 비용)를 넘는가?

방법
  1) 두 종목 호가 중간값 (매수1호가+매도1호가)/2 을 수신 시각 기준 1초 격자로(직전 값 유지)
  2) 시차 상관: 1초 수익률 corr(선행 종목 t, 후행 종목 t+L), L = −10~+10초
  3) 사건 연구: 선행 종목 중간값이 W 초 동안 k bp 이상 움직이고, 후행 종목은 그동안 거의 안 움직인(± 2bp) 순간
     → 후행 종목 중간값이 그 뒤 h 초 동안 같은 방향으로 움직인 폭(β 로 조정한 기대 폭과 비교)
  4) 전략 재현: 그 순간 후행 종목을 시장가로(사면 매도호가, 팔면 매수호가) 1계약, h 초 뒤 시장가 청산.
     지연 0.5초(신호 → 주문). 겹치는 사건은 앞 거래가 끝난 뒤에만. 비용 0(중간값↔중간값)과 실제(호가) 손익.

  python sec_leadlag.py ../../week04/results/pair_paper_20261111_090158 ../../week04/results/pair_paper_20261112_085733 \
                        ../../week04/results/pair_paper_20261113_093639
결과: results/sec_leadlag/ (report.txt · events.csv · xcorr.csv)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BETA = 0.59          # 삼성 수익률 ≈ 0.59 × 하이닉스 수익률 (4주차)
MULT = 10            # 계약 승수(주)
LAG_SEC = 0.5        # 신호 → 주문 지연


def load(folder: Path) -> pd.DataFrame:
    df = pd.read_csv(folder / "ticks.csv", encoding="utf-8-sig")
    df["t"] = pd.to_datetime(df["recv_ts"])
    df = df[(df["bid"] > 0) & (df["ask"] > 0) & (df["ask"] >= df["bid"])]
    tod = df["t"].dt.strftime("%H:%M:%S")
    df = df[(tod >= "09:05:00") & (tod <= "15:15:00")]
    df["mid"] = (df["bid"] + df["ask"]) / 2
    return df


def grid(df: pd.DataFrame, freq: str = "1s") -> pd.DataFrame:
    """종목별 bid·ask·mid 를 1초 격자로. 실행이 멈춘 구간(10초 넘게 두 종목 모두 수신 없음)은 빼기 위해 표시."""
    out = {}
    for leg in ("S", "H"):
        g = df[df["leg"] == leg].set_index("t")[["bid", "ask", "mid"]]
        g = g[~g.index.duplicated(keep="last")]
        out[leg] = g.resample(freq).last()
    idx = out["S"].index.union(out["H"].index)
    S = out["S"].reindex(idx)
    H = out["H"].reindex(idx)
    alive = (~S["mid"].isna()) | (~H["mid"].isna())
    gap = (~alive).astype(int).groupby(alive.cumsum()).cumsum()       # 연속 무수신 초
    S, H = S.ffill(), H.ffill()
    g = pd.DataFrame({"bid_s": S["bid"], "ask_s": S["ask"], "mid_s": S["mid"],
                      "bid_h": H["bid"], "ask_h": H["ask"], "mid_h": H["mid"], "dead": gap > 10}).dropna()
    # 날짜가 바뀌는 곳·하루 시작 부분 정리
    return g


def xcorr(g: pd.DataFrame, lags=range(-10, 11)) -> pd.DataFrame:
    rs = np.log(g["mid_s"]).diff()
    rh = np.log(g["mid_h"]).diff()
    ok = ~g["dead"]
    rows = []
    for L in lags:
        # L>0: 하이닉스가 L 초 먼저 (corr(rh_t, rs_{t+L}))
        a = rh.where(ok)
        b = rs.shift(-L).where(ok.shift(-L, fill_value=False))
        m = a.notna() & b.notna()
        rows.append({"lag_sec": L, "corr": round(float(np.corrcoef(a[m], b[m])[0, 1]), 4), "n": int(m.sum())})
    return pd.DataFrame(rows)


def events(g: pd.DataFrame, lead: str, W: int, k_bp: float, quiet_bp: float, horizons, day: str) -> list[dict]:
    lag = "S" if lead == "H" else "H"
    scale = BETA if lead == "H" else 1 / BETA          # 후행 종목의 기대 반응 = scale × 선행 움직임
    ml, mf = np.log(g[f"mid_{lead.lower()}"].to_numpy()), np.log(g[f"mid_{lag.lower()}"].to_numpy())
    bid = g[f"bid_{lag.lower()}"].to_numpy()
    ask = g[f"ask_{lag.lower()}"].to_numpy()
    mid = g[f"mid_{lag.lower()}"].to_numpy()
    dead = g["dead"].to_numpy()
    n = len(g)
    lag_steps = int(np.ceil(LAG_SEC))   # 1초 격자에서 0.5초 지연 → 다음 초 호가로 체결(보수적)
    H = max(horizons)
    out = []
    busy_until = {h: -1 for h in horizons}
    i = W
    while i < n - H - lag_steps:
        if dead[i - W:i + H + lag_steps + 1].any():
            i += 1
            continue
        move = (ml[i] - ml[i - W]) * 1e4
        fol = (mf[i] - mf[i - W]) * 1e4
        if abs(move) >= k_bp and abs(fol) <= quiet_bp:
            d = 1 if move > 0 else -1
            e = i + lag_steps
            row = {"day": day, "lead": lead, "t": str(g.index[i]), "lead_move_bp": round(move, 1),
                   "follow_before_bp": round(fol, 1), "expected_bp": round(scale * abs(move), 1),
                   "spread_won": ask[e] - bid[e]}
            for h in horizons:
                x = e + h
                row[f"follow_{h}s_bp"] = round(d * (mf[x] - mf[e]) * 1e4, 2)
                gross = d * (mid[x] - mid[e]) * MULT
                net = (bid[x] - ask[e]) * MULT if d > 0 else (bid[e] - ask[x]) * MULT
                row[f"gross_{h}s"] = gross
                row[f"net_{h}s"] = net
                row[f"take_{h}s"] = int(i > busy_until[h])
                if i > busy_until[h]:
                    busy_until[h] = x
            out.append(row)
            i += 1
        else:
            i += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default=str(HERE / "results" / "sec_leadlag"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    horizons = (1, 2, 5, 10, 30, 60)
    L = ["# 초 단위 선행·후행 — 4주차 실시간 체결 기록", ""]
    xs, evs = [], []
    for r in a.runs:
        r = Path(r)
        df = load(r)
        day = str(df["t"].iloc[0].date())
        g = grid(df)
        sp_s = (g["ask_s"] - g["bid_s"])
        sp_h = (g["ask_h"] - g["bid_h"])
        L.append(f"- {day}: 체결 기록 {len(df):,}건 · 1초 격자 {len(g):,}초 (멈춘 구간 {int(g['dead'].sum()):,}초 제외) · "
                 f"호가 차 중앙 삼성 {sp_s.median():,.0f}원({1e4 * (sp_s / g['mid_s']).median():.1f}bp) · "
                 f"하이닉스 {sp_h.median():,.0f}원({1e4 * (sp_h / g['mid_h']).median():.1f}bp)")
        x = xcorr(g)
        x["day"] = day
        xs.append(x)
        for lead in ("H", "S"):
            for W, k in ((1, 8), (3, 8), (3, 15), (5, 15), (5, 25)):
                for e in events(g, lead, W, k, 2.0, horizons, day):
                    e.update({"W": W, "k_bp": k})
                    evs.append(e)
    X = pd.concat(xs)
    X.to_csv(out / "xcorr.csv", index=False, encoding="utf-8-sig")
    E = pd.DataFrame(evs)
    E.to_csv(out / "events.csv", index=False, encoding="utf-8-sig")

    piv = X.pivot_table(index="lag_sec", columns="day", values="corr")
    piv["평균"] = piv.mean(axis=1)
    L += ["", "## 1. 시차 상관 — corr(하이닉스 1초 수익률 t, 삼성 1초 수익률 t+L). L>0 = 하이닉스가 먼저", "",
          "| L(초) | " + " | ".join(piv.columns) + " |", "| --- |" + " --- |" * len(piv.columns)]
    for lag_s, row in piv.iterrows():
        L.append(f"| {lag_s:+d} | " + " | ".join(f"{v:+.3f}" for v in row) + " |")
    L.append("")
    L += ["## 2. 사건 연구 — 선행 종목이 W 초 동안 k bp 이상 움직이고 후행 종목은 ±2bp 안에 있던 순간",
          "", "후행 종목의 그 뒤 움직임(같은 방향 +, bp) 평균 · 기대 폭 = β 조정 선행 움직임", "",
          "| 선행 | W·k | 사건 | 기대 폭 | 1초 | 2초 | 5초 | 10초 | 30초 | 60초 | 후행 호가 차 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for (lead, W, k), d in E.groupby(["lead", "W", "k_bp"]):
        mids = d["spread_won"] / 1  # 원
        L.append(f"| {'하이닉스' if lead == 'H' else '삼성'} | {W}초·{k}bp | {len(d)} | {d['expected_bp'].mean():.1f} | "
                 + " | ".join(f"{d[f'follow_{h}s_bp'].mean():+.1f}" for h in horizons)
                 + f" | {mids.median():,.0f}원 |")
    L.append("")
    L += ["## 3. 전략 재현 — 후행 종목 1계약 시장가 진입(0.5초 지연 → 다음 초 호가), h 초 뒤 시장가 청산, 겹침 없음", "",
          "| 선행 | W·k | h | 거래 | 비용 0 평균(원/계약) | 실제 평균(원/계약) | 실제 합계 | 이긴 비율(실제) |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for (lead, W, k), d in E.groupby(["lead", "W", "k_bp"]):
        for h in (2, 5, 10, 30):
            t = d[d[f"take_{h}s"] == 1]
            if t.empty:
                continue
            L.append(f"| {'하이닉스→삼성' if lead == 'H' else '삼성→하이닉스'} | {W}초·{k}bp | {h}초 | {len(t)} | "
                     f"{t[f'gross_{h}s'].mean():+,.0f} | {t[f'net_{h}s'].mean():+,.0f} | {t[f'net_{h}s'].sum():+,.0f} | "
                     f"{100 * (t[f'net_{h}s'] > 0).mean():.0f}% |")
    L.append("")
    (out / "report.txt").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    sys.exit(main())
