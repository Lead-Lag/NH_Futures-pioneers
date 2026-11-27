"""키 없이 페어 주문 흐름을 점검하는 가짜 NH API 서버(응답 모양만 실제와 같음).

시나리오(환경변수 MOCK_SCENARIO 또는 POST /mock/scenario {"name": ...}):
  normal         모든 시장가 주문이 바로 전량 체결
  leg2_never     삼성(둘째 다리) 주문은 접수만 되고 체결 안 됨 → 취소하면 취소됨
  leg2_partial   삼성 매도 주문은 요청 수량의 절반만 체결, 나머지는 취소로(되돌리는 매수는 정상 체결)
  leg1_reject    하이닉스(첫 다리) 주문 거부(msg_cd)
  cancel_race    삼성 주문이 처음엔 미체결, 취소 요청이 오는 순간 전량 체결(취소 직전 체결)
  split_fills    삼성 주문이 체결 두 번으로 나뉘어 주문내역에 두 줄로 옴
  timeout_once   첫 삼성 주문은 접수하되 응답을 늦게 줘서 클라이언트 시간 초과를 냄
  token_flaky    정상 토큰이어도 세 번에 한 번 401 EGW023 「유효하지 않은 토큰」(11/10 실제 관찰 재현)
  ws_drop        실시간 연결을 붙은 지 8초 뒤 한 번 끊음(재연결·REST 보충 시험)
  ws_silent_o1   실시간 주문/체결 통보(O1)를 보내지 않음 → 주문내역 조회로 확인하는지 시험

실시간(WebSocket): GET /permission/v1/web-socket-key 로 접속키, serve_ws(포트)로 띄운 서버가
세션 초기화·구독·pingpong·시세체결(KA, 0.3초마다)·주문/체결 통보(O1)를 실제와 같은 모양으로 보냅니다.

실행:  python tests/mock_server.py 8799        (실시간은 8800)
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KST = timezone(timedelta(hours=9))
SAM, HYN = "A116C000", "A506C000"
PRICE = {SAM: 188500.0, HYN: 1116000.0}
TICK = {SAM: 100.0, HYN: 1000.0}

STATE = {"scenario": os.environ.get("MOCK_SCENARIO", "normal"), "orders": [], "pos": {SAM: 0, HYN: 0},
         "n": 7000000000, "timeout_used": False}
LOCK = threading.Lock()
_T0 = datetime.now(KST)
LIVE_FIRST = max(_T0.hour * 60 + _T0.minute - 150, 0)


def _bars(sym: str, day: str, st: str, ed: str) -> list[dict]:
    random.seed(int(day))   # 두 종목이 같은 경로를 쓰도록 날짜로만 씨앗
    now = datetime.now(KST)
    out = []
    lh, dev = math.log(PRICE[HYN]), 0.0
    # MOCK_LIVE=1 이면 장 시간과 상관없이 「지금까지 150분」 봉을 만들어 실시간 반복을 시험
    first = LIVE_FIRST if os.environ.get("MOCK_LIVE") else 8 * 60 + 45
    for m in range(first, 15 * 60 + 46 if not os.environ.get("MOCK_LIVE") else 24 * 60):
        t = datetime.strptime(day, "%Y%m%d").replace(tzinfo=KST) + timedelta(minutes=m)
        lh += random.gauss(0, 0.001)
        dev = 0.9 * dev + random.gauss(0, 0.0008)
        if t + timedelta(minutes=1) > now:
            break
        px = math.exp(lh) if sym == HYN else math.exp(math.log(PRICE[SAM]) + 0.59 * (lh - math.log(PRICE[HYN])) + dev)
        px = round(px / TICK[sym]) * TICK[sym]
        hhmmss = t.strftime("%H%M%S")
        if st <= hhmmss <= ed:
            out.append({"occurs1_ref_dt": day, "occurs1_exch_tm": hhmmss, "occurs1_open_pric": str(px),
                        "occurs1_high_pric": str(px), "occurs1_low_pric": str(px), "occurs1_close_pric": str(px),
                        "occurs1_cum_trd_qty": "12"})
    return list(reversed(out))


WS_CLIENTS: list = []           # (연결, 구독 집합)
WS_LIVE_PX = {SAM: PRICE[SAM], HYN: PRICE[HYN]}


def _push_o1(o: dict, sts: str, per: int = 0, px: float = 0.0) -> None:
    if STATE["scenario"] == "ws_silent_o1":
        return
    filled = sum(e["qty"] for e in o["execs"])
    body = {"ord_no": o["no"], "sym": o["sym"], "trd_div": o["side"], "ord_sts": sts, "ord_qty": str(o["qty"]),
            "exec_qty": f"{filled:07d}", "per_exec_qty": f"{per:07d}", "exec_pric": str(px) if px else "000000000000000",
            "exec_no": str(len(o["execs"])) if per else "0000000000", "rem_ord_qty": str(o["qty"] - filled - o["cxl"]),
            "cxl_qty": f"{o['cxl']:07d}", "err_cd": "", "exec_tm": datetime.now(KST).strftime("%H%M%S") if per else "",
            "rcv_tm": o["tm"], "pric_cnd": "2"}
    msg = json.dumps({"header": {"action_acnt": "000", "action_code": "O1"}, "body": body}, ensure_ascii=False)
    for ws, subs in list(WS_CLIENTS):
        if "O1" in subs:
            try:
                ws.send(msg)
            except Exception:
                pass


def _fill(o: dict, qty: int, px: float) -> None:
    o["execs"].append({"no": f"{len(o['execs']) + 1:06d}", "qty": qty, "px": px})
    sign = 1 if o["side"] == "1" else -1
    STATE["pos"][o["sym"]] += sign * qty
    filled = sum(e["qty"] for e in o["execs"])
    _push_o1(o, "6" if filled >= o["qty"] else "4", qty, px)


def _rows() -> list[dict]:
    rows = []
    for o in STATE["orders"]:
        base = {"occurs1_ord_no": o["no"], "occurs1_sym": o["sym"], "occurs1_trd_div": o["side"],
                "occurs1_exch_rcv_tm": o["tm"] + "000", "occurs1_ord_rejt_rsn": o.get("reject", ""),
                "occurs1_cxl_qty": f"{o['cxl']:.9f}", "occurs1_ord_sts_nm": o["sts"],
                "occurs1_avg_exec_pric": "0", "occurs1_ord_qty": str(o["qty"])}
        if STATE["scenario"] == "split_fills" and len(o["execs"]) > 1:
            for e in o["execs"]:
                rows.append({**base, "occurs1_exch_exec_no": e["no"], "occurs1_exec_qty": f"{e['qty']:.9f}",
                             "occurs1_exec_pric": f"{e['px']:.9f}"})
        else:
            q = sum(e["qty"] for e in o["execs"])
            px = (sum(e["qty"] * e["px"] for e in o["execs"]) / q) if q else 0.0
            rows.append({**base, "occurs1_exch_exec_no": "0", "occurs1_exec_qty": f"{q:.9f}",
                         "occurs1_exec_pric": f"{px:.9f}"})
    return rows


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/web-socket-key"):
            if self.headers.get("Authorization") != "Bearer T":
                return self._send({"errorCode": "EGW023", "errorMessage": "유효하지 않은 토큰입니다."}, 401)
            STATE["n"] += 1
            return self._send({"access_key": f"K{STATE['n']}"})
        return self._send({"errorCode": "9999"}, 404)

    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # timeout_once: 클라이언트가 먼저 끊음

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode()
        if self.path == "/mock/scenario":
            with LOCK:
                STATE["scenario"] = json.loads(raw)["name"]
                STATE["orders"].clear()
                STATE["pos"] = {SAM: 0, HYN: 0}
                STATE["timeout_used"] = False
                STATE["dropped"] = False
            return self._send({"ok": True})
        if self.path == "/mock/state":
            return self._send({"pos": STATE["pos"], "orders": len(STATE["orders"])})
        if self.path.endswith("/token"):
            return self._send({"access_token": "T", "token_type": "Bearer", "expires_in": 86400})
        if self.headers.get("Authorization") != "Bearer T":
            return self._send({"errorCode": "EGW00123", "errorMessage": "token"}, 401)
        if STATE["scenario"] == "token_flaky":
            with LOCK:
                STATE["flaky"] = STATE.get("flaky", 0) + 1
                reject = STATE["flaky"] % 3 == 0
            if reject:
                return self._send({"errorCode": "EGW023", "errorMessage": "유효하지 않은 토큰입니다."}, 401)
        body = json.loads(raw or "{}")
        sc = STATE["scenario"]
        p = self.path
        if p.endswith("/chart-minute"):
            return self._send({"Occurs1": _bars(body["sym"], body["inq_strt_dt"], body["strt_tm"], body["ed_tm"])[:300]})
        if p.endswith("/price"):
            s = body["sym"]
            return self._send({"sym_nm": "삼성전자   F 202612 (  10)" if s == SAM else "SK하이닉스 F 202612 (  10)",
                               "last_pric": str(PRICE[s]), "ask_pric": str(PRICE[s] + TICK[s]),
                               "bid_pric": str(PRICE[s]), "lst_trd_dt": "20261210", "rem_dy": "30", "mltplr": "10"})
        if p.endswith("/orderable-quantity"):
            return self._send({"ord_psb_qty": "50", "msg_cd": ""})
        if p.endswith("/open-interest"):
            rows = [{"occurs1_sym": s, "occurs1_opint_qty": f"{abs(q):.9f}", "occurs1_trd_div": "1" if q > 0 else "2"}
                    for s, q in STATE["pos"].items() if q]
            return self._send({"Occurs1": rows, "nxt_key": ""})
        if p.endswith("/order-history"):
            with LOCK:
                return self._send({"Occurs1": _rows(), "nxt_key": ""})
        if p.endswith("/order"):
            with LOCK:
                s, qty = body["sym"], int(body["ord_qty"])
                if sc == "leg1_reject" and s == HYN:
                    return self._send({"msg_cd": "4001", "msg_cntn": "주문가능수량 부족(가짜)", "ord_no": ""})
                STATE["n"] += 1
                o = {"no": str(STATE["n"]), "sym": s, "side": body["trd_div"], "qty": qty, "execs": [], "cxl": 0,
                     "sts": "접수", "tm": datetime.now(KST).strftime("%H%M%S")}
                STATE["orders"].append(o)
                _push_o1(o, "1")
                px = PRICE[s] + (TICK[s] if body["trd_div"] == "1" else 0)
                slow = sc == "timeout_once" and s == SAM and not STATE["timeout_used"]
                if slow:
                    STATE["timeout_used"] = True
                if s == SAM and sc in ("leg2_never", "cancel_race"):
                    pass
                elif s == SAM and sc == "leg2_partial" and body["trd_div"] == "2":
                    _fill(o, max(1, qty // 2), px)
                elif s == SAM and sc == "split_fills" and qty > 1:
                    _fill(o, qty // 2, px)
                    _fill(o, qty - qty // 2, px + TICK[s])
                else:
                    _fill(o, qty, px)
                o["sts"] = "체결" if o["execs"] else "접수"
            if slow:
                time.sleep(4)
            return self._send({"msg_cd": "", "msg_cntn": "", "ord_no": o["no"]})
        if p.endswith("/cancel"):
            with LOCK:
                o = next((x for x in STATE["orders"] if x["no"].lstrip("0") == body["org_ord_no"].lstrip("0")), None)
                if not o:
                    return self._send({"msg_cd": "5001", "msg_cntn": "원주문 없음"})
                filled = sum(e["qty"] for e in o["execs"])
                if sc == "cancel_race":
                    _fill(o, o["qty"] - filled, PRICE[o["sym"]])
                    o["sts"] = "체결"
                    return self._send({"msg_cd": "5002", "msg_cntn": "취소 가능 수량 없음(이미 체결)"})
                o["cxl"] = o["qty"] - filled
                o["sts"] = "취소"
            return self._send({"msg_cd": "", "msg_cntn": "", "ord_no": str(int(o["no"]) + 1)})
        return self._send({"errorCode": "9999", "errorMessage": f"unknown {p}"})


def _ws_handler(ws) -> None:
    path = getattr(getattr(ws, "request", None), "path", "") or ""
    if "/trade/ws-stream" not in path or "access_key=K" not in path:
        ws.close(4001, "bad key")
        return
    first = json.loads(ws.recv(timeout=5))
    if first.get("body", {}).get("session_token") != "T":
        ws.send(json.dumps({"code": "INIT", "msg": "INIT_FAIL"}))
        return
    ws.send(json.dumps({"code": "INIT", "msg": "INIT_SUCCESS"}))
    subs: set = set()
    entry = (ws, subs)
    WS_CLIENTS.append(entry)
    opened, last_ping, last_tick = time.monotonic(), time.monotonic(), 0.0
    drop = STATE["scenario"] == "ws_drop" and not STATE.get("dropped")
    rnd = random.Random(7)
    try:
        while True:
            try:
                raw = ws.recv(timeout=0.1)
                m = json.loads(raw)
                if m.get("code") == "pingpong":
                    continue
                b = m.get("body", {})
                key = b.get("action_code") if b.get("action_code") == "O1" else b.get("action_sym")
                if m.get("header", {}).get("action") == "A" and key:
                    subs.add(key)
                    ws.send(json.dumps({"code": "RT_INIT", "data": {"action": "A", "action_code": b.get("action_code"),
                                                                    "action_sym": b.get("action_sym", "")}, "msg": ""}))
            except TimeoutError:
                pass
            now = time.monotonic()
            if drop and now - opened > 8:
                STATE["dropped"] = True
                ws.close(1011, "mock drop")
                return
            if now - last_ping > 5:
                ws.send(json.dumps({"code": "pingpong"}))
                last_ping = now
            if now - last_tick > 0.3:
                last_tick = now
                common = rnd.gauss(0, 0.0004)
                for sym in (SAM, HYN):
                    if sym not in subs:
                        continue
                    r = common * (0.59 if sym == SAM else 1.0) + (rnd.gauss(0, 0.0003) if sym == SAM else 0)
                    WS_LIVE_PX[sym] *= math.exp(r)
                    px = round(WS_LIVE_PX[sym] / TICK[sym]) * TICK[sym]
                    ws.send(json.dumps({"header": {"action_code": "KA", "action_sym": sym},
                                        "body": {"sym": sym, "last_pric": str(px), "exec_qty": "3",
                                                 "exch_tm": datetime.now(KST).strftime("%H%M%S"),
                                                 "bid_best_pric": str(px - TICK[sym]), "ask_best_pric": str(px),
                                                 "cum_trd_qty": "1000"}}))
    except Exception:
        return
    finally:
        if entry in WS_CLIENTS:
            WS_CLIENTS.remove(entry)


def serve_ws(port: int):
    from websockets.sync.server import serve as ws_serve

    srv = ws_serve(_ws_handler, "127.0.0.1", port, ping_interval=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    serve(port)
    try:
        serve_ws(port + 1)
    except ImportError:
        print("websockets 가 없어 실시간 가짜 서버는 띄우지 않음")
    print(f"mock NH server running — REST {port} · WebSocket {port + 1} (NH_WS_BASE=http://127.0.0.1:{port + 1})")
    while True:
        time.sleep(3600)
