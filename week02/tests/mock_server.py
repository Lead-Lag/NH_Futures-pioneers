"""가짜 NH선물 API 서버 — 키 없이 run_live.py 전체 흐름을 점검하기 위한 것.

응답 모양은 공개 저장소의 실제 응답 기록(tests/fixtures/nh)을 본떴습니다. 가격은 지어낸 값입니다.
  python tests/mock_server.py --port 8765 [--reject-first]   # --reject-first: 첫 조회를 401 로 거절(토큰 재발급 경로 시험)
  NH_BASE_URL=http://127.0.0.1:8765 NH_APP_KEY=x NH_APP_SECRET=y python run_live.py --mode paper --smoke-order --run-minutes 4
"""
from __future__ import annotations

import argparse
import json
import math
import random
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KST = timezone(timedelta(hours=9))
LOCK = threading.Lock()
STATE = {"position": 0, "avg": 0.0, "orders": [], "n": 7745230000, "rejected_once": False}
OPEN = 1385.0


def price_at(t: datetime) -> float:
    """빠르게 출렁이는 가격(주기 7분 사인파 + 잡음) — 짧은 시험에서도 밴드 신호가 나도록."""
    s = t.timestamp()
    rnd = random.Random(int(s))
    return round(round((OPEN + 1.2 * math.sin(s / 67.0) + rnd.gauss(0, 0.15)) / 0.1) * 0.1, 1)


def bars_today(now: datetime) -> list[dict]:
    start = now.replace(hour=8, minute=45, second=0, microsecond=0)
    if now < start:
        start = now.replace(second=0, microsecond=0) - timedelta(minutes=60)
    rows, t = [], start
    while t <= now:
        pts = [price_at(t + timedelta(seconds=k)) for k in range(0, 60, 5) if t + timedelta(seconds=k) <= now]
        rows.append({
            "occurs1_close_pric": f"{pts[-1]:.2f}", "occurs1_cum_trd_amt": "100", "occurs1_cum_trd_qty": "10",
            "occurs1_diff_rt": "0.00", "occurs1_exch_tm": t.strftime("%H%M%S"), "occurs1_high_pric": f"{max(pts):.2f}",
            "occurs1_low_pric": f"{min(pts):.2f}", "occurs1_open_pric": f"{pts[0]:.2f}", "occurs1_opint": "1189231",
            "occurs1_ref_dt": t.strftime("%Y%m%d"), "occurs1_rt": "",
        })
        t += timedelta(minutes=1)
    return list(reversed(rows))  # 최신 봉이 먼저


class H(BaseHTTPRequestHandler):
    reject_first = False

    def log_message(self, *a):
        pass

    def _send(self, code: int, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        path = self.path
        now = datetime.now(KST)
        if path == "/auth-service/v1/token":
            return self._send(200, {"access_token": "mock-token-" + now.strftime("%H%M%S%f"), "token_type": "Bearer",
                                    "expires_in": "86400"})
        if not self.headers.get("Authorization", "").startswith("Bearer mock-token"):
            return self._send(401, {"errorCode": "EGW001", "errorMessage": "unauthorized"})
        with LOCK:
            if H.reject_first and not STATE["rejected_once"]:
                STATE["rejected_once"] = True
                return self._send(401, {"errorCode": "EGW052", "errorMessage": "token expired (mock)"})
        body = json.loads(raw or "{}")
        last = price_at(now)
        if path.endswith("/domestic/price"):
            return self._send(200, {"sym_nm": "미국달러 F 202611", "last_pric": f"{last:.2f}", "ask_pric": f"{last + 0.1:.2f}",
                                    "bid_pric": f"{last:.2f}", "lst_trd_dt": "20261116", "rem_dy": "17", "mltplr": "10000",
                                    "sym_exch": body.get("sym")})
        if path.endswith("/domestic/product-detail"):
            return self._send(200, {"Occurs1": [{
                "occurs1_cd_val_inf_1": "0.1", "occurs1_cd_val_inf_3": "15:35~15:45", "occurs1_cd_val_inf_4": "08:45~15:45",
                "occurs1_ctr_init_mrgn": "649924.8", "occurs1_dtl_cd_ko_nm": "미국달러선물(USD)", "occurs1_exp_dt": "20261116",
                "occurs1_tick_val": "0000000001000.00000000"}]})
        if path.endswith("/domestic/chart-minute"):
            rows = bars_today(now)
            return self._send(200, {"Occurs1": rows[:300], "nrec": str(min(300, len(rows))), "nxt_key": "", "sym": body.get("sym")})
        if path.endswith("/domestic/order"):
            if body.get("ord_tp_cd") != "2" or body.get("ord_pric") != "0":
                return self._send(200, {"msg_cd": "40450000", "msg_cntn": "mock: 시장가만 지원", "ord_no": ""})
            with LOCK:
                STATE["n"] += 1
                no = str(STATE["n"])
                side = 1 if body["trd_div"] == "1" else -1
                qty = int(body["ord_qty"])
                px = last + 0.1 if side > 0 else last
                STATE["position"] += side * qty
                STATE["orders"].append({
                    "occurs1_ord_no": no, "occurs1_org_ord_no": no, "occurs1_sym": body["sym"], "occurs1_trd_div": body["trd_div"],
                    "occurs1_ord_qty": f"{qty:020.9f}", "occurs1_exec_qty": f"{qty:020.9f}", "occurs1_exec_pric": f"{px:024.9f}",
                    "occurs1_avg_exec_pric": "00000000000000.000000000", "occurs1_ord_sts_nm": "체결", "occurs1_ord_rejt_rsn": "",
                    "occurs1_exch_rcv_tm": now.strftime("%H%M%S%f")[:9], "occurs1_sym_nm": "미국달러 F 202611",
                })
            return self._send(200, {"msg_cd": "", "msg_cntn": "", "ord_no": no})
        if path.endswith("/domestic/order-history"):
            with LOCK:
                return self._send(200, {"Occurs1": list(STATE["orders"]), "nrec": str(len(STATE["orders"])), "nxt_key": ""})
        if path.endswith("/domestic/open-interest"):
            with LOCK:
                p = STATE["position"]
            rows = [] if p == 0 else [{"occurs1_sym": "A756B000", "occurs1_trd_div": "1" if p > 0 else "2",
                                       "occurs1_opint_qty": f"{abs(p):021.9f}"}]
            return self._send(200, {"Occurs1": rows, "nrec": str(len(rows)), "nxt_key": ""})
        if path.endswith("/domestic/cancel"):
            return self._send(200, {"msg_cd": "", "msg_cntn": "", "ord_no": "0"})
        return self._send(404, {"errorCode": "EGW404", "errorMessage": path})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--reject-first", action="store_true")
    a = ap.parse_args()
    H.reject_first = a.reject_first
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
