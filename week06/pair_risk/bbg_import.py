"""블룸버그에서 받은 1분봉·틱 파일을 이 저장소 형식으로 바꿉니다.

1분봉  파일 이름에 종목(S·삼성·Samsung / H·하이닉스·Hynix)과 결제월(YYYYMM 또는 YYMM)이 들어 있으면 됩니다. 예) S_202609.csv
       → bbg_<YYYYMM>/bars_S.csv · bars_H.csv · meta.json  (pair_walkforward·risk_analyze·risk_regime 가 그대로 읽음)
틱     파일 이름에 tick 이 들어 있고 종목·날짜(YYYYMMDD)가 있으면 됩니다. 예) tick_S_20261112.csv
       → bbg_ticks_<YYYYMMDD>/ticks.csv  (sec_leadlag.py 가 그대로 읽음 — 체결·매수1호가·매도1호가를 시각 순으로)

열 이름은 자동으로 찾습니다(대소문자·한글 무관): 날짜/시각(Date·Time·Dates), 시가(Open), 고가(High), 저가(Low),
종가(Close·Last·Last Price), 거래량(Volume) / 틱: 종류(Type·Event — TRADE·BID·ASK), 가격(Value·Price), 수량(Size).
시각이 한국 시간이 아니면 --tz-shift 로 맞춥니다(예: 런던 시간이면 +9, 뉴욕이면 +14 → 실제 차이는 서머타임 확인).

  python bbg_import.py <받은 파일 폴더> [--tz-shift 0]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
LEG_PAT = {"S": re.compile(r"(^|[_\-\s])(S|삼성|samsung|ssnlf|005930)([_\-\s.]|$)", re.I),
           "H": re.compile(r"(^|[_\-\s])(H|하이닉스|hynix|000660)([_\-\s.]|$)", re.I)}


def leg_of(name: str) -> str | None:
    for leg, p in LEG_PAT.items():
        if p.search(name):
            return leg
    return None


def month_of(name: str) -> str | None:
    m = re.search(r"(20\d{2})(0[1-9]|1[0-2])(?!\d)", name)
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r"(?<!\d)(\d{2})(0[1-9]|1[0-2])(?!\d)", name)
    return f"20{m.group(1)}{m.group(2)}" if m else None


def read_any(p: Path) -> pd.DataFrame:
    if p.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(p)
    else:
        df = None
        for enc in ("utf-8-sig", "cp949", "utf-16"):
            try:
                df = pd.read_csv(p, encoding=enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        if df is None:
            raise ValueError(f"{p.name}: 인코딩을 읽지 못함")
    # 블룸버그 엑셀은 위쪽에 티커·설명 줄이 있을 수 있음 → 날짜처럼 보이는 첫 열이 나오는 줄을 머리글로
    if not any(_find(df, k) for k in ("date", "time")):
        for i in range(min(10, len(df))):
            row = [str(x).strip() for x in df.iloc[i].tolist()]
            if any(re.search(r"date|time|일자|시간", c, re.I) for c in row):
                df.columns = row
                df = df.iloc[i + 1:].reset_index(drop=True)
                break
    return df


def _find(df: pd.DataFrame, kind: str) -> str | None:
    pats = {"date": r"^(date|dates|일자|날짜)$", "time": r"^(time|시간|시각)$", "dt": r"date.?time|timestamp|일시",
            "open": r"^(open|시가|open_price|px_open)$", "high": r"^(high|고가|px_high)$", "low": r"^(low|저가|px_low)$",
            "close": r"^(close|last|last.?price|종가|px_last|last_trade)$", "volume": r"^(volume|vol|거래량|px_volume|num.?events)$",
            "type": r"^(type|event|event.?type|종류)$", "value": r"^(value|price|가격)$", "size": r"^(size|수량|volume)$"}
    for c in df.columns:
        if re.search(pats[kind], str(c).strip(), re.I):
            return c
    return None


def timestamps(df: pd.DataFrame, shift: float) -> pd.Series:
    c = _find(df, "dt")
    if c is None:
        d, t = _find(df, "date"), _find(df, "time")
        if d is not None and t is not None:
            s = pd.to_datetime(df[d].astype(str) + " " + df[t].astype(str), errors="coerce")
        else:
            s = pd.to_datetime(df[d or t or df.columns[0]], errors="coerce")
    else:
        s = pd.to_datetime(df[c], errors="coerce")
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_convert("Asia/Seoul").dt.tz_localize(None)
    return s + pd.Timedelta(hours=shift)


def do_bars(files: list[Path], shift: float) -> list[Path]:
    made = {}
    for p in files:
        leg, mon = leg_of(p.stem), month_of(p.stem)
        if not leg or not mon:
            print(f"  건너뜀(이름에서 종목·월물을 못 찾음): {p.name}")
            continue
        df = read_any(p)
        ts = timestamps(df, shift)
        cols = {k: _find(df, k) for k in ("open", "high", "low", "close", "volume")}
        if cols["close"] is None:
            print(f"  건너뜀(종가 열 없음): {p.name} 열={list(df.columns)}")
            continue
        out = pd.DataFrame({"t": ts})
        for k, c in cols.items():
            out[k] = pd.to_numeric(df[c], errors="coerce") if c is not None else (
                pd.to_numeric(df[cols["close"]], errors="coerce") if k != "volume" else 1.0)
        out = out.dropna(subset=["t", "close"]).sort_values("t").drop_duplicates("t", keep="last")
        out = out[(out["volume"] > 0) & (out["close"] > 0)]
        folder = HERE / f"bbg_{mon}"
        folder.mkdir(exist_ok=True)
        res = pd.DataFrame({"date": out["t"].dt.strftime("%Y%m%d"), "time": out["t"].dt.strftime("%H%M%S"),
                            "open": out["open"], "high": out["high"], "low": out["low"], "close": out["close"],
                            "volume": out["volume"]})
        res.to_csv(folder / f"bars_{leg}.csv", index=False, encoding="utf-8")
        made.setdefault(folder, {})[leg] = {"src": p.name, "rows": len(res), "first": str(out["t"].min()),
                                            "last": str(out["t"].max())}
        print(f"  {p.name} → {folder.name}/bars_{leg}.csv  {len(res):,}봉  {out['t'].min()} ~ {out['t'].max()}")
    for folder, legs in made.items():
        days = set()
        for leg in legs:
            days |= set(pd.read_csv(folder / f"bars_{leg}.csv", dtype=str)["date"])
        (folder / "meta.json").write_text(json.dumps({"source": "bloomberg", "legs": legs, "days": sorted(days)},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    return list(made)


def do_ticks(files: list[Path], shift: float) -> list[Path]:
    by_day: dict[str, list[pd.DataFrame]] = {}
    for p in files:
        leg = leg_of(p.stem.replace("tick", "").replace("TICK", ""))
        if not leg:
            print(f"  건너뜀(이름에서 종목을 못 찾음): {p.name}")
            continue
        df = read_any(p)
        ts = timestamps(df, shift)
        ty, va, sz = _find(df, "type"), _find(df, "value"), _find(df, "size")
        if ty is None or va is None:
            print(f"  건너뜀(종류·가격 열 없음): {p.name} 열={list(df.columns)}")
            continue
        x = pd.DataFrame({"t": ts, "type": df[ty].astype(str).str.upper().str.strip(),
                          "px": pd.to_numeric(df[va], errors="coerce"),
                          "qty": pd.to_numeric(df[sz], errors="coerce") if sz is not None else 0})
        x = x.dropna(subset=["t", "px"]).sort_values("t", kind="stable")
        x["bid"] = x["px"].where(x["type"].str.contains("BID")).ffill()
        x["ask"] = x["px"].where(x["type"].str.contains("ASK")).ffill()
        x["last"] = x["px"].where(x["type"].str.contains("TRADE")).ffill()
        x["leg"] = leg
        for day, g in x.groupby(x["t"].dt.strftime("%Y%m%d")):
            by_day.setdefault(day, []).append(g)
        print(f"  {p.name} → {leg} {len(x):,}건  {x['t'].min()} ~ {x['t'].max()}  "
              f"(체결 {int(x['type'].str.contains('TRADE').sum()):,} · 호가 {int(x['type'].str.contains('BID|ASK').sum()):,})")
    made = []
    for day, parts in by_day.items():
        g = pd.concat(parts).sort_values("t", kind="stable")
        g = g.dropna(subset=["bid", "ask"])
        folder = HERE / f"bbg_ticks_{day}"
        folder.mkdir(exist_ok=True)
        out = pd.DataFrame({"recv_ts": g["t"].dt.strftime("%Y-%m-%dT%H:%M:%S.%f").str[:-3] + "+09:00",
                            "leg": g["leg"], "sym": "", "exch_tm": g["t"].dt.strftime("%H%M%S"),
                            "last": g["last"], "exec_qty": g["qty"].where(g["type"].str.contains("TRADE"), 0),
                            "bid": g["bid"], "ask": g["ask"], "cum_qty": ""})
        out.to_csv(folder / "ticks.csv", index=False, encoding="utf-8-sig")
        legs = sorted(set(g["leg"]))
        print(f"  {day}: {folder.name}/ticks.csv  {len(out):,}건  종목 {legs}" + ("" if legs == ["H", "S"] else "  ⚠ 두 종목이 다 있어야 선행 분석 가능"))
        made.append(folder)
    return made


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--tz-shift", type=float, default=0.0, help="시각에 더할 시간(한국 시간으로 맞추기)")
    a = ap.parse_args()
    files = sorted(p for p in Path(a.folder).rglob("*") if p.suffix.lower() in (".csv", ".xlsx", ".xls"))
    ticks = [p for p in files if "tick" in p.name.lower() or "틱" in p.name]
    bars = [p for p in files if p not in ticks]
    print(f"1분봉 파일 {len(bars)}개 · 틱 파일 {len(ticks)}개")
    if bars:
        do_bars(bars, a.tz_shift)
    if ticks:
        do_ticks(ticks, a.tz_shift)


if __name__ == "__main__":
    sys.exit(main())
