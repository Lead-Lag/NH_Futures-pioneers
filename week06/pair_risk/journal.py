"""실행 기록. 실행 하나 = results/<run_id>/ 폴더 하나.

events.jsonl   한 줄 = 한 사건(상태·신호·주문·체결·레그 리스크·잔고 불일치·오류·공백)
bars.csv       마감된 페어 봉(두 종가·체결 여부·스프레드·z)
signals.csv    신호마다 판정 값과 진입 여부, 막힌 이유
pair_legs.csv  다리 주문 하나 = 한 줄 (페어 ID · 역할 · 주문번호 · 체결수량 · 평균가 · 미끄러짐 · 지연)
pairs.csv      페어 주문 하나 = 한 줄 (결과 COMPLETE/ABORTED/UNWOUND/BROKEN · 단독 노출 시간)
trades.csv     진입~청산 한 건의 손익
api_calls.csv  API 호출마다 HTTP 상태·지연·오류코드
ticks.csv      WebSocket 시세체결(KA) 한 건 = 한 줄 (수신 시각 · 거래소 체결시각 · 체결가 · 체결수량 · 호가)
ws_orders.csv  WebSocket 주문/체결 통보(O1) 한 건 = 한 줄
run_meta.json  실행 설정 전부
summary.md     종료 때 요약
"""
from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))


class RunJournal:
    FILES = ("bars", "signals", "pair_legs", "pairs", "trades", "api_calls")

    def __init__(self, folder: Path, echo: bool = True) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self._events = (self.folder / "events.jsonl").open("a", encoding="utf-8")
        self._writers: dict[str, tuple[Any, csv.DictWriter]] = {}

    def _write(self, name: str, row: dict[str, Any]) -> None:
        if name not in self._writers:
            path = self.folder / f"{name}.csv"
            new = not path.exists()
            fh = path.open("a", encoding="utf-8-sig", newline="")
            w = csv.DictWriter(fh, fieldnames=list(row.keys()), extrasaction="ignore", restval="")
            if new:
                w.writeheader()
            self._writers[name] = (fh, w)
        fh, w = self._writers[name]
        w.writerow(row)
        fh.flush()

    # ---- 엔진·브로커가 부르는 것
    def bar(self, row: dict[str, Any]) -> None:
        self._write("bars", row)

    def signal(self, row: dict[str, Any]) -> None:
        self._write("signals", row)
        mark = "진입" if row.get("taken") else f"보류({row.get('blocked_by')})"
        self.event("signal", f"{row.get('signal')} z={row.get('z')} {row.get('reason')} → {mark}", quiet=True)

    def trade(self, row: dict[str, Any]) -> None:
        self._write("trades", row)
        self.event("trade", f"#{row['trade_no']} {row['direction']} {row['exit_reason']} "
                            f"스프레드 {row['spread_bp']}bp · 손익 {row['net_pl']:,}원 (하루 {row['daily_pnl_after']:,}원)")

    def leg(self, row: dict[str, Any]) -> None:
        self._write("pair_legs", row)

    def pair(self, row: dict[str, Any]) -> None:
        self._write("pairs", row)
        self.event("pair", f"페어 {row['pair_id']} {row['purpose']} → {row['status']} "
                           f"(삼성 {row['qty_s']}@{row['avg_s']} · 하이닉스 {row['qty_h']}@{row['avg_h']}, "
                           f"단독 노출 {row['exposure_ms']}ms) {row['message']}")

    def api_call(self, row: dict[str, Any]) -> None:
        self._write("api_calls", row)

    def tick(self, row: dict[str, Any]) -> None:
        self._write("ticks", row)

    def ws_order(self, row: dict[str, Any]) -> None:
        self._write("ws_orders", row)

    def event(self, kind: str, message: str, quiet: bool = False, **extra: Any) -> None:
        now = datetime.now(KST)
        self._events.write(json.dumps({"ts": now.isoformat(timespec="milliseconds"), "kind": kind,
                                       "message": message, **extra}, ensure_ascii=False) + "\n")
        self._events.flush()
        if self.echo and not quiet:
            print(f"{now:%H:%M:%S} [{kind}] {message}", flush=True)

    def meta(self, data: dict[str, Any]) -> None:
        (self.folder / "run_meta.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                                                   encoding="utf-8")

    def close(self) -> None:
        for fh, _ in self._writers.values():
            fh.close()
        self._events.close()


# ---------------------------------------------------------------- 요약
def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _f(x: str) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def summarize_run(folder: Path) -> Path:
    folder = Path(folder)
    trades = _read(folder / "trades.csv")
    pairs = _read(folder / "pairs.csv")
    legs = _read(folder / "pair_legs.csv")
    signals = _read(folder / "signals.csv")
    calls = _read(folder / "api_calls.csv")
    meta = json.loads((folder / "run_meta.json").read_text(encoding="utf-8")) if (folder / "run_meta.json").exists() else {}

    L = [f"# 실행 요약 — {meta.get('run_id', folder.name)}", "",
         f"- 모드 {meta.get('mode_label', '?')} · 환경 {meta.get('environment', '?')} · "
         f"삼성 {meta.get('symbols', {}).get('S', '?')} · 하이닉스 {meta.get('symbols', {}).get('H', '?')}",
         f"- 시작 {meta.get('started_at', '?')} · 종료 {meta.get('ended_at', '?')}", ""]

    pnl = [_f(t["net_pl"]) or 0 for t in trades]
    wins = [x for x in pnl if x > 0]
    loss = [x for x in pnl if x < 0]
    L += ["## 거래", "", "| 항목 | 값 |", "| --- | --- |",
          f"| 신호 / 진입 | {len(signals)} / {sum(1 for s in signals if s.get('taken') == '1')} |",
          f"| 완료 거래 | {len(trades)} (승 {len(wins)} · 패 {len(loss)}) |",
          f"| 순손익 | {sum(pnl):,.0f}원 |",
          f"| 평균 이익 / 평균 손실 | {statistics.mean(wins) if wins else 0:,.0f} / {statistics.mean(loss) if loss else 0:,.0f}원 |"]
    reasons: dict[str, list[float]] = {}
    for t in trades:
        reasons.setdefault(t["exit_reason"], []).append(_f(t["net_pl"]) or 0)
    for k, v in reasons.items():
        L.append(f"| 청산 {k} | {len(v)}건 {sum(v):,.0f}원 |")
    sp = [_f(t["spread_bp"]) for t in trades if _f(t["spread_bp"]) is not None]
    if sp:
        L.append(f"| 스프레드 변화(체결가 기준) 평균 | {statistics.mean(sp):.1f}bp |")
    L.append("")

    L += ["## 두 다리 주문 관리", "", "| 항목 | 값 |", "| --- | --- |"]
    st: dict[str, int] = {}
    for p in pairs:
        st[p["status"]] = st.get(p["status"], 0) + 1
    L.append(f"| 페어 주문 | {len(pairs)}건 — " + " · ".join(f"{k} {v}" for k, v in st.items()) + " |")
    exp = [_f(p["exposure_ms"]) for p in pairs if _f(p["exposure_ms"]) is not None]
    if exp:
        L.append(f"| 한쪽만 체결된 시간(첫 체결→두 다리 맞춤) | 중앙 {statistics.median(exp):,.0f}ms · 최대 {max(exp):,.0f}ms |")
    L.append(f"| 다리 주문 | {len(legs)}건 (재전송 {sum(1 for x in legs if x['role'] == 'retry')} · "
             f"되돌림 {sum(1 for x in legs if x['role'] == 'unwind')} · 잔고 정리 {sum(1 for x in legs if x['role'] == 'flatten')}) |")
    for name, label in (("S", "삼성"), ("H", "하이닉스")):
        sl = [_f(x["slippage_ticks"]) for x in legs if x["leg"] == name and _f(x["slippage_ticks"]) is not None]
        lt = [_f(x["latency_ms"]) for x in legs if x["leg"] == name and _f(x["latency_ms"]) is not None]
        if sl:
            L.append(f"| {label} 미끄러짐(판정가 대비) | 평균 {statistics.mean(sl):+.2f}틱 · 최대 {max(sl):+.1f}틱 ({len(sl)}건) |")
        if lt:
            L.append(f"| {label} 주문→체결 확인 | 중앙 {statistics.median(lt):,.0f}ms |")
    L.append("")

    conf = {}
    for x in legs:
        if x.get("confirm"):
            conf.setdefault(x["confirm"], []).append(_f(x["latency_ms"]))
    for k, v in conf.items():
        v = [y for y in v if y is not None]
        if v:
            L.insert(len(L) - 1,  # 두 다리 표 안(마지막 빈 줄 앞)에
                     f"| 체결 확인 경로 {k} | {len(v)}건 · 주문→확인 중앙 {statistics.median(v):,.0f}ms |")

    ticks = _read(folder / "ticks.csv")
    wso = _read(folder / "ws_orders.csv")
    ws_meta = meta.get("websocket", {})
    L += ["## 실시간(WebSocket)", "", "| 항목 | 값 |", "| --- | --- |",
          f"| 사용 | {'예' if ws_meta.get('enabled') else '아니오'} · 연결 {ws_meta.get('connects', 0)}번 · 끊김 {ws_meta.get('gaps', 0)}번 |",
          f"| 시세체결(KA) 수신 | {len(ticks):,}건 (삼성 {sum(1 for t in ticks if t['leg'] == 'S'):,} · "
          f"하이닉스 {sum(1 for t in ticks if t['leg'] == 'H'):,}) |",
          f"| 주문/체결 통보(O1) 수신 | {len(wso):,}건 |",
          f"| 분봉 출처 | 실시간 {ws_meta.get('bars_ws', 0):,}개 · REST 보충 {ws_meta.get('bars_rest', 0):,}개 |",
          f"| REST 시세 대체 조회 | {ws_meta.get('rest_quote_fallbacks', 0):,}회 |"]
    if ws_meta.get("clock_offset_median") is not None:
        L.append(f"| PC 시각 − 거래소 체결시각 | 중앙 {ws_meta['clock_offset_median']:+.2f}초 |")
    L.append("")

    errs = [c for c in calls if c.get("error_code")]
    L += ["## API", "", f"- 호출 {len(calls):,}회 · 오류 {len(errs)}회 "
          + ("(" + ", ".join(sorted({c['error_code'] for c in errs})) + ")" if errs else ""), ""]
    out = folder / "summary.md"
    out.write_text("\n".join(L), encoding="utf-8")
    return out
