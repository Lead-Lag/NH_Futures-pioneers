"""해외 웹소켓 실시간 체결(FA) 기록 전용 — 주문 없음.

11/5 운영진 안내(「처음에만 REST, 이후는 웹소켓 실시간」)에 따라, 매매(run_orb.py, REST 조회)는
그대로 두고 옆 창에서 실시간 체결을 기록해 REST 5초 조회와 비교한다.

    python -m pip install websockets
    python ws_probe.py                         # SFCX26, MHIX26 · 16:57 까지
    python ws_probe.py --syms MHIX26 --until 1700

절차 (참고 저장소 backend/realtime/link.py 와 같은 순서):
  접속키 발급 GET /permission/v1/web-socket-key → wss://<서버>/trade/ws-stream?access_key=…
  → 세션 초기화(session_init) → FA 구독 → 서버의 pingpong 을 받은 그대로 되돌려 세션 유지
  접속키는 연결마다 새로 받는다(1회용). 키·토큰은 기록하지 않는다.
기록: results/ws_<시각>/ticks.jsonl (받은 시각·종목·본문 원본) · events.jsonl (연결·구독·끊김)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import websockets

from nh_overseas import OverseasClient, keys_from_env

KEY_PATH = "/permission/v1/web-socket-key"
STREAM_PATH = "/trade/ws-stream"


def now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class Probe:
    def __init__(self, syms: list[str], until: str, out: Path):
        self.syms, self.until, self.out = syms, until, out
        out.mkdir(parents=True, exist_ok=True)
        self.api = OverseasClient("paper", *keys_from_env(), out)
        self.ticks = open(out / "ticks.jsonl", "a", encoding="utf-8")
        self.events = open(out / "events.jsonl", "a", encoding="utf-8")
        self.count = {s: 0 for s in syms}
        self.shown = 0

    def event(self, kind: str, **kw) -> None:
        rec = {"ts": now(), "kind": kind, **kw}
        self.events.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.events.flush()
        print(f"{datetime.now():%H:%M:%S}  [{kind}] " + " ".join(f"{k}={v}" for k, v in kw.items()), flush=True)

    def done(self) -> bool:
        return datetime.now().strftime("%H%M") >= self.until

    def ws_key(self) -> str:
        self.api._ensure_token()
        r = self.api.s.get(self.api.base + KEY_PATH, timeout=10,
                           headers={"Authorization": f"Bearer {self.api.token}"})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        key = str((body or {}).get("access_key") or "").strip()
        if r.status_code >= 400 or not key:
            raise RuntimeError(f"접속키 발급 실패 HTTP {r.status_code}: {str(body)[:200]}")
        return key

    async def session(self) -> None:
        key = self.ws_key()
        url = self.api.base.replace("https://", "wss://") + STREAM_PATH + "?access_key=" + quote(key, safe="")
        async with websockets.connect(url, ping_interval=None, open_timeout=15, close_timeout=5) as ws:
            # 세션 초기화
            await ws.send(json.dumps({"header": {"api_ver": "1", "action": "0"},
                                      "body": {"action_code": "session_init", "session_token": self.api.token}}))
            t_end = time.time() + 15
            while True:
                if time.time() > t_end:
                    raise TimeoutError("세션 초기화 응답 없음")
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if str(msg.get("code") or "").lower() == "pingpong":
                    await ws.send(json.dumps(msg)); continue
                if msg.get("code") == "INIT" and msg.get("msg") == "INIT_SUCCESS":
                    break
                raise ConnectionError(f"초기화 거절: {str(msg)[:200]}")
            self.event("connected")
            for s in self.syms:
                await ws.send(json.dumps({"header": {"api_ver": "1", "action": "A"},
                                          "body": {"action_code": "FA", "action_sym": s}}))
            last_report = time.time()
            while not self.done():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=90)
                except asyncio.TimeoutError:
                    raise TimeoutError("90초 동안 서버 신호 없음")
                recv = now()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                code = msg.get("code")
                if str(code or "").lower() == "pingpong":
                    await ws.send(json.dumps(msg)); continue
                if code is not None:   # RT_INIT(구독 확인) · RT_FAIL(거절) 등
                    self.event("control", code=code, msg=str(msg.get("msg") or "")[:120],
                               data=str(msg.get("data") or "")[:200])
                    continue
                header, body = msg.get("header") or {}, msg.get("body")
                sym = str(header.get("action_sym") or "")
                self.ticks.write(json.dumps({"recv": recv, "sym": sym, "code": header.get("action_code"),
                                             "body": body}, ensure_ascii=False) + "\n")
                self.ticks.flush()
                self.count[sym] = self.count.get(sym, 0) + 1
                if self.shown < 3:   # 처음 몇 건은 칸 이름 확인용으로 화면에
                    self.shown += 1
                    print(f"{datetime.now():%H:%M:%S}  [sample] {sym} {json.dumps(body, ensure_ascii=False)[:300]}", flush=True)
                if time.time() - last_report > 60:
                    last_report = time.time()
                    self.event("count", **self.count)

    async def run(self) -> None:
        self.event("start", syms=self.syms, until=self.until)
        wait = 1
        while not self.done():
            try:
                await self.session()
            except Exception as e:   # 끊기면 새 접속키로 다시 붙는다
                self.event("disconnected", err=f"{type(e).__name__}: {str(e)[:150]}", retry_in=wait)
                await asyncio.sleep(wait)
                wait = min(60, wait * 2)
                continue
            wait = 1
        self.event("end", **self.count)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--syms", default="SFCX26,MHIX26")
    ap.add_argument("--until", default="1657", help="HHMM 에 종료")
    a = ap.parse_args()
    out = Path("results") / f"ws_{datetime.now():%Y%m%d_%H%M%S}"
    p = Probe([s.strip() for s in a.syms.split(",") if s.strip()], a.until, out)
    try:
        asyncio.run(p.run())
    except KeyboardInterrupt:
        p.event("stopped_by_user", **p.count)


if __name__ == "__main__":
    main()
