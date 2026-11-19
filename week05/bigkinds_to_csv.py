#!/usr/bin/env python3
"""빅카인즈 뉴스 다운로드 엑셀(.xlsx) 여러 개 → kdi_bottleneck.py 입력 CSV(date, issue_rank, issue_title, news_title).

빅카인즈에는 KDI 의 이슈 순위가 없으므로 issue_rank = 1(모든 기사 같은 가중치), issue_title = "" 로 둔다.
→ 점수는 「병목어와 같은 제목에 함께 나온 기사 수」가 되고, TOP10 이슈 점유율 기반 과열 익절은 작동하지 않는다.

사용:
  python bigkinds_to_csv.py data/bigkinds/*.xlsx --out data/bigkinds_news.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

DATE_COLS = ["일자", "날짜", "date"]
TITLE_COLS = ["제목", "title"]


def pick(cols, cands):
    for c in cands:
        if c in cols:
            return c
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()

    frames = []
    for f in a.files:
        df = pd.read_excel(f, dtype=str) if f.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(f, dtype=str, encoding="utf-8-sig")
        dc, tc = pick(df.columns, DATE_COLS), pick(df.columns, TITLE_COLS)
        if not dc or not tc:
            sys.exit(f"[오류] {f.name}: 날짜·제목 열을 찾지 못했습니다. 열 목록: {list(df.columns)}")
        d = pd.to_datetime(df[dc].str.replace(r"\D", "", regex=True).str[:8], format="%Y%m%d", errors="coerce")
        part = pd.DataFrame({"date": d, "news_title": df[tc].fillna("").str.strip(), "source_file": f.name})
        frames.append(part)
        print(f"{f.name}: {len(part)}건 · {d.min():%Y-%m-%d} ~ {d.max():%Y-%m-%d}")

    out = pd.concat(frames, ignore_index=True)
    bad = out["date"].isna() | (out["news_title"] == "")
    if bad.any():
        print(f"[경고] 날짜·제목이 비어 있는 {int(bad.sum())}건 제외", file=sys.stderr)
        out = out[~bad]
    before = len(out)
    out = out.drop_duplicates(["date", "news_title"])          # 같은 날 같은 제목(여러 언론사 전재) 1건으로
    print(f"중복 제목 {before - len(out)}건 제외 → {len(out)}건")
    out["issue_rank"] = 1
    out["issue_title"] = ""
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    out[["date", "issue_rank", "issue_title", "news_title", "source_file"]].sort_values("date") \
        .to_csv(a.out, index=False, encoding="utf-8-sig")
    print("월별 건수:\n" + out.groupby(out["date"].str[:7]).size().to_string())
    print(f"저장: {a.out}")


if __name__ == "__main__":
    main()
