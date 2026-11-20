#!/usr/bin/env python3
"""공급계약 공시 이벤트 스터디 — 「공시가 나면 바로 산다」 는 규칙에 초과수익이 있는가.

규칙(결과를 보기 전에 고정):
  - 대상: dart_collect.py contracts 결과 중 원래 공시(정정 · 거래정지 공지 제외), 상장 종목.
  - 진입: 공시 **다음 거래일 시가**. 공시에는 날짜만 있고 시각이 없어(장중 공시 가능) 당일 진입은 미래 정보가 섞일 수 있음.
  - 보유: 1 · 5 · 21 · 63 거래일, 왕복 비용 0.3%. 코스피 대비 초과수익, 코스피200 대비(헤지) 함께.
  - 공시일 반응: 공시 전날 종가 → 공시일 종가(코스피 대비). 시장이 공시를 얼마나 빨리 반영하는지 보여 주는 참고값(매매 불가).
  - 계약 규모(매출액 대비 %)로 나눠 봄: <10 · 10~30 · 30~100 · ≥100.
  - 같은 종목이 같은 날 여러 건 공시하면 1건으로 본다.

사용(Colab):
  python contract_event.py --contracts results/dart/contracts_2023-04_2024-11.csv --out results/event_contracts
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kdi_bottleneck import get_prices  # noqa: E402  (같은 가격 캐시 results/prices 사용)

HORIZONS = [1, 5, 21, 63]
COST = 0.003
BUCKETS = [(-np.inf, 10, "<10%"), (10, 30, "10~30%"), (30, 100, "30~100%"), (100, np.inf, "≥100%")]


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 1e-9 else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contracts", required=True, type=Path)
    ap.add_argument("--prices", default=Path("results/prices"), type=Path)
    ap.add_argument("--out", default=Path("results/event_contracts"), type=Path)
    ap.add_argument("--today", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    a = ap.parse_args()

    c = pd.read_csv(a.contracts, dtype=str, encoding="utf-8-sig").fillna("")
    n0 = len(c)
    c = c[(c["is_correction"].str.lower() != "true") & ~c["report_nm"].str.contains("거래정지") & (c["stock_code"] != "")].copy()
    c["date"] = pd.to_datetime(c["rcept_dt"], format="%Y%m%d", errors="coerce")
    c["code"] = c["stock_code"].str.zfill(6)
    c["ratio"] = pd.to_numeric(c["sales_ratio"].str.replace(",", "").str.extract(r"(-?\d+\.?\d*)")[0], errors="coerce")
    c = c.dropna(subset=["date"]).sort_values("date").drop_duplicates(["code", "date"])
    print(f"공시 {n0}건 → 원래 공시 · 종목 · 날짜 중복 제거 후 {len(c)}건", file=sys.stderr)

    start = (c["date"].min() - pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    end = a.today
    kospi, k200 = get_prices("KS11", start, end, a.prices), get_prices("KS200", start, end, a.prices)
    cal = kospi.index
    rows, miss = [], 0
    codes = c["code"].unique()
    for i, code in enumerate(codes, 1):
        px = get_prices(code, start, end, a.prices)
        if i % 100 == 0:
            print(f"  가격 {i}/{len(codes)}", file=sys.stderr)
        for r in c[c["code"] == code].itertuples():
            j = cal.searchsorted(r.date, side="right")          # 공시일 다음 거래일
            if px is None or j >= len(cal) or j < 2:
                miss += 1
                continue
            d0, entry = cal[j - 1] if cal[j - 1] == r.date else None, cal[j]
            row = {"rcept_no": r.rcept_no, "date": r.date.date(), "code": code, "name": r.corp_name, "market": r.corp_cls,
                   "counterparty": r.counterparty, "contract": r.contract_name, "ratio": r.ratio, "entry": entry.date()}
            # 공시일 반응(참고, 매매 불가): 공시 전날 종가 → 공시일 종가
            if d0 is not None and d0 in px.index:
                prev = cal[cal.get_loc(d0) - 1]
                if prev in px.index and px.loc[prev, "Close"] > 0:
                    row["day0"] = px.loc[d0, "Close"] / px.loc[prev, "Close"] - 1 - (kospi.loc[d0, "Close"] / kospi.loc[prev, "Close"] - 1)
            if entry not in px.index or not px.loc[entry, "Open"] > 0:
                miss += 1
                continue
            o = px.loc[entry, "Open"]
            for H in HORIZONS:
                k = j + H - 1
                if k >= len(cal):
                    continue
                ex = cal[k]
                if ex not in px.index:
                    continue
                ret = px.loc[:ex, "Close"].replace(0, np.nan).ffill().iloc[-1] / o - 1 - COST
                row[f"ret{H}"] = ret
                row[f"ex{H}"] = ret - (kospi.loc[ex, "Close"] / kospi.loc[entry, "Open"] - 1)
                if k200 is not None and entry in k200.index and ex in k200.index:
                    row[f"hedge{H}"] = ret - (k200.loc[ex, "Close"] / k200.loc[entry, "Open"] - 1)
            rows.append(row)
    ev = pd.DataFrame(rows)
    a.out.mkdir(parents=True, exist_ok=True)
    ev.to_csv(a.out / "events.csv", index=False, encoding="utf-8-sig")

    def block(df: pd.DataFrame, label: str) -> list[str]:
        out = [f"### {label} — 공시 {len(df)}건"]
        if "day0" in df:
            d = df["day0"].dropna()
            out.append(f"- 공시일 반응(전날 종가→공시일 종가, 코스피 대비, 매매 불가): 평균 {d.mean():+.2%} · 중앙값 {d.median():+.2%} · t {tstat(d):+.1f} · n {len(d)}")
        for H in HORIZONS:
            col = f"hedge{H}" if f"hedge{H}" in df else f"ex{H}"
            if col not in df:
                continue
            x = df[col].dropna()
            if len(x) == 0:
                continue
            out.append(f"- 다음 날 시가 진입 · {H:>2}거래일 보유: 코스피200 대비 평균 {x.mean():+.2%} · 중앙값 {x.median():+.2%} · "
                       f"t {tstat(x):+.1f} · 플러스 {(x > 0).mean():.0%} · n {len(x)} (비용 0.3% 반영)")
        return out + [""]

    lines = ["# 공급계약 공시 이벤트 스터디", f"(원래 공시만, 진입 = 공시 다음 거래일 시가, 가격 없음 · 기간 부족 {miss}건 제외)", ""]
    lines += block(ev, "전체")
    for lo, hi, name in BUCKETS:
        lines += block(ev[(ev["ratio"] >= lo) & (ev["ratio"] < hi)], f"계약 규모 매출액 대비 {name}")
    for mk, name in (("Y", "유가증권"), ("K", "코스닥")):
        lines += block(ev[ev["market"] == mk], name)
    lines.append("주의: 같은 기간 공시가 몰리면 수익률이 서로 겹쳐 실제 불확실성은 t값보다 큼. 탐색 결과이며 최종 검증 아님.")
    text = "\n".join(lines)
    (a.out / "summary.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
