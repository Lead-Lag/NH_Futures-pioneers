#!/usr/bin/env python3
"""KDI 월간 경제 이슈 텍스트 → 2차 병목 키워드 신호 → 다음 달 첫 거래일 시가 진입 백테스트.

미래 편향을 막는 규칙(코드로 강제):
  1. M월 신호는 date <= M월 말일인 행만 사용한다. (published_date 열이 있으면 그 날짜도 진입일 계산에 반영)
  2. 「새로움(novelty)」 기준선은 M월 이전 lookback 개월만으로 계산한다.
  3. 과열 익절은 K월 텍스트로 판정하고 K+1월 첫 거래일 시가에 실행한다.
  4. 진입일은 하드코딩하지 않고 벤치마크(코스피) 거래일 달력에서 찾는다.
  5. 신호 달을 고르지 않는다 — 기본은 CSV 에 있는 모든 달을 같은 규칙으로 돌린다(--months 는 파일럿용).

사용:
  python kdi_bottleneck.py score    --news data/kdi_news.csv
  python kdi_bottleneck.py backtest --news data/kdi_news.csv --out results/bt_bottleneck
  python kdi_bottleneck.py backtest --news data/kdi_news.csv --signal-mode mention --out results/bt_mention   # 대조군
  python kdi_bottleneck.py discover --news data/kdi_news.csv --out results/discover                          # 사전 없이 발견
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED = ["date", "issue_rank", "issue_title", "news_title"]
FREQ = "M"          # --freq W 이면 "W-SUN"(월~일 한 주). 아래 코드의 「month」 열은 신호 기간(달 또는 주)을 뜻한다.


# ───────────────────────── 데이터 읽기 ─────────────────────────
def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_news(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        sys.exit(f"[오류] CSV 에 열이 없습니다: {missing} (필요: {REQUIRED})")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["issue_rank"] = pd.to_numeric(df["issue_rank"], errors="coerce")
    bad = df["date"].isna() | df["issue_rank"].isna()
    if bad.any():
        print(f"[경고] 날짜·순위를 읽을 수 없는 {int(bad.sum())}행을 뺐습니다.", file=sys.stderr)
        df = df[~bad].copy()
    if "published_date" in df.columns:
        df["published_date"] = pd.to_datetime(df["published_date"], errors="coerce")
    for c in ("issue_title", "news_title"):
        df[c] = df[c].fillna("").str.strip()
    df["month"] = df["date"].dt.to_period(FREQ)
    if (df["issue_title"] == "").all():
        print("[알림] issue_title 이 모두 비어 있습니다(빅카인즈 데이터 등) — TOP10 이슈 점유율 기반 과열 익절은 작동하지 않습니다.",
              file=sys.stderr)
    elif FREQ != "M":
        # KDI 이슈 묶음(어떤 기사가 어느 이슈에 들어가는지)은 그 달이 끝난 뒤 공개된다 → 달 중간 주 단위 신호는 미래 정보 사용
        sys.exit("[오류] 주 단위(--freq W)는 그날그날 검색할 수 있는 뉴스(빅카인즈 등, issue_title 이 빈 데이터)에만 쓸 수 있습니다. "
                 "KDI 월간 이슈는 달이 끝난 뒤 묶여 공개되므로 주 단위로 쓰면 미래 편향이 생깁니다.")
    # 과열 판정용 이슈 목록은 중복 제거 전에 따로 보관(어느 이슈에도 기사가 남지 않는 경우 대비)
    issues = df[["month", "issue_rank", "issue_title"]].drop_duplicates(["month", "issue_rank"])
    # 같은 달 같은 제목이 여러 이슈(또는 여러 언론사)에 있으면 가장 높은 순위 이슈의 1건만 남긴다
    n0 = len(df)
    df = df.sort_values(["month", "issue_rank", "date"]).drop_duplicates(["month", "news_title"], keep="first")
    dup = n0 - len(df)
    print(f"[중복] 같은 달 같은 제목 {dup}건 제외(가장 높은 순위 이슈에만 남김) — 원래 {n0}행 → {len(df)}행", file=sys.stderr)
    df = df.sort_values(["date", "issue_rank"]).reset_index(drop=True)
    df.attrs["issues"] = issues
    df.attrs["dup_removed"] = dup
    return df


def rank_weight(rank: pd.Series, mode: str) -> pd.Series:
    if mode == "inverse":
        return 1.0 / rank.clip(lower=1)
    if mode == "uniform":
        return pd.Series(1.0, index=rank.index)
    raise ValueError(f"rank_weight 는 inverse|uniform: {mode}")


def b_filter(titles: pd.Series, cfg: dict) -> pd.Series:
    """병목어가 있고, 해소어·제외어(맥락 좁히기: 파업·인사·주택 …)가 없는 제목."""
    return (contains_any(titles, cfg["bottleneck_terms"]) & ~contains_any(titles, cfg.get("resolution_terms", []))
            & ~contains_any(titles, cfg.get("exclude_terms", [])))


def contains_any(s: pd.Series, words: list[str]) -> pd.Series:
    out = pd.Series(False, index=s.index)
    for w in words:
        out |= s.str.contains(w, regex=False)
    return out


# ───────────────────────── 월별 점수 ─────────────────────────
def monthly_counts(news: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """키워드 × 월: 언급(mention) · 병목어와 같은 제목 안 동시 언급(co) · TOP10 이슈 점유율."""
    b_terms = cfg["bottleneck_terms"]
    r_terms = cfg.get("resolution_terms", [])
    w = rank_weight(news["issue_rank"], cfg["rank_weight"])
    # 병목어가 있어도 해소어(해소·해결·완화 …)가 같은 제목에 있으면 병목이 풀렸다는 기사로 보고 뺀다
    news_has_b = b_filter(news["news_title"], cfg)
    issues = news.attrs.get("issues", news[["month", "issue_rank", "issue_title"]].drop_duplicates(["month", "issue_rank"]))

    rows = []
    for kw, spec in cfg["keywords"].items():
        al = spec["aliases"]
        in_news = contains_any(news["news_title"], al)
        in_issue = contains_any(news["issue_title"], al)
        # 기사 제목만 센다. 이슈명(KDI 가 붙인 제목)은 그 이슈의 모든 기사 행에 반복되므로, 세면 이슈 하나가
        # 기사 수만큼 부풀려진다(11/18 실제 데이터에서 발견: 「전력망 확충 …」 이슈 → 동시 언급 253건). 이슈명은 과열 판정에만 쓴다.
        mention = in_news
        co = in_news & news_has_b
        tmp = pd.DataFrame({"month": news["month"], "w": w, "mention": mention, "co": co,
                            "issue_title": news["issue_title"], "issue_rank": news["issue_rank"],
                            "in_issue": in_issue})
        iss = issues.assign(in_issue=contains_any(issues["issue_title"], al))
        for m, g in tmp.groupby("month"):
            top = iss[(iss["month"] == m) & (iss["issue_rank"] <= 10)].groupby("issue_rank")["in_issue"].max()
            rows.append({
                "month": m, "keyword": kw, "tier": spec["tier"],
                "mention_n": int(g["mention"].sum()), "mention_w": float(g.loc[g["mention"], "w"].sum()),
                "co_n": int(g["co"].sum()), "co_w": float(g.loc[g["co"], "w"].sum()),
                "top10_issues": int(len(top)),
                "top10_issue_share": float(top.mean()) if len(top) else 0.0,
            })
    return pd.DataFrame(rows)


def add_scores(counts: pd.DataFrame, cfg: dict, mode: str) -> pd.DataFrame:
    """novelty 기준선은 그 달 이전 lookback 개월만 사용 — shift(1) 로 당월을 뺀다."""
    col = "co_w" if mode == "bottleneck" else "mention_w"
    L, min_hist = cfg["lookback_months"], cfg["min_history_months"]
    all_months = pd.period_range(counts["month"].min(), counts["month"].max(), freq=FREQ)
    out = []
    for kw, g in counts.groupby("keyword"):
        g = g.set_index("month").reindex(all_months)
        g["keyword"] = kw
        g["tier"] = g["tier"].ffill().bfill()
        for c in ("mention_n", "mention_w", "co_n", "co_w", "top10_issues", "top10_issue_share"):
            g[c] = g[c].fillna(0)
        past = g[col].shift(1)
        g["base"] = past.rolling(L, min_periods=min_hist).mean()
        g["hist_months"] = past.rolling(L, min_periods=1).count()
        g["novelty"] = (g[col] + 1.0) / (g["base"] + 1.0)
        g["score"] = g[col] * g["novelty"]
        out.append(g.rename_axis("month").reset_index())
    return pd.concat(out, ignore_index=True)


def pick_signals(scores: pd.DataFrame, cfg: dict, mode: str, months: list[str] | None) -> pd.DataFrame:
    n_col = "co_n" if mode == "bottleneck" else "mention_n"
    s = scores.copy()
    s["eligible"] = (
        (s[n_col] >= cfg["min_co_count"])
        & (s["novelty"] >= cfg["min_novelty"])
        & s["base"].notna()
        & ~s["tier"].isin(cfg["exclude_tier"])
    )
    if months:
        s = s[s["month"].astype(str).isin(months)]
    sig = (s[s["eligible"]].sort_values(["month", "score"], ascending=[True, False])
           .groupby("month").head(cfg["top_n"]))
    return sig.reset_index(drop=True)


# ───────────────────────── 가격 ─────────────────────────
def get_prices(code: str, start: str, end: str, cache: Path) -> pd.DataFrame | None:
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"{code}.csv"
    if f.exists():
        df = pd.read_csv(f, parse_dates=["Date"], index_col="Date")
        if len(df) and "Volume" in df.columns and df.index.min() <= pd.Timestamp(start) + pd.Timedelta(days=7) \
                and df.index.max() >= pd.Timestamp(end) - pd.Timedelta(days=7):
            return df
    try:
        import FinanceDataReader as fdr
    except ImportError:
        sys.exit("[오류] pip install finance-datareader  (또는 --prices 폴더에 Date,Open,High,Low,Close CSV 를 넣어 두세요)")
    try:
        df = fdr.DataReader(code, start, end)
    except Exception as e:  # noqa: BLE001
        print(f"[경고] {code} 가격을 받지 못했습니다: {e}", file=sys.stderr)
        return None
    if df is None or df.empty:
        return None
    df = df[[c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]].copy()
    df.index.name = "Date"
    df.to_csv(f)
    return df


def first_trading_day_on_or_after(cal: pd.DatetimeIndex, d: pd.Timestamp) -> pd.Timestamp | None:
    i = cal.searchsorted(d)
    return cal[i] if i < len(cal) else None


def max_drawdown(path: pd.Series) -> float:
    peak = path.cummax()
    return float((path / peak - 1).min())


# ───────────────────────── 백테스트 ─────────────────────────
ENTRY_MIN_DAY = 1   # --entry-day: KDI 집계 공개가 늦을 때를 대비해 M+1월 N일 이후 첫 거래일에 진입(민감도)


def entry_date_for(month: pd.Period, news: pd.DataFrame, cal: pd.DatetimeIndex) -> pd.Timestamp | None:
    d = (month + 1).start_time + pd.Timedelta(days=ENTRY_MIN_DAY - 1)
    if "published_date" in news.columns:          # KDI 집계가 늦게 공개되면 그 다음 거래일로 미룸
        pub = news.loc[news["month"] == month, "published_date"].max()
        if pd.notna(pub):
            d = max(d, pub + pd.Timedelta(days=1))
    return first_trading_day_on_or_after(cal, d)


def overheat_exit_date(scores: pd.DataFrame, kw: str, entry: pd.Timestamp, last: pd.Timestamp,
                       cfg: dict, cal: pd.DatetimeIndex) -> tuple[pd.Timestamp | None, str | None]:
    g = scores[(scores["keyword"] == kw)].set_index("month")
    m = entry.to_period(FREQ)
    while True:
        if m not in g.index:
            return None, None
        if g.loc[m, "top10_issue_share"] >= cfg["overheat_issue_share"]:
            d = first_trading_day_on_or_after(cal, (m + 1).start_time)
            if d is not None and entry < d <= last:
                return d, str(m)
            return None, None
        m += 1


def backtest(news, scores, signals, cfg, prices_dir: Path, today: pd.Timestamp):
    bench_code = cfg["benchmark"]
    start = (news["date"].min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    bench = get_prices(bench_code, start, end, prices_dir)
    if bench is None:
        sys.exit(f"[오류] 벤치마크 {bench_code} 가격이 없습니다.")
    cal = bench.index
    cost = cfg["round_trip_cost"]
    # 헤지: 같은 금액의 코스피200 을 판 것으로 계산(코스피200선물 매도의 근사, 선물 베이시스·증거금 이자 무시)
    hedge = get_prices(cfg.get("hedge_benchmark", "KS200"), start, end, prices_dir)
    trades, events = [], []
    month_legs: dict = {}   # (월, 보유기간) → {종목코드: (경로, 순수익률)} — 같은 달 신호끼리 겹치는 종목은 한 번만

    for _, s in signals.iterrows():
        kw, month = s["keyword"], s["month"]
        entry = entry_date_for(month, news, cal)
        if entry is None:
            continue
        i0 = cal.get_loc(entry)
        for H in cfg["horizons_days"]:
            i1 = min(i0 + H - 1, len(cal) - 1)
            exit_d = cal[i1]
            complete = (i0 + H - 1) <= len(cal) - 1
            oh_d, oh_m = overheat_exit_date(scores, kw, entry, exit_d, cfg, cal)
            b_ret = bench.loc[exit_d, "Close"] / bench.loc[entry, "Open"] - 1
            paths = []
            for code, name in cfg["keywords"][kw]["tickers"].items():
                px = get_prices(code, start, end, prices_dir)
                if px is None or entry not in px.index or exit_d not in px.index:
                    trades.append({"signal_month": str(month), "keyword": kw, "code": code, "name": name,
                                   "horizon": H, "note": "가격 없음(미상장·거래정지 등) — 제외"})
                    continue
                o = px.loc[entry, "Open"]
                if not (pd.notna(o) and o > 0):            # 거래정지 등으로 시가가 0·빈 값이면 그날 종가, 그것도 없으면 제외
                    o = px.loc[entry, "Close"]
                if not (pd.notna(o) and o > 0):
                    trades.append({"signal_month": str(month), "keyword": kw, "code": code, "name": name,
                                   "horizon": H, "note": "진입일 가격 0(거래정지 등) — 제외"})
                    continue
                path = px.loc[entry:exit_d, "Close"].replace(0, np.nan).ffill() / o
                r_h = path.iloc[-1] - 1
                r = r_h
                r_oh = None
                if oh_d is not None and oh_d in px.index and px.loc[oh_d, "Open"] > 0:
                    r_oh = px.loc[oh_d, "Open"] / o - 1
                    f = cfg["overheat_exit_fraction"]
                    r = f * r_oh + (1 - f) * r_h
                r_net = r - cost
                paths.append(path)
                month_legs.setdefault((str(month), H, entry, exit_d, complete, b_ret), {}).setdefault(code, (path, r_net))
                trades.append({
                    "signal_month": str(month), "keyword": kw, "code": code, "name": name, "horizon": H,
                    "entry_date": entry.date(), "entry_open": o, "exit_date": exit_d.date(),
                    "complete": complete, "overheat_month": oh_m,
                    "overheat_exit": oh_d.date() if oh_d is not None else None,
                    "ret_hold": r_h, "ret_overheat_leg": r_oh, "ret_net": r_net,
                    "bench_ret": b_ret, "alpha": r_net - b_ret, "mdd": max_drawdown(path),
                })
            if paths:
                port = pd.concat(paths, axis=1).ffill().mean(axis=1)   # 동일가중 매수 후 보유
                t = [x for x in trades if x.get("signal_month") == str(month) and x.get("keyword") == kw
                     and x.get("horizon") == H and "ret_net" in x]
                pr = float(np.mean([x["ret_net"] for x in t]))
                events.append({
                    "signal_month": str(month), "keyword": kw, "horizon": H, "entry_date": entry.date(),
                    "exit_date": exit_d.date(), "complete": complete, "n_stocks": len(t),
                    "score": s["score"], "co_n": s["co_n"], "novelty": s["novelty"],
                    "port_ret": pr, "bench_ret": b_ret, "alpha": pr - b_ret,
                    "port_mdd": max_drawdown(port), "overheat_exit": oh_d.date() if oh_d is not None else None,
                })
    ports = []
    for (m, H, entry, exit_d, complete, b_ret), legs in month_legs.items():
        port = pd.concat([v[0] for v in legs.values()], axis=1).ffill().mean(axis=1)
        pr = float(np.mean([v[1] for v in legs.values()]))
        kws = signals.loc[signals["month"].astype(str) == m, "keyword"].tolist()
        row = {"signal_month": m, "horizon": H, "keywords": "·".join(kws), "n_stocks": len(legs),
               "codes": ",".join(legs), "entry_date": entry.date(), "exit_date": exit_d.date(),
               "complete": complete, "port_ret": pr, "bench_ret": b_ret, "alpha": pr - b_ret,
               "port_mdd": max_drawdown(port)}
        if hedge is not None and entry in hedge.index and exit_d in hedge.index:
            hp = hedge.loc[entry:exit_d, "Close"] / hedge.loc[entry, "Open"]
            hp = hp.reindex(port.index).ffill()
            hedged = 1 + (port - 1) - (hp - 1)                      # 주식 매수 + 같은 금액 코스피200 매도
            row.update({"k200_ret": float(hp.iloc[-1] - 1), "hedged_ret": pr - float(hp.iloc[-1] - 1),
                        "hedged_mdd": max_drawdown(hedged)})
        ports.append(row)
    return pd.DataFrame(trades), pd.DataFrame(events), pd.DataFrame(ports)


def summarize(ports: pd.DataFrame, events: pd.DataFrame, mode: str) -> str:
    lines = [f"# 백테스트 요약 — signal_mode = {mode}",
             "(집계 단위 = 신호 달의 포트폴리오. 같은 달 신호끼리 겹치는 종목은 한 번만 넣음. 키워드별 결과는 events.csv)", ""]
    if ports.empty:
        return "\n".join(lines + ["신호 없음"])
    for H, g in ports.groupby("horizon"):
        done = g[g["complete"]]
        lines.append(f"## 보유 {H}거래일 — 신호 달 {len(g)}개 (기간 끝난 것 {len(done)}개)")
        if len(done):
            a = done["alpha"]
            se = a.std(ddof=1) / np.sqrt(len(a)) if len(a) > 1 else float("nan")
            lines += [
                f"- 평균 수익률 {done['port_ret'].mean():+.1%} · 코스피 {done['bench_ret'].mean():+.1%} · "
                f"평균 초과수익 {a.mean():+.1%} (표준오차 {se:.1%}, 보유기간이 겹치면 실제 불확실성은 더 큼)",
                f"- 초과수익 > 0 비율 {(a > 0).mean():.0%} · 평균 MDD {done['port_mdd'].mean():.1%} · 최악 MDD {done['port_mdd'].min():.1%}",
            ]
            if "hedged_ret" in done.columns and done["hedged_ret"].notna().any():
                hr = done["hedged_ret"].dropna()
                hse = hr.std(ddof=1) / np.sqrt(len(hr)) if len(hr) > 1 else float("nan")
                lines.append(f"- 코스피200 헤지(같은 금액 매도): 평균 {hr.mean():+.1%} (표준오차 {hse:.1%}, t {hr.mean() / hse if hse == hse and hse > 0 else float('nan'):+.2f}) · "
                             f"> 0 비율 {(hr > 0).mean():.0%} · 평균 MDD {done['hedged_mdd'].mean():.1%} · 최악 MDD {done['hedged_mdd'].min():.1%}")
        lines.append("")
        cols = ["signal_month", "keywords", "n_stocks", "entry_date", "exit_date", "complete",
                "port_ret", "bench_ret", "alpha", "port_mdd"] + [c for c in ("hedged_ret", "hedged_mdd") if c in g.columns]
        lines.append(g[cols]
                     .to_string(index=False, float_format=lambda x: f"{x:+.3f}"))
        lines.append("")
    return "\n".join(lines)



# ───────────────────────── 발견 모드(사전 없이) ─────────────────────────
TOKEN_RE = None


def tokenize(title: str, dcfg: dict) -> list[str]:
    """한글·영문·숫자 덩어리로 자르고 끝의 조사를 뗀다(남는 길이가 min_token_len 이상일 때만)."""
    import re
    global TOKEN_RE
    if TOKEN_RE is None:
        TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")
    parts = sorted(dcfg["particles"], key=len, reverse=True)
    out = []
    for t in TOKEN_RE.findall(title):
        for p in parts:
            if t.endswith(p) and len(t) - len(p) >= dcfg["min_token_len"]:
                t = t[: -len(p)]
                break
        out.append(t)
    return out


def discover(news: pd.DataFrame, cfg: dict, require_bottleneck: bool = True, top_n: int | None = None) -> pd.DataFrame:
    """월마다: 병목어가 있는 제목 안의 다른 단어(와 이웃 두 단어)를 순위 가중으로 세고, 이전 달 기준선 대비 새로움을 낸다.
    require_bottleneck=False 이면 병목어 조건 없이 모든 제목의 단어를 센다(대조군: 「그냥 많이 나온 새 단어」)."""
    d = cfg["discover"]
    b_terms = cfg["bottleneck_terms"]
    # 단어에서 뺄 병목 표현: 병목어 자체 + 구(「공급 부족」)를 이루는 단어 + 핵심 병목어(부족·지연 …)가 들어간 단어
    core = cfg.get("bottleneck_core", b_terms)
    stop = set(d["stopwords"]) | set(b_terms) | {w for t in b_terms for w in t.split()}
    w = rank_weight(news["issue_rank"], cfg["rank_weight"])
    keep = b_filter(news["news_title"], cfg) if require_bottleneck else pd.Series(True, index=news.index)
    rows = []
    for (m, wt, title, k) in zip(news["month"], w, news["news_title"], keep):
        if not k:
            continue
        toks = tokenize(title, d)
        uni = [t for t in toks if len(t) >= d["min_token_len"] and t not in stop and not any(b in t for b in core)]
        grams = set(uni)
        if d["use_bigrams"]:
            for a, b in zip(toks, toks[1:]):
                if a in uni and b in uni:
                    grams.add(f"{a} {b}")
        for g in grams:                                    # 한 제목에서 같은 단어는 1번
            rows.append({"month": m, "token": g, "w": wt})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    all_m = pd.period_range(news["month"].min(), news["month"].max(), freq=FREQ)
    co_n = df.pivot_table(index="token", columns="month", values="w", aggfunc="size", fill_value=0).reindex(columns=all_m, fill_value=0)
    co_w = df.pivot_table(index="token", columns="month", values="w", aggfunc="sum", fill_value=0).reindex(columns=all_m, fill_value=0)
    past = co_w.shift(1, axis=1)                          # 당월 제외, 이전 달만
    base = past.T.rolling(cfg["lookback_months"], min_periods=cfg["min_history_months"]).mean().T
    nov = (co_w + 1) / (base + 1)
    sc = co_w * nov
    long = lambda x, name: x.stack(future_stack=True).rename(name) if hasattr(x, "stack") else x
    res = pd.concat([long(co_n, "co_n"), long(co_w, "co_w"), long(base, "base"), long(nov, "novelty"), long(sc, "score")], axis=1)
    res.index.names = ["token", "month"]
    res = res.reset_index()
    res = res[(res["co_n"] >= d["min_co_count"]) & (res["novelty"] >= d["min_novelty"]) & res["base"].notna()]
    # 사전 키워드와 겹치면 표시만 한다(종목 연결은 여전히 사람이 정한 표)
    alias = {a: k for k, v in cfg["keywords"].items() for a in v["aliases"]}
    res["matched_keyword"] = res["token"].map(lambda t: next((k for a, k in alias.items() if a in t or t in a), ""))
    return (res.sort_values(["month", "score"], ascending=[True, False])
            .groupby("month").head(top_n or d["top_n"]).reset_index(drop=True))


# ───────────────────────── 발견 모드 매매: 단어 → 주요제품 → 종목 ─────────────────────────
def load_listing(path: Path | None) -> pd.DataFrame:
    """상장사 목록(종목코드·이름·주요제품). 파일이 없으면 FinanceDataReader 의 KRX-DESC 를 받아 저장한다.
    파일 형식: FDR KRX-DESC(Code, Name, Industry …) 또는 KIND 상장법인목록(종목코드, 회사명, 주요제품 …)."""
    if path is not None and path.exists():
        if path.suffix.lower() == ".csv":
            df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
        else:
            try:                                   # KIND 「상장법인목록」 .xls 는 실제로는 HTML 표
                df = pd.read_excel(path, dtype=str)
            except Exception:  # noqa: BLE001
                raw = path.read_bytes()
                for enc in ("utf-8", "cp949", "euc-kr"):
                    try:
                        txt = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                from io import StringIO
                df = pd.read_html(StringIO(txt), converters={"종목코드": str})[0].astype(str)
    else:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX-DESC").astype(str)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index=False, encoding="utf-8-sig")
    df = df.loc[:, ~df.columns.duplicated()]
    def col(*names):
        for n in names:
            if n in df.columns:
                return df[n].astype(str)
        return None
    code, name = col("종목코드", "Code", "Symbol"), col("회사명", "Name", "종목명")
    # 주요제품 설명이 가장 구체적이므로 우선. 없으면 Industry(업종) 사용
    prods = col("주요제품", "Products", "Industry")
    if code is None or name is None or prods is None:
        sys.exit(f"[오류] 상장사 목록에 종목코드·회사명·주요제품(또는 Industry) 열이 없습니다. 열: {list(df.columns)}")
    out = pd.DataFrame({"Code": code.str.extract(r"(\d{1,6})")[0].str.zfill(6), "Name": name,
                        "Products": prods.fillna("").replace("nan", "")})
    ld = col("상장일", "ListingDate")
    out["ListingDate"] = pd.to_datetime(ld, errors="coerce") if ld is not None else pd.NaT
    mk = col("시장구분", "Market")
    if mk is not None:
        out["Market"] = mk
    out = out.dropna(subset=["Code"]).drop_duplicates("Code")
    filled = (out["Products"].str.strip().str.len() > 0).mean()
    if filled < 0.5:
        sys.exit(f"[오류] 상장사 {len(out)}곳 중 주요제품이 채워진 곳이 {filled:.0%} 뿐입니다(FDR KRX-DESC 가 빈 값을 줌). "
                 "KIND 「상장법인목록」 엑셀을 받아 --listing 파일경로 로 지정하세요.")
    print(f"[상장사 목록] {len(out)}곳 · 주요제품 열 = "
          f"{next(n for n in ('주요제품', 'Products', 'Industry') if n in df.columns)}", file=sys.stderr)
    return out


def match_products(token: str, listing: pd.DataFrame, dcfg: dict) -> pd.DataFrame:
    """주요제품 설명에 단어가 들어 있는 회사. 너무 일반적인 단어(부품·제품 …)와 한 글자는 쓰지 않는다."""
    if " " in token or len(token) < dcfg["min_token_len"] or token in set(dcfg["generic_products"]):
        return listing.iloc[0:0]
    if dcfg.get("exclude_digit_tokens", False) and re.search(r"\d", token):
        return listing.iloc[0:0]                       # v2: 「15」「08」「2차」 같은 숫자 섞인 단어 제외
    if dcfg.get("match_mode", "substring") == "item_head":
        # v2: 주요제품을 쉼표 등으로 나눈 항목마다 「마지막 단어(한국어 명사구의 중심어)」가 그 단어로 끝날 때만 연결.
        #     「메모리 반도체」→반도체 ○ · 「서울 소재 오피스 빌딩 임대」→서울 × · 「IT인프라」→인프라 ○
        def head_hit(text: str) -> bool:
            for it in re.split(r"[,·/;()]", str(text)):
                words = it.strip().split()
                if words and words[-1].endswith(token):
                    return True
            return False
        return listing[listing["Products"].map(head_hit)]
    return listing[listing["Products"].str.contains(token, regex=False)]       # v1: 문장 아무 곳


def avg_trading_value(code: str, before: pd.Timestamp, days: int, start: str, end: str, cache: Path) -> float:
    px = get_prices(code, start, end, cache)
    if px is None or "Volume" not in px.columns:
        return float("nan")
    win = px[(px.index < before)].tail(days)               # 진입일 이전 자료만(미래 정보 없음)
    traded = (win["Volume"] > 0).sum()
    if len(win) < days // 2 or traded < days // 2:          # 거래정지 등으로 거래가 없던 종목 제외
        return float("nan")
    return float((win["Close"] * win["Volume"]).mean())


# ───────────────────────── 2차 연결: 1차 회사를 계약상대로 둔 공급계약 공시 ─────────────────────────
CORP_RE = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|co\.?,?\s*ltd\.?|inc\.?|corporation|corp\.?|limited|ltd\.?", re.I)


# 한글로 적은 영문 그룹명(「(주)엘지유플러스」 「(주)케이티」) → 상장사 목록 표기(LG유플러스 · KT)
HANGUL_ALIAS = {"엘지": "lg", "에스케이": "sk", "케이티": "kt", "씨제이": "cj", "지에스": "gs", "에이치디": "hd",
                "엘에스": "ls", "에이치엘": "hl", "디비": "db", "케이씨씨": "kcc", "에쓰오일": "s-oil", "포스코홀딩스": "posco홀딩스"}


def norm_name(s: str) -> str:
    t = re.sub(r"[\s.,·\-()&]", "", CORP_RE.sub("", str(s))).lower()
    for k, v in HANGUL_ALIAS.items():
        if t.startswith(k):
            t = v.replace("-", "") + t[len(k):]
            break
    return t


def load_contracts(path: Path, corp_codes: Path | None) -> pd.DataFrame:
    """dart_collect.py contracts 결과. 정정 공시는 쓰지 않는다(원래 공시의 접수일 = 그 정보를 알 수 있게 된 날)."""
    c = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    c = c[(c["is_correction"].str.lower() != "true") & (c["stock_code"].str.strip() != "")
          & ~c["report_nm"].str.contains("거래정지")].copy()          # 「주권매매거래정지(단일판매공급계약)」 는 계약 공시가 아님
    c["rcept_dt"] = pd.to_datetime(c["rcept_dt"], format="%Y%m%d", errors="coerce")
    c["supplier"] = c["stock_code"].str.zfill(6)
    # 계약상대 칸에 여러 회사가 있으면(「삼성전자, SK하이닉스」) 나눠서 각각 맞춘다
    c["cp_parts"] = c["counterparty"].map(lambda x: [norm_name(p) for p in re.split(r"[,/;]|및|외\s*\d*\s*개?사?|등", x) if norm_name(p)])
    print(f"[공급계약] {len(c)}건(정정 제외) · 계약상대 읽음 {(c['counterparty'].str.len() > 1).mean():.0%} · "
          f"비공개(-) {(c['counterparty'].str.strip() == '-').mean():.0%}", file=sys.stderr)
    eng = {}
    if corp_codes is not None and corp_codes.exists():
        cc = pd.read_csv(corp_codes, dtype=str, encoding="utf-8-sig").fillna("")
        if "corp_eng_name" in cc.columns:
            eng = {r.stock_code.zfill(6): norm_name(r.corp_eng_name) for r in cc.itertuples() if r.corp_eng_name}
    c.attrs["eng"] = eng
    return c


def secondary_candidates(first: pd.DataFrame, contracts: pd.DataFrame, entry: pd.Timestamp, days: int,
                         listing: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """1차 회사(first)를 계약상대로 둔 공급계약을 진입일 전 days 일 안에 공시한 회사 = 2차 후보(1차 회사 자신은 제외)."""
    win = contracts[(contracts["rcept_dt"] < entry) & (contracts["rcept_dt"] >= entry - pd.Timedelta(days=days))]
    eng = contracts.attrs.get("eng", {})
    names = {}
    for code, nm in zip(first["Code"], first["Name"]):
        for k in {norm_name(nm), eng.get(code, "")}:
            if len(k) >= 2:
                names[k] = (code, nm)
    hits = []
    for r in win.itertuples():
        for part in r.cp_parts:
            m = names.get(part) or next((v for k, v in names.items() if len(k) >= 3 and part.startswith(k)), None)
            if m and r.supplier != m[0]:
                hits.append({"supplier": r.supplier, "supplier_name": r.corp_name, "customer": m[1],
                             "counterparty": r.counterparty, "contract": r.contract_name, "rcept_dt": r.rcept_dt.date(),
                             "sales_ratio": r.sales_ratio, "rcept_no": r.rcept_no})
                break
    sup = {h["supplier"] for h in hits} - set(first["Code"])
    return listing[listing["Code"].isin(sup)], [h for h in hits if h["supplier"] in sup]


def build_discover_signals(news, cfg, listing, prices_dir: Path, today: pd.Timestamp, control: bool,
                           contracts: pd.DataFrame | None = None):
    d = cfg["discover"]
    found = discover(news, cfg, require_bottleneck=not control, top_n=d["candidate_tokens"])
    start = (news["date"].min() - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    bench = get_prices(cfg["benchmark"], start, end, prices_dir)
    cal = bench.index
    sig_rows, cfg2, mapping_log = [], json.loads(json.dumps(cfg)), []
    evidence = []                                           # 2차 연결 근거(어떤 공시로 연결됐는지)
    cfg2["keywords"] = {}
    for m, g in found.groupby("month"):
        entry = entry_date_for(m, news, cal)
        if entry is None:
            continue
        picked = 0
        for _, r in g.iterrows():                           # 점수 높은 단어부터
            cand = match_products(r["token"], listing, d)
            cand = cand[~(cand["ListingDate"] >= entry)]          # 진입일에 아직 상장 전인 회사 제외
            n_first = len(cand)
            if contracts is not None and not cand.empty:         # --link secondary: 1차 → 그 1차에 납품한 회사
                cand, ev = secondary_candidates(cand, contracts, entry, cfg.get("secondary_days", 365), listing)
                cand = cand[~(cand["ListingDate"] >= entry)]
                evidence += [{"month": str(m), "token": r["token"], **e} for e in ev]
            if cand.empty:
                mapping_log.append({"month": str(m), "token": r["token"], "score": r["score"], "n_first": n_first,
                                    "n_match": 0, "picked": ""})
                continue
            tv = [(c, nm, avg_trading_value(c, entry, d["liquidity_days"], start, end, prices_dir))
                  for c, nm in zip(cand["Code"], cand["Name"])]
            tv = sorted([x for x in tv if pd.notna(x[2])], key=lambda x: -x[2])[: d["stocks_per_token"]]
            mapping_log.append({"month": str(m), "token": r["token"], "score": r["score"], "n_first": n_first,
                                "n_match": len(cand), "picked": " · ".join(f"{nm}({c})" for c, nm, _ in tv)})
            if not tv:
                continue
            key = f"{r['token']}@{m}"
            cfg2["keywords"][key] = {"aliases": [r["token"]], "tier": "발견", "tickers": {c: nm for c, nm, _ in tv}}
            sig_rows.append({"month": m, "keyword": key, "token": r["token"], "tier": "발견", "co_n": r["co_n"],
                             "mention_n": r["co_n"], "novelty": r["novelty"], "score": r["score"]})
            picked += 1
            if picked >= d["map_top_tokens"]:
                break
    cfg2["_evidence"] = evidence
    return pd.DataFrame(sig_rows), cfg2, pd.DataFrame(mapping_log)


# ───────────────────────── CLI ─────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["score", "backtest", "discover", "backtest-discover"])
    ap.add_argument("--news", required=True, type=Path)
    ap.add_argument("--config", default=Path("strategy_config.json"), type=Path)
    ap.add_argument("--signal-mode", choices=["bottleneck", "mention"], default="bottleneck",
                    help="bottleneck: 병목어와 같은 제목 안 동시 언급 / mention: 단순 언급(대조군)")
    ap.add_argument("--months", default="", help="파일럿용, 예: 2024-03,2024-09,2025-05 (비우면 전체 달)")
    ap.add_argument("--prices", default=Path("results/prices"), type=Path)
    ap.add_argument("--out", default=None, type=Path)
    ap.add_argument("--today", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--listing", default=Path("results/listing_krx_desc.csv"), type=Path,
                    help="상장사 주요제품 목록(없으면 FDR KRX-DESC 를 받아 이 경로에 저장)")
    ap.add_argument("--entry-day", type=int, default=1,
                    help="M+1월 이 날짜 이후 첫 거래일에 진입(기본 1일). 공개 시점 민감도 분석용, 예: 15")
    ap.add_argument("--freq", choices=["M", "W", "D"], default="M",
                    help="신호 기간: M=월(기본) · W=주(월~일, 다음 주 첫 거래일 시가) · D=일(다음 거래일 시가). W·D 는 빅카인즈처럼 그날 검색 가능한 뉴스에만")
    ap.add_argument("--link", choices=["first", "secondary"], default="first",
                    help="first: 주요제품이 단어와 맞는 회사(1차) · secondary: 그 1차 회사를 계약상대로 둔 공급계약 공시 회사(2차)")
    ap.add_argument("--contracts", type=Path, default=None, help="--link secondary 용 dart_collect.py contracts 결과 CSV")
    ap.add_argument("--corp-codes", type=Path, default=Path("results/dart/corp_codes.csv"))
    ap.add_argument("--control", action="store_true", help="backtest-discover 대조군: 병목어 조건 없이 새로 많이 나온 단어")
    a = ap.parse_args()

    cfg = load_config(a.config)
    global ENTRY_MIN_DAY, FREQ
    ENTRY_MIN_DAY = a.entry_day
    cfg["entry_min_day"] = a.entry_day
    if a.freq == "W":
        FREQ = "W-SUN"
        cfg.update(cfg.get("weekly", {}))                  # 주 단위 기준선 길이·보유기간(설정 파일의 weekly 항목)
    elif a.freq == "D":
        FREQ = "D"                                         # 그날 기사 → 다음 거래일 시가 진입(주말 기사는 월요일)
        cfg.update(cfg.get("daily", {}))
    cfg["freq"] = FREQ
    news = load_news(a.news)
    months = [m.strip() for m in a.months.split(",") if m.strip()] or None
    out = a.out or Path("results") / f"{a.cmd}_{a.signal_mode}_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)

    if a.cmd == "backtest-discover":
        listing = load_listing(a.listing)
        contracts = None
        if a.link == "secondary":
            if a.contracts is None:
                sys.exit("[오류] --link secondary 에는 --contracts results/dart/contracts_….csv 가 필요합니다.")
            contracts = load_contracts(a.contracts, a.corp_codes)
        cfg["link"] = a.link
        sigs, cfg2, mlog = build_discover_signals(news, cfg, listing, a.prices, pd.Timestamp(a.today), a.control, contracts)
        mlog.to_csv(out / "token_mapping.csv", index=False, encoding="utf-8-sig")
        if contracts is not None:
            pd.DataFrame(cfg2.pop("_evidence")).to_csv(out / "secondary_evidence.csv", index=False, encoding="utf-8-sig")
        else:
            cfg2.pop("_evidence", None)
        sigs.to_csv(out / "signals.csv", index=False, encoding="utf-8-sig")
        mode = ("discover-control" if a.control else "discover") + ("-secondary" if contracts is not None else "")
        (out / "run_meta.json").write_text(json.dumps({"cmd": a.cmd, "control": a.control, "news": str(a.news),
            "listing": str(a.listing), "listing_rows": len(listing), "today": a.today, "config": cfg},
            ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"뉴스 {len(news)}행 · 상장사 {len(listing)}곳 → 발견 신호 {len(sigs)}건 ({mode})")
        if not sigs.empty:
            print(mlog[mlog["picked"] != ""].to_string(index=False, float_format=lambda x: f"{x:.1f}"))
            scores2 = add_scores(monthly_counts(news, cfg2), cfg2, "bottleneck")
            trades, events, ports = backtest(news, scores2, sigs, cfg2, a.prices, pd.Timestamp(a.today))
            trades.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
            events.to_csv(out / "events.csv", index=False, encoding="utf-8-sig")
            ports.to_csv(out / "portfolios.csv", index=False, encoding="utf-8-sig")
            text = summarize(ports, events, mode)
            (out / "summary.txt").write_text(text, encoding="utf-8")
            print("\n" + text)
        print(f"\n결과: {out}")
        return

    if a.cmd == "discover":
        found = discover(news, cfg)
        if months is not None and not found.empty:
            found = found[found["month"].astype(str).isin(months)]
        found.to_csv(out / "discovered_by_month.csv", index=False, encoding="utf-8-sig")
        (out / "run_meta.json").write_text(json.dumps({"cmd": "discover", "news": str(a.news), "months": months,
            "config": cfg}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"뉴스 {len(news)}행 → 달별 상위 단어 {len(found)}개 (사전 없이 발견)")
        if not found.empty:
            print(found[["month", "token", "co_n", "novelty", "score", "matched_keyword"]]
                  .to_string(index=False, float_format=lambda x: f"{x:.2f}"))
        print(f"\n결과: {out}")
        return

    counts = monthly_counts(news, cfg)
    scores = add_scores(counts, cfg, a.signal_mode)
    signals = pick_signals(scores, cfg, a.signal_mode, months)
    scores.to_csv(out / "scores_by_month.csv", index=False, encoding="utf-8-sig")
    signals.to_csv(out / "signals.csv", index=False, encoding="utf-8-sig")
    (out / "run_meta.json").write_text(json.dumps(
        {"cmd": a.cmd, "signal_mode": a.signal_mode, "news": str(a.news), "months": months,
         "news_rows": len(news), "news_months": [str(news["month"].min()), str(news["month"].max())],
         "today": a.today, "config": cfg}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    b_any = contains_any(news["news_title"], cfg["bottleneck_terms"])
    r_any = contains_any(news["news_title"], cfg.get("resolution_terms", []))
    print(f"병목어 제목 {int(b_any.sum())}건 중 해소어가 함께 있어 뺀 제목 {int((b_any & r_any).sum())}건 "
          f"(results 폴더 resolved_titles.csv 에서 직접 확인)")
    news.loc[b_any & r_any, ["date", "issue_rank", "news_title"]].to_csv(out / "resolved_titles.csv", index=False, encoding="utf-8-sig")
    print(f"뉴스 {len(news)}행 · {news['month'].nunique()}개월 → 신호 {len(signals)}건")
    print(signals[["month", "keyword", "tier", "co_n", "mention_n", "novelty", "score"]]
          .to_string(index=False, float_format=lambda x: f"{x:.2f}"))

    if a.cmd == "backtest":
        trades, events, ports = backtest(news, scores, signals, cfg, a.prices, pd.Timestamp(a.today))
        trades.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
        events.to_csv(out / "events.csv", index=False, encoding="utf-8-sig")
        ports.to_csv(out / "portfolios.csv", index=False, encoding="utf-8-sig")
        text = summarize(ports, events, a.signal_mode)
        (out / "summary.txt").write_text(text, encoding="utf-8")
        print("\n" + text)
    print(f"\n결과: {out}")


if __name__ == "__main__":
    main()
