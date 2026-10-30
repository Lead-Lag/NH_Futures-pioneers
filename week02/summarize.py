"""실행 기록(results/<run_id>/)을 표로 정리해 summary.md 와 chart.png 를 만듭니다.

  python summarize.py results/run_paper_20261027_090012
  python summarize.py results/*            # 여러 실행을 한 번에
기록 파일은 읽기만 합니다. 손익은 trades.csv 의 net_pl 을 더하기만 합니다.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REASON_KO = {
    "EXIT_TP": "익절(중심선)", "EXIT_STOP": "손절", "EXIT_TIME": "시간 청산",
    "EXIT_EOD": "장 마감 정리", "EXIT_MANUAL": "종료 시 정리",
}


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def summarize_run(folder: Path | str) -> Path:
    folder = Path(folder)
    meta = json.loads((folder / "run_meta.json").read_text(encoding="utf-8")) if (folder / "run_meta.json").exists() else {}
    trades = _read(folder / "trades.csv")
    signals = _read(folder / "signals.csv")
    orders = _read(folder / "orders.csv")
    fills = _read(folder / "fills.csv")
    calls = _read(folder / "api_calls.csv")
    bars = _read(folder / "bars.csv")
    events = []
    if (folder / "events.jsonl").exists():
        events = [json.loads(l) for l in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    pl = [_f(t["net_pl"]) for t in trades]
    wins = [x for x in pl if x > 0]
    losses = [x for x in pl if x <= 0]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for x in pl:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)

    L = []
    L.append(f"# 실행 요약 — {meta.get('run_id', folder.name)}\n")
    if meta.get("synthetic"):
        L.append("> ⚠️ **가짜(synthetic) 데이터로 돌린 코드 점검용 결과입니다. 보고서 결과로 쓰지 마세요.**\n")
    L.append("| 항목 | 값 |\n| --- | --- |")
    for k, label in [("mode_label", "실행 환경"), ("environment", "접속 환경"), ("symbol", "종목"),
                     ("started_at", "시작"), ("ended_at", "종료"), ("data_source", "데이터"),
                     ("first_bar", "첫 봉"), ("last_bar", "마지막 봉"), ("slippage_ticks", "슬리피지(틱)")]:
        if meta.get(k) not in (None, ""):
            L.append(f"| {label} | {meta[k]} |")
    inst = meta.get("instrument") or {}
    if inst:
        L.append(f"| 종목명 / 최종거래일 | {inst.get('sym_nm')} / {inst.get('lst_trd_dt')} |")
    L.append(f"| 처리한 봉 | {len(bars)} |")

    L.append("\n## 성과\n")
    L.append("| 항목 | 값 |\n| --- | --- |")
    L.append(f"| 거래 수 (매수/매도) | {len(trades)} ({sum(t['direction']=='LONG' for t in trades)} / {sum(t['direction']=='SHORT' for t in trades)}) |")
    L.append(f"| 승 / 패 | {len(wins)} / {len(losses)} |")
    L.append(f"| 승률 | {len(wins)/len(trades)*100:.1f}% |" if trades else "| 승률 | - |")
    L.append(f"| 순손익 | **{sum(pl):+,.0f}원** |")
    L.append(f"| 평균 이익 / 평균 손실 | {(sum(wins)/len(wins) if wins else 0):+,.0f}원 / {(sum(losses)/len(losses) if losses else 0):+,.0f}원 |")
    gl = -sum(losses)
    L.append(f"| Profit Factor | {sum(wins)/gl:.2f} |" if gl > 0 else "| Profit Factor | - |")
    L.append(f"| 최대 낙폭(실현 누적) | {mdd:,.0f}원 |")
    L.append(f"| 평균 보유 봉 | {sum(_f(t['held_bars']) for t in trades)/len(trades):.1f} |" if trades else "| 평균 보유 봉 | - |")

    L.append("\n### 청산 사유\n")
    L.append("| 사유 | 건수 | 순손익 합 |\n| --- | --- | --- |")
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t["exit_reason"]].append(_f(t["net_pl"]))
    for r, v in sorted(by_reason.items()):
        L.append(f"| {REASON_KO.get(r, r)} | {len(v)} | {sum(v):+,.0f}원 |")

    by_day = defaultdict(list)
    for t in trades:
        by_day[t["exit_time"][:10]].append(_f(t["net_pl"]))
    if by_day:
        L.append("\n### 거래일별\n")
        L.append("| 날짜 | 거래 | 승 | 순손익 |\n| --- | --- | --- | --- |")
        for d, v in sorted(by_day.items()):
            L.append(f"| {d} | {len(v)} | {sum(x>0 for x in v)} | {sum(v):+,.0f}원 |")

    L.append("\n## 신호와 주문\n")
    taken = [s for s in signals if s.get("taken") == "1"]
    L.append(f"- 진입 신호 {len(signals)}건 중 진입 {len(taken)}건")
    import re
    blocked = Counter(re.split(r"[\d(]", s.get("blocked_by") or "")[0].strip() or "기타"
                      for s in signals if s.get("taken") != "1")
    for k, v in blocked.most_common():
        L.append(f"  - 보류: {k} {v}건")
    st = Counter(o.get("status") for o in orders)
    L.append(f"- 주문 {len(orders)}건: " + ", ".join(f"{k} {v}" for k, v in st.items()))
    slips = [_f(x["slippage_ticks"]) for x in fills if x.get("slippage_ticks") not in ("", None)]
    if slips:
        L.append(f"- 기준가(신호 봉 종가·손절 판정가) 대비 체결 미끄러짐: 평균 {sum(slips)/len(slips):+.2f}틱, 최대 {max(slips):+.1f}틱 (양수 = 불리)")

    L.extend(time_of_day_lines(trades, signals, bars))

    if calls:
        L.append("\n## API 호출\n")
        L.append("| API | 호출 | 오류 | 평균 지연(ms) |\n| --- | --- | --- | --- |")
        agg = defaultdict(lambda: [0, 0, 0.0])
        for c in calls:
            a = agg[c["api"]]
            a[0] += 1
            a[1] += 1 if c.get("error_code") else 0
            a[2] += _f(c.get("latency_ms"))
        for api, (n, e, lat) in sorted(agg.items()):
            L.append(f"| {api} | {n} | {e} | {lat/n:.0f} |")
        errs = Counter(c["error_code"] for c in calls if c.get("error_code"))
        if errs:
            L.append("\n오류 코드: " + ", ".join(f"`{k}` {v}건" for k, v in errs.items()))

    ev = Counter(e["kind"] for e in events)
    notable = {k: ev[k] for k in ("api_error", "gap", "pause", "mismatch", "unfilled", "order_unknown", "order_failed", "warning", "risk") if ev.get(k)}
    L.append("\n## 주의 이벤트\n")
    L.append(", ".join(f"{k} {v}건" for k, v in notable.items()) if notable else "없음")
    for e in events:
        if e["kind"] in notable:
            L.append(f"- {e['ts']} [{e['kind']}] {e['message']}")

    if trades:
        L.append("\n## 거래 목록\n")
        L.append("| # | 방향 | 진입 | 진입가 | 청산 | 청산가 | 사유 | 틱 | 순손익 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for t in trades:
            L.append(f"| {t['trade_no']} | {t['direction']} | {t['entry_time'][5:19]} | {t['entry_price']} | {t['exit_time'][5:19]} | "
                     f"{t['exit_price']} | {REASON_KO.get(t['exit_reason'], t['exit_reason'])} | {t['ticks']} | {_f(t['net_pl']):+,.0f} |")

    chart = _chart(folder, bars, fills)
    if chart:
        L.append(f"\n![가격·밴드·체결]({chart.name})")

    out = folder / "summary.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


TICK = 0.1  # 미국달러 선물 호가 단위(원)


def _hour(ts: str) -> int | None:
    try:
        return int(ts[11:13])
    except (TypeError, ValueError):
        return None


def time_of_day_lines(trades: list[dict], signals: list[dict], bars: list[dict]) -> list[str]:
    """시간대별 분석 — 「원/달러 장중 되돌림」 가설이 어느 시간대에서 맞았는지.

    시간대 = 진입 시각의 시(hour). 밴드폭은 그 시간 봉들의 (상단−하단) 평균(틱)으로,
    그 시간대가 얼마나 출렁였는지(변동성)를 대신 보여 줍니다.
    """
    hours = sorted({h for h in [_hour(b.get("start", "")) for b in bars if b.get("upper")] if h is not None}
                   | {h for h in [_hour(t["entry_time"]) for t in trades] if h is not None})
    if not hours:
        return []
    width = defaultdict(list)
    for b in bars:
        if b.get("upper") and b.get("lower") and b.get("warmup") != "1":
            h = _hour(b["start"])
            width[h].append((_f(b["upper"]) - _f(b["lower"])) / TICK)
    if not any(width.values()):  # 준비 봉만 있는 짧은 실행이면 전부 사용
        for b in bars:
            if b.get("upper") and b.get("lower"):
                width[_hour(b["start"])].append((_f(b["upper"]) - _f(b["lower"])) / TICK)
    sig = defaultdict(lambda: [0, 0])
    for s in signals:
        h = _hour(s.get("ts", ""))
        sig[h][0] += 1
        sig[h][1] += 1 if s.get("taken") == "1" else 0
    tr = defaultdict(list)
    for t in trades:
        tr[_hour(t["entry_time"])].append(t)

    L = ["\n## 시간대별 (진입 시각 기준)\n",
         "| 시간 | 평균 밴드폭(틱) | 신호 / 진입 | 거래 | 승률 | 익절 / 손절 / 시간 | 순손익 |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for h in hours:
        ts = tr.get(h, [])
        w = width.get(h, [])
        pl = sum(_f(t["net_pl"]) for t in ts)
        wins = sum(_f(t["net_pl"]) > 0 for t in ts)
        rc = Counter(t["exit_reason"] for t in ts)
        L.append(
            f"| {h:02d}시 | {(sum(w)/len(w)):.1f} | {sig[h][0]} / {sig[h][1]} | {len(ts)} | "
            f"{(wins/len(ts)*100):.0f}% | {rc.get('EXIT_TP',0)} / {rc.get('EXIT_STOP',0)} / {rc.get('EXIT_TIME',0)} | {pl:+,.0f}원 |"
            if ts else
            f"| {h:02d}시 | {(sum(w)/len(w)) if w else 0:.1f} | {sig[h][0]} / {sig[h][1]} | 0 | - | - | - |"
        )
    am = [t for t in trades if (_hour(t["entry_time"]) or 0) < 12]
    pm = [t for t in trades if (_hour(t["entry_time"]) or 0) >= 12]
    for name, grp in (("오전(~12시)", am), ("오후(12시~)", pm)):
        if grp:
            L.append(f"\n- {name}: 거래 {len(grp)}건, 승률 {sum(_f(t['net_pl'])>0 for t in grp)/len(grp)*100:.0f}%, "
                     f"순손익 {sum(_f(t['net_pl']) for t in grp):+,.0f}원")
    return L


def combine(results_dir: Path) -> Path:
    """여러 날의 모의계좌 실행(run_paper_*)을 하나로 모아 combined_summary.md 와 데이터셋을 만듭니다.

    - combined_summary.md      : 전체 성과, 날짜별, 시간대별
    - usdkrw_1min_bars.csv     : 실행 중 받은 미국달러 선물 1분봉(중복 제거) — 팀 탐구 주제용 데이터
    """
    runs = sorted(p for p in results_dir.iterdir() if p.is_dir() and p.name.startswith("run_paper_"))
    trades, signals, bars = [], [], {}
    used = []
    for r in runs:
        t = _read(r / "trades.csv")
        s = _read(r / "signals.csv")
        b = _read(r / "bars.csv")
        if not (t or s or b):
            continue
        used.append(r.name)
        trades += t
        signals += s
        for row in b:
            bars[row["start"]] = row  # 같은 봉은 마지막 실행 값으로
    bar_rows = [bars[k] for k in sorted(bars)]

    pl = [_f(t["net_pl"]) for t in trades]
    wins = [x for x in pl if x > 0]
    cum = peak = mdd = 0.0
    for x in sorted(zip([t["exit_time"] for t in trades], pl)):
        cum += x[1]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    L = ["# 모의계좌 실행 종합\n", f"포함한 실행 {len(used)}개: " + ", ".join(f"`{u}`" for u in used) + "\n",
         "| 항목 | 값 |\n| --- | --- |",
         f"| 거래 | {len(trades)} |",
         f"| 승률 | {len(wins)/len(trades)*100:.1f}% |" if trades else "| 승률 | - |",
         f"| 순손익 | {sum(pl):+,.0f}원 |",
         f"| 최대 낙폭(실현 누적) | {mdd:,.0f}원 |",
         f"| 신호 / 진입 | {len(signals)} / {sum(s.get('taken')=='1' for s in signals)} |",
         f"| 1분봉 | {len(bar_rows)}개 |"]
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["exit_time"][:10]].append(_f(t["net_pl"]))
    if by_day:
        L += ["\n## 날짜별\n", "| 날짜 | 거래 | 승 | 순손익 |", "| --- | --- | --- | --- |"]
        L += [f"| {d} | {len(v)} | {sum(x>0 for x in v)} | {sum(v):+,.0f}원 |" for d, v in sorted(by_day.items())]
    L += time_of_day_lines(trades, signals, bar_rows)

    out = results_dir / "combined_summary.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    with open(results_dir / "usdkrw_1min_bars.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["start", "open", "high", "low", "close", "volume"])
        for b in bar_rows:
            w.writerow([b["start"], b["open"], b["high"], b["low"], b["close"], b.get("volume", "")])
    return out


def _chart(folder: Path, bars: list[dict], fills: list[dict]) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from datetime import datetime
    except ImportError:
        return None
    if not bars:
        return None
    for fam in ("Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR"):
        from matplotlib import font_manager

        if any(fam in f.name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = fam
            break
    plt.rcParams["axes.unicode_minus"] = False
    xs = list(range(len(bars)))
    idx = {b["start"]: i for i, b in enumerate(bars)}
    close = [_f(b["close"]) for b in bars]
    fig, ax = plt.subplots(figsize=(14, 5), dpi=110)
    ax.plot(xs, close, lw=0.9, color="#333", label="종가")
    for key, col, lab in (("mid", "#1f77b4", "중심선"), ("upper", "#999", "상단"), ("lower", "#999", "하단")):
        ys = [_f(b[key], None) if b[key] != "" else None for b in bars]
        ax.plot(xs, [y if y is not None else float("nan") for y in ys], lw=0.8, color=col, ls="-" if key == "mid" else "--", label=lab)
    starts = [datetime.fromisoformat(b["start"]) for b in bars]

    def pos_of(ts: str) -> int:
        t = datetime.fromisoformat(ts)
        best = 0
        for i, s in enumerate(starts):
            if s <= t:
                best = i
        return best

    style = {"ENTRY_LONG": ("^", "#d62728", "매수 진입"), "ENTRY_SHORT": ("v", "#2ca02c", "매도 진입")}
    seen = set()
    for f in fills:
        p = f["purpose"]
        m, c, lab = style.get(p, ("x", "#000", "청산"))
        ax.scatter([pos_of(f["ts"])], [_f(f["price"])], marker=m, color=c, s=40, zorder=5,
                   label=None if lab in seen else lab)
        seen.add(lab)
    ticks = list(range(0, len(bars), max(1, len(bars) // 12)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([bars[i]["start"][5:16].replace("T", " ") for i in ticks], rotation=30, fontsize=8)
    ax.set_title(f"{folder.name} — 미국달러 선물 1분봉, 볼린저 밴드와 체결")
    ax.legend(loc="upper left", fontsize=8, ncol=6)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    out = folder / "chart.png"
    fig.savefig(out)
    plt.close(fig)
    return out


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--combine":
        print(combine(Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parent / "results")))
        raise SystemExit(0)
    for arg in sys.argv[1:]:
        p = Path(arg)
        if (p / "run_meta.json").exists() or (p / "trades.csv").exists():
            print(summarize_run(p))
