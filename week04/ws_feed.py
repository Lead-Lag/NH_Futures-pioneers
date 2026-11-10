"""NH선물 실시간(WebSocket) 수신 — 시세체결(KA) 두 종목 + 국내 주문/체결 통보(O1).

운영진 안내대로 **시세는 처음에만 REST 로 받고(오늘 분봉으로 지표 준비), 그 뒤로는 WebSocket 실시간 값**을 씁니다.
REST 시세 조회는 WebSocket 이 끊긴 동안의 대체와, 끊겨서 빠진 분봉을 메우는 데만 씁니다.

접속 절차 (공개 저장소 backend/realtime/link.py 와 실제 기록 tests/fixtures/nh/KA · O1_K 에서 확인)
  1) GET /permission/v1/web-socket-key → access_key   (연결 1회용 — 다시 붙을 때마다 새로 받음)
  2) wss://<접속주소>/trade/ws-stream?access_key=...  (웹소켓 규격 ping 프레임은 끔 — 켜면 국내 연결이 끊김)
  3) 세션 초기화 {"header":{"api_ver":"1","action":"0"},"body":{"action_code":"session_init","session_token":<토큰>}}
     → {"code":"INIT","msg":"INIT_SUCCESS"}
  4) 구독 {"header":{"api_ver":"1","action":"A"},"body":{"action_code":"KA","action_sym":<종목>}} · O1 은 종목 없이
     → {"code":"RT_INIT",...}  (거절이면 RT_FAIL 등 다른 코드)
  5) 서버의 {"code":"pingpong"} 은 받은 그대로 되돌려야 세션이 유지됨
  6) 자료: {"header":{"action_code":"KA","action_sym":...},"body":{last_pric, exec_qty, exch_tm, bid/ask_best_pric, ...}}
           {"header":{"action_code":"O1",...},"body":{ord_no, ord_sts(1접수·3확인·4부분·6체결·8자동취소·9거부),
            exec_qty(누적), per_exec_qty(이번), exec_pric, rem_ord_qty, cxl_qty, err_cd, ...}}

1분봉: 시세체결 한 건 = 체결 한 건이라, 거래소 체결시각(exch_tm)의 분으로 묶어 종가(마지막 체결가)·거래량을 만듭니다.
끊겼던 시간(연결 전·끊김~재연결)에 걸친 분은 「불완전」으로 표시하고, 실행 쪽이 그 분만 REST 분봉으로 채웁니다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse

from nh_client import NHApiError, NHClient, num

KST = timezone(timedelta(hours=9))
STREAM_PATH = "/trade/ws-stream"
IDLE_TIMEOUT = 90.0                    # 이만큼 아무것도 안 오면 죽은 연결로 보고 다시 붙음
BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)  # 다시 붙기 전 기다림(초)
QUOTE_FRESH_SEC = 15.0                 # 실시간 연결이 살아 있다고 보는 마지막 수신 뒤 시간


def _ws_url(base: str, key: str) -> str:
    u = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{u}{STREAM_PATH}?access_key={quote(key, safe='')}"


def _msg(action: str, body: dict[str, Any]) -> str:
    return json.dumps({"header": {"api_ver": "1", "action": action}, "body": body}, ensure_ascii=False)


class RealtimeFeed:
    def __init__(self, client: NHClient, syms: dict[str, str], journal: Any, *, orders: bool = True) -> None:
        self.c = client
        self.syms = syms                       # {"S": 코드, "H": 코드}
        self.name_of = {v: k for k, v in syms.items()}
        self.j = journal
        self.want_orders = orders
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None
        self.live = False
        self.live_since: datetime | None = None
        self.last_msg: datetime | None = None
        self.gaps: list[tuple[datetime, datetime | None]] = []   # (끊긴 시각, 다시 살아난 시각)
        self.quotes: dict[str, dict[str, Any]] = {}             # 이름 → {last, bid, ask, exch_tm, ts}
        self.bars: dict[str, dict[datetime, dict[str, float]]] = {"S": {}, "H": {}}
        self.orders: dict[str, dict[str, Any]] = {}              # 주문번호 → 상태
        self.ticks = 0
        self.connects = 0
        self.clock_offsets: list[float] = []   # PC 시각 − 거래소 체결시각(초) — 분 묶음이 맞는지 점검용

    # ------------------------------------------------------------ 시작·정지
    def start(self, wait: float = 10.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="nh-ws", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not self.live:
            time.sleep(0.1)
        return self.live

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------ 실행 쪽이 읽는 것
    def healthy(self) -> bool:
        return bool(self.live and self.last_msg and
                    (datetime.now(KST) - self.last_msg).total_seconds() < IDLE_TIMEOUT)

    def quote(self, name: str) -> dict[str, Any] | None:
        """그 종목의 최신 실시간 시세. 연결이 죽었으면 None (오래된 값을 쓰지 않음)."""
        if not self.healthy():
            return None
        with self._lock:
            q = self.quotes.get(name)
            return dict(q) if q else None

    def complete_since(self) -> datetime | None:
        """이 시각 이후 시작한 분봉은 실시간으로 빠짐없이 받았다고 볼 수 있는 첫 분(연결된 다음 분)."""
        if not self.live_since:
            return None
        return self.live_since.replace(second=0, microsecond=0) + timedelta(minutes=1)

    def minute_complete(self, start: datetime) -> bool:
        """그 분 동안 연결이 계속 살아 있었는지."""
        first = self.complete_since()
        if first is None or start < first:
            return False
        end = start + timedelta(minutes=1)
        for down, up in self.gaps:
            if down < end and (up is None or up > start):
                return False
        return bool(self.live or (self.gaps and self.gaps[-1][0] >= end))

    def bars_of(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            return [{"start": t, "close": b["close"], "volume": b["volume"]} for t, b in sorted(self.bars[name].items())]

    def order_state(self, ord_no: str) -> dict[str, Any] | None:
        with self._lock:
            o = self.orders.get(ord_no.lstrip("0"))
            return dict(o) if o else None

    # ------------------------------------------------------------ 연결 유지
    def _run(self) -> None:
        tries = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._session()
            except Exception as exc:  # 연결 실패·끊김 모두 다시 붙음
                if not self._stop.is_set():
                    self.j.event("ws", f"실시간 연결 끊김/실패: {type(exc).__name__} {str(exc)[:160]}")
            finally:
                if self.live:
                    self.gaps.append((datetime.now(KST), None))
                self.live = False
            if self._stop.is_set():
                break
            tries = 0 if time.monotonic() - started > 60 else tries + 1
            wait = BACKOFF[min(tries, len(BACKOFF) - 1)]
            self.j.event("ws", f"{wait:.0f}초 뒤 다시 연결 (그동안 시세는 REST 로 대체)")
            self._stop.wait(wait)

    def _session(self) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        key = self.c.websocket_key()
        url = _ws_url(os.environ.get("NH_WS_BASE") or self.c.creds.url, key)   # NH_WS_BASE: 시험용 가짜 서버 주소
        local = urlparse(url).hostname in ("127.0.0.1", "localhost")
        kw: dict[str, Any] = {"open_timeout": 10, "close_timeout": 2, "ping_interval": None, "ping_timeout": None}
        if local:
            kw["proxy"] = None
        with connect(url, **kw) as ws:
            self._ws = ws
            ws.send(_msg("0", {"action_code": "session_init", "session_token": self.c.session_token}))
            deadline = time.monotonic() + 10
            while True:
                raw = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                m = json.loads(raw)
                if str(m.get("code", "")).lower() == "pingpong":
                    ws.send(raw)
                    continue
                if m.get("code") == "INIT" and m.get("msg") == "INIT_SUCCESS":
                    break
                raise ConnectionError(f"세션 초기화 거절: {str(m)[:200]}")
            for name, sym in self.syms.items():
                ws.send(_msg("A", {"action_code": "KA", "action_sym": sym}))
            if self.want_orders:
                ws.send(_msg("A", {"action_code": "O1"}))
            now = datetime.now(KST)
            self.connects += 1
            if self.gaps and self.gaps[-1][1] is None:
                self.gaps[-1] = (self.gaps[-1][0], now)
            if self.live_since is None:
                self.live_since = now
            self.live, self.last_msg = True, now
            self.j.event("ws", f"실시간 연결 #{self.connects} — 시세체결 KA {list(self.syms.values())}"
                               + (" · 주문/체결 통보 O1" if self.want_orders else "") + " 구독")
            while not self._stop.is_set():
                try:
                    raw = ws.recv(timeout=IDLE_TIMEOUT)
                except TimeoutError:
                    raise TimeoutError(f"{IDLE_TIMEOUT:.0f}초 동안 수신 없음")
                except ConnectionClosed as exc:
                    raise ConnectionError(f"서버가 닫음 code={getattr(exc.rcvd, 'code', '')}") from exc
                self.last_msg = datetime.now(KST)
                self._handle(ws, raw)

    # ------------------------------------------------------------ 메시지
    def _handle(self, ws, raw: str) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        code = m.get("code")
        if code is not None:
            if str(code).lower() == "pingpong":
                ws.send(raw)
            elif code == "RT_INIT":
                d = m.get("data") or {}
                self.j.event("ws", f"구독 확인 {d.get('action_code')} {d.get('action_sym', '')}".strip(), quiet=True)
            else:
                self.j.event("ws", f"실시간 서버 응답 {code}: {str(m.get('msg') or m.get('message') or '')[:160]}")
            return
        h, b = m.get("header") or {}, m.get("body") or {}
        ac = str(h.get("action_code") or "")
        if ac == "KA":
            self._on_tick(str(h.get("action_sym") or b.get("sym") or ""), b)
        elif ac == "O1":
            self._on_order(b)

    def _on_tick(self, sym: str, b: dict[str, Any]) -> None:
        name = self.name_of.get(sym)
        if not name:
            return
        px, qty = num(b.get("last_pric")), num(b.get("exec_qty"))
        tm = str(b.get("exch_tm") or "").zfill(6)
        now = datetime.now(KST)
        try:
            ex_t = datetime.combine(now.date(), datetime.strptime(tm, "%H%M%S").time(), KST)
        except ValueError:
            ex_t = now
        start = ex_t.replace(second=0, microsecond=0)
        with self._lock:
            self.ticks += 1
            self.quotes[name] = {"last": px, "bid": num(b.get("bid_best_pric")), "ask": num(b.get("ask_best_pric")),
                                 "exch_tm": tm, "ts": now}
            if px > 0:
                bar = self.bars[name].setdefault(start, {"close": px, "volume": 0.0})
                bar["close"] = px
                bar["volume"] += qty
            if len(self.clock_offsets) < 5000:
                self.clock_offsets.append((now - ex_t).total_seconds())
        self.j.tick({"recv_ts": now.isoformat(timespec="milliseconds"), "leg": name, "sym": sym, "exch_tm": tm,
                     "last": px, "exec_qty": qty, "bid": b.get("bid_best_pric", ""), "ask": b.get("ask_best_pric", ""),
                     "cum_qty": b.get("cum_trd_qty", "")})

    def _on_order(self, b: dict[str, Any]) -> None:
        no = str(b.get("ord_no") or "").strip().lstrip("0")
        if not no:
            return
        now = datetime.now(KST)
        per, px = num(b.get("per_exec_qty")), num(b.get("exec_pric"))
        with self._lock:
            o = self.orders.setdefault(no, {"filled": 0, "notional": 0.0, "avg": 0.0, "status": "", "rem": None,
                                            "cancelled": 0, "err": "", "first_ts": now, "last_ts": now, "execs": set(),
                                            "exec_sum": 0.0})
            exec_no = str(b.get("exec_no") or "").strip().lstrip("0")
            if per > 0 and px > 0 and exec_no and exec_no not in o["execs"]:
                o["execs"].add(exec_no)
                o["notional"] += per * px
                o["exec_sum"] += per
            # exec_qty 는 누적 체결수량. 없으면 이번 체결분을 더한 값
            o["filled"] = max(o["filled"], int(num(b.get("exec_qty"))), int(o["exec_sum"]))
            if o["exec_sum"] > 0:
                o["avg"] = round(o["notional"] / o["exec_sum"], 2)
            o["status"] = str(b.get("ord_sts") or o["status"])
            rem = b.get("rem_ord_qty")
            o["rem"] = int(num(rem)) if rem not in (None, "") else o["rem"]
            o["cancelled"] = max(o["cancelled"], int(num(b.get("cxl_qty"))))
            o["err"] = str(b.get("err_cd") or "").strip() or o["err"]
            o["last_ts"] = now
        self.j.ws_order({"recv_ts": now.isoformat(timespec="milliseconds"), "ord_no": no, "sym": b.get("sym", ""),
                         "trd_div": b.get("trd_div", ""), "ord_sts": b.get("ord_sts", ""), "ord_qty": b.get("ord_qty", ""),
                         "exec_qty": b.get("exec_qty", ""), "per_exec_qty": b.get("per_exec_qty", ""),
                         "exec_pric": b.get("exec_pric", ""), "rem_ord_qty": b.get("rem_ord_qty", ""),
                         "cxl_qty": b.get("cxl_qty", ""), "err_cd": b.get("err_cd", ""), "exec_tm": b.get("exec_tm", "")})
