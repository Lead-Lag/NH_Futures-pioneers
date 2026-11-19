#!/usr/bin/env python3
"""5주차 실행: 최근 신호(10월 텍스트 → 11월 포트폴리오) 중 주식선물이 있는 종목을 NH선물 모의계좌로 매수하고 기록합니다.

- 시세: 시작할 때만 REST(토큰·현재가·호가·주문가능수량·잔고), 이후 WebSocket 실시간(시세체결 KA · 주문/체결 통보 O1)
- 주문: 매도 1호가 지정가 1계약(거래가 얇은 개별주식선물이라 시장가 대신). --wait 초 안에 안 되면 남은 수량 취소.
       --market 을 주면 시장가. 주문은 절대 자동 재전송하지 않음(4주차 nh_client 그대로).
- 벤치마크: 코스피200 선물 12월물 시세를 함께 기록(진입 시점 가격) → 나중에 --mark 로 평가손익·초과수익 기록.
- 필요한 것: 4주차 nh_client.py · ws_feed.py(같은 폴더), requests · websockets (pandas 불필요)

사용:
  python live_order.py --dry-run            # ① 주문 없이 시세·호가·주문가능수량만 확인
  python live_order.py                      # ② 실제 모의 주문
  python live_order.py --mark               # ③ (나중에) 보유 종목 평가 — 현재가·벤치마크 기록
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nh_client import BusinessError, NHClient, TransportError, load_credentials, num

KST = timezone(timedelta(hours=9))

# 10월 텍스트 → 11월 포트폴리오(results/bt_discover_all_v2) 9종목 중 주식선물 상장 4종목 (stk_fut_opt.mst 확인)
TARGETS = {
    "LG씨엔에스": "A2K6C000",
    "현대오토에버": "A2R6C000",
    "로보티즈": "A2X6C000",
    "레인보우로보틱스": "A2W6C000",
}
BENCH = {"코스피200선물": "A016C000"}   # 12월물 추정 코드 — 시작 때 REST 로 확인, 실패하면 벤치마크 없이 진행
MULTIPLIER = 10


class Journal:
    """ws_feed.RealtimeFeed 가 부르는 event/tick/ws_order 와 우리 기록을 한 폴더에."""

    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, Any] = {}
        self._writers: dict[str, csv.DictWriter] = {}

    def _row(self, name: str, row: dict[str, Any]) -> None:
        if name not in self._writers:
            f = (self.out / f"{name}.csv").open("w", newline="", encoding="utf-8-sig")
            self._files[name] = f
            self._writers[name] = csv.DictWriter(f, fieldnames=list(row.keys()), extrasaction="ignore")
            self._writers[name].writeheader()
        self._writers[name].writerow(row)
        self._files[name].flush()

    def event(self, kind: str, msg: str, quiet: bool = False) -> None:
        ts = datetime.now(KST).isoformat(timespec="milliseconds")
        with (self.out / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "kind": kind, "msg": msg}, ensure_ascii=False) + "\n")
        if not quiet:
            print(f"[{ts[11:19]}] {kind}: {msg}")

    def tick(self, row: dict[str, Any]) -> None:
        self._row("ticks", row)

    def ws_order(self, row: dict[str, Any]) -> None:
        self._row("ws_orders", row)

    def api(self, row: dict[str, Any]) -> None:
        self._row("api_calls", row)

    def order(self, row: dict[str, Any]) -> None:
        self._row("orders", row)

    def raw(self, name: str, body: Any) -> None:
        (self.out / f"raw_{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")

    def close(self) -> None:
        for f in self._files.values():
            f.close()


def find_num(body: Any, *needles: str, avoid: tuple[str, ...] = ()) -> float:
    """응답에서 이름에 needles 가 모두 들어간 첫 숫자 칸(필드 이름을 정확히 몰라도 찾도록)."""
    stack = [body]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                kl = str(k).lower()
                if all(n in kl for n in needles) and not any(a in kl for a in avoid) and num(v) > 0:
                    return num(v)
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(cur)
    return 0.0


def find_str(body: Any, needle: str) -> str:
    stack = [body]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if needle in str(k).lower() and str(v).strip():
                    return str(v).strip()
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(cur)
    return ""


def rest_quote(c: NHClient, j: Journal, name: str, sym: str) -> dict[str, float]:
    """호가(price-bid) 응답: last_pric · Occurs1[0].occurs1_ask_best_pric / occurs1_bid_best_pric (11/19 원본으로 확인)."""
    b = c.price_bid(sym)
    j.raw(f"pricebid_{sym}", b)
    lvl = (b.get("Occurs1") or [{}])[0] if isinstance(b, dict) else {}
    q = {"last": num(b.get("last_pric")) if isinstance(b, dict) else 0.0,
         "ask": num(lvl.get("occurs1_ask_best_pric")), "bid": num(lvl.get("occurs1_bid_best_pric"))}
    if q["last"] <= 0:                                   # 체결이 없던 종목이면 현재가 API 로 한 번 더
        try:
            p = c.price(sym)
            j.raw(f"price_{sym}", p)
            q["last"] = num(p.get("last_pric")) if isinstance(p, dict) else 0.0
        except (BusinessError, TransportError) as e:
            j.event("rest", f"{name} 현재가 조회 실패: {e}")
    return q


def wait_fill(c: NHClient, feed: Any, ord_no: str, wait: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        st = feed.order_state(ord_no) if feed else None
        if st and (st.get("filled", 0) >= 1 or st.get("status") in ("8", "9")):
            return {"filled": st["filled"], "avg": st["avg"], "via": "ws", "status": st.get("status"), "err": st.get("err")}
        time.sleep(0.3)
    f = c.order_fills(ord_no)                      # 실시간 통보가 안 왔으면 주문내역으로
    return ({"filled": f["filled"], "avg": f["avg"], "via": "rest", "status": f["status"], "err": f["reject"]} if f else None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.local.json")
    ap.add_argument("--mode", default="paper", choices=["paper"])
    ap.add_argument("--dry-run", action="store_true", help="주문 없이 시세·호가·주문가능수량만")
    ap.add_argument("--market", action="store_true", help="시장가 주문(기본은 매도 1호가 지정가)")
    ap.add_argument("--wait", type=float, default=30.0, help="지정가 체결 대기(초), 넘으면 취소")
    ap.add_argument("--listen", type=float, default=20.0, help="실시간 시세를 모으는 시간(초)")
    ap.add_argument("--mark", action="store_true", help="보유 종목·벤치마크 현재가 기록(나중 평가용)")
    ap.add_argument("--no-ws", action="store_true")
    a = ap.parse_args()

    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    j = Journal(Path("results") / (f"live_mark_{stamp}" if a.mark else f"live_{stamp}"))
    c = NHClient(load_credentials(a.config, a.mode), token_cache=Path(".token_cache.json"), call_logger=j.api)
    j.event("start", f"모드={a.mode} dry_run={a.dry_run} market={a.market} 대상={list(TARGETS)}")

    # ---------------- 시작: REST
    syms = dict(TARGETS)
    quotes: dict[str, dict[str, float]] = {}
    for name, sym in {**TARGETS, **BENCH}.items():
        try:
            quotes[name] = rest_quote(c, j, name, sym)
            j.event("rest", f"{name}({sym}) 현재가 {quotes[name].get('last')} · 매수1 {quotes[name].get('bid')} · 매도1 {quotes[name].get('ask')}")
            if name in BENCH:
                syms.update({name: sym})
        except (BusinessError, TransportError) as e:
            j.event("rest", f"{name}({sym}) 시세 조회 실패 — {'벤치마크 없이 진행' if name in BENCH else '이 종목 제외'}: {e}")
            syms.pop(name, None)

    if a.mark:
        rows = []
        for name, sym in {**TARGETS, **BENCH}.items():
            q = quotes.get(name, {})
            pos = c.position_of(sym) if name in TARGETS else ""
            rows.append({"ts": datetime.now(KST).isoformat(timespec="seconds"), "name": name, "sym": sym,
                         "last": q.get("last", ""), "position": pos})
        for r in rows:
            j._row("marks", r)
        j.event("mark", f"현재가·잔고 기록 → {j.out / 'marks.csv'}")
        # 가장 최근 실제 주문 기록(체결 1 이상)과 비교해 수익률·초과수익 계산
        entries = []
        for d in sorted(Path("results").glob("live_2*"), reverse=True):
            f = d / "orders.csv"
            if not f.exists():
                continue
            with f.open(encoding="utf-8-sig") as fh:
                got = [r for r in csv.DictReader(fh) if num(r.get("filled")) >= 1]
            if got:
                entries = got
                j.event("mark", f"진입 기록: {d.name}")
                break
        last = {r["name"]: num(r["last"]) for r in rows}
        rets = []
        for e in entries:
            px = last.get(e["name"], 0)
            if px > 0 and num(e["avg"]) > 0:
                r = px / num(e["avg"]) - 1
                rets.append(r)
                j._row("mark_returns", {"name": e["name"], "entry": e["avg"], "now": px, "ret": round(r, 4),
                                        "pnl_krw": round((px - num(e["avg"])) * MULTIPLIER)})
                j.event("mark", f"{e['name']}: {num(e['avg']):,.0f} → {px:,.0f} ({r:+.2%}, {(px - num(e['avg'])) * MULTIPLIER:+,.0f}원)")
        b0 = num(entries[0].get("bench_last")) if entries else 0
        b1 = last.get(next(iter(BENCH)), 0)
        if rets and b0 > 0 and b1 > 0:
            avg, br = sum(rets) / len(rets), b1 / b0 - 1
            j.event("mark", f"단순 평균 {avg:+.2%} · 코스피200 선물 {br:+.2%} · 초과 {avg - br:+.2%} ({len(rets)}종목)")
        j.close()
        return

    # ---------------- 실시간: WebSocket
    feed = None
    if not a.no_ws:
        from ws_feed import RealtimeFeed
        feed = RealtimeFeed(c, syms, j, orders=True)
        feed.bars = {n: {} for n in syms}             # 4주차 파일은 두 종목(S·H) 전제라 이름별로 다시 만듦
        ok = feed.start(wait=15)
        j.event("ws", "실시간 연결됨" if ok else "실시간 연결 실패 — REST 호가로 진행")
        if ok:
            time.sleep(a.listen)

    def best(name: str) -> dict[str, float]:
        live = feed.quote(name) if feed else None
        if live and live.get("ask", 0) > 0:
            return {"last": live["last"], "bid": live["bid"], "ask": live["ask"], "src": "ws"}
        q = dict(quotes.get(name, {}))
        q["src"] = "rest"
        return q

    bench_px = best(next(iter(BENCH))) if next(iter(BENCH)) in syms else {}
    j.event("bench", f"벤치마크 진입 시점 가격 {bench_px}")

    # ---------------- 주문
    for name, sym in TARGETS.items():
        if name not in syms:
            continue
        q = best(name)
        try:
            oq = c.orderable_quantity(sym, side="BUY", price=str(int(q.get("ask") or q.get("last") or 0)))
            j.raw(f"orderable_{sym}", oq)
        except (BusinessError, TransportError) as e:
            j.event("rest", f"{name} 주문가능수량 조회 실패: {e}")
        ask = q.get("ask", 0)
        row = {"ts": datetime.now(KST).isoformat(timespec="milliseconds"), "name": name, "sym": sym, "side": "BUY",
               "qty": 1, "type": "market" if a.market else "limit", "price": "" if a.market else ask,
               "quote_src": q.get("src"), "last": q.get("last"), "bid": q.get("bid"), "ask": ask,
               "bench_last": bench_px.get("last", ""), "ord_no": "", "filled": 0, "avg": "", "via": "", "status": "", "note": ""}
        ref = q.get("last") or 0
        if not a.market and (ask <= 0 or (ref > 0 and abs(ask / ref - 1) > 0.3)):
            row["note"] = f"매도 1호가 {ask} 가 현재가 {ref} 와 맞지 않음 — 주문 안 함(raw_pricebid_*.json 확인)"
            j.order(row); j.event("order", f"{name}: {row['note']}")
            continue
        if a.dry_run:
            row["note"] = "dry-run — 주문 안 함"
            j.order(row); j.event("order", f"{name}: {'시장가' if a.market else f'지정가 {ask:,.0f}'} 1계약 (dry-run)")
            continue
        try:
            body = c.order(sym, side="BUY", qty=1, order_type="2" if a.market else "1",
                           price="0" if a.market else str(int(ask)))
            j.raw(f"order_{sym}", body)
            ord_no = find_str(body, "ord_no")
        except TransportError as e:
            row["note"] = f"응답 없음 — 재전송하지 않음, 주문내역 확인 필요: {e}"
            j.order(row); j.event("order", f"{name}: {row['note']}")
            continue
        except BusinessError as e:
            row["note"] = f"거부: {e}"
            j.order(row); j.event("order", f"{name}: {row['note']}")
            continue
        row["ord_no"] = ord_no
        res = wait_fill(c, feed, ord_no, a.wait) if ord_no else None
        if res:
            row.update({"filled": res["filled"], "avg": res["avg"], "via": res["via"], "status": res["status"], "note": res.get("err") or ""})
        if not a.market and ord_no and row["filled"] < 1:
            try:
                c.cancel(sym, org_ord_no=ord_no, side="BUY", qty=1)
                time.sleep(1.0)
                f = c.order_fills(ord_no)            # 취소 직전 체결 확인
                if f and f["filled"] >= 1:
                    row.update({"filled": f["filled"], "avg": f["avg"], "via": "rest", "note": "취소 직전 체결"})
                else:
                    row["note"] = f"{a.wait:.0f}초 미체결 → 취소"
            except (BusinessError, TransportError) as e:
                row["note"] = f"취소 실패: {e}"
        j.order(row)
        j.event("order", f"{name}: 주문번호 {ord_no or '?'} · 체결 {row['filled']} @ {row['avg']} ({row['via']}) {row['note']}")

    # ---------------- 잔고 대조
    for name, sym in TARGETS.items():
        try:
            j.event("position", f"{name}({sym}) 잔고 {c.position_of(sym)}계약")
        except (BusinessError, TransportError) as e:
            j.event("position", f"{name} 잔고 조회 실패: {e}")
    if feed:
        feed.stop()
        j.event("ws", f"실시간 수신 {feed.ticks}건 · 연결 {feed.connects}번")
    j.event("done", f"기록 폴더: {j.out}")
    j.close()


if __name__ == "__main__":
    main()
