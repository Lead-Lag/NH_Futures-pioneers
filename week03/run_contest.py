"""11/13 해외 모의계좌 수익률 대회용 실행 (15:00~18:00) — 3주차 「범위 돌파」를 대회 시간대에 맞춘 변형.

    python -m pip install websockets
    python run_contest.py --hkdkrw 170.9                 # 대회: MHI, 15:30 범위, 18:00 까지 켜져 있음
    python run_contest.py --dry-run --test-at 15:25 --speed 10   # 시험: 주문 없이 오늘 분봉으로 빨리 감기

run_orb.py(3주차 실행 코드)를 그대로 불러 쓰고, 대회에 필요한 것만 바꿉니다.
  - 세션: 기준 범위 15:30~15:45 (KST), 진입은 16:29 봉까지 첫 돌파 1번, 시간 청산 16:55 (홍콩 현물 마감 17:00 전)
          (대회 시간대에 같은 규칙을 시각별로 백테스트해 15:30 이 가장 나았음 — 9건뿐이라 근거는 약함)
  - 계약 수: 거래당 위험 = 원금의 --risk-pct(기본 0.2%) ÷ (손절폭 × 틱가치), 최소 1·최대 --max-qty
             주문이 거부되면 계약 수를 반으로 줄여 다시 시도
  - 손절·익절 판정: 웹소켓 실시간 체결(FA) 가격 우선, 3초 넘게 안 오면 REST 현재가로 대신 (11/5 운영진 안내)
  - 멈추지 않기: API 오류가 이어져도 종료하지 않고 최대 30초 간격으로 계속 재시도, 청산은 잔고가 0 이 될 때까지
  - 하루 손실 한도: --max-loss-pct(기본 0.6%) — 넘으면 신규 진입 중지(이 전략은 하루 1번이라 안전장치)
  - 거래가 끝나도 --until(기본 18:00)까지 켜 두고 10분마다 상태를 찍음
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as urlquote

import run_orb as R
from check_overseas import LIMIT_ONLY_EXCH, LIMIT_SLIP_TICKS, decimals_of, product_info, resolve_front, wait_fill
from nh_overseas import ApiFailure, NoResponse, OverseasClient, keys_from_env, num

R.SESSIONS["late"] = dict(open_t="153000", last_entry="162900")
R.PRODUCTS["MHI"]["exit"]["late"] = "165500"
R.PRODUCTS["SFC"]["exit"]["late"] = "155500"
USD_PER = {"USD": 1.0, "HKD": 1 / 7.8}   # 홍콩달러는 미국달러에 7.75~7.85 로 묶여 있음


# ── 웹소켓 실시간 체결 (기록 + 손절 판정용 최신가) ───────────────
class WsFeed:
    def __init__(self, api: OverseasClient, syms: list[str], j: R.Journal, out: Path):
        self.api, self.syms, self.j = api, syms, j
        self.last: dict[str, tuple[float, float]] = {}   # sym -> (가격, 받은 시각 epoch)
        self.ticks = open(out / "ws_ticks.jsonl", "a", encoding="utf-8")
        self.stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def price(self, sym: str, max_age: float = 3.0) -> float | None:
        v = self.last.get(sym)
        return v[0] if v and time.time() - v[1] <= max_age else None

    def _run(self) -> None:
        wait = 1
        while not self.stop:
            try:
                asyncio.run(self._session())
                wait = 1
            except Exception as e:   # 끊기면 새 접속키로 다시
                self.j.event("ws_disconnected", err=f"{type(e).__name__}: {str(e)[:120]}", retry_in=wait)
                time.sleep(wait)
                wait = min(30, wait * 2)

    async def _session(self) -> None:
        import websockets
        self.api._ensure_token()
        r = self.api.s.get(self.api.base + "/permission/v1/web-socket-key", timeout=10,
                           headers={"Authorization": f"Bearer {self.api.token}"})
        key = str((r.json() or {}).get("access_key") or "").strip()
        if not key:
            raise RuntimeError(f"웹소켓 접속키 발급 실패 HTTP {r.status_code}")
        url = self.api.base.replace("https://", "wss://") + "/trade/ws-stream?access_key=" + urlquote(key, safe="")
        async with websockets.connect(url, ping_interval=None, open_timeout=15, close_timeout=5) as ws:
            await ws.send(json.dumps({"header": {"api_ver": "1", "action": "0"},
                                      "body": {"action_code": "session_init", "session_token": self.api.token}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if str(msg.get("code") or "").lower() == "pingpong":
                    await ws.send(json.dumps(msg)); continue
                if msg.get("code") == "INIT" and msg.get("msg") == "INIT_SUCCESS":
                    break
                raise ConnectionError(f"초기화 거절: {str(msg)[:150]}")
            for s in self.syms:
                await ws.send(json.dumps({"header": {"api_ver": "1", "action": "A"},
                                          "body": {"action_code": "FA", "action_sym": s}}))
            self.j.event("ws_connected", syms=self.syms)
            while not self.stop:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
                code = msg.get("code")
                if str(code or "").lower() == "pingpong":
                    await ws.send(json.dumps(msg)); continue
                if code is not None:
                    if code != "RT_INIT":
                        self.j.event("ws_control", code=code, msg=str(msg.get("msg") or "")[:100])
                    continue
                sym = str((msg.get("header") or {}).get("action_sym") or "")
                body = msg.get("body") or {}
                px = num(body.get("last_pric"))
                if px:
                    self.last[sym] = (px, time.time())
                    self.ticks.write(json.dumps({"recv": datetime.now().isoformat(timespec="milliseconds"),
                                                 "sym": sym, "px": px, "exec_tm": body.get("ko_exec_tm")}) + "\n")


# ── 대회용 실행기 ─────────────────────────────────────────────
class ContestRunner(R.Runner):
    def __init__(self, a, api, clock, j):
        super().__init__(a, api, clock, j)
        self.ws: WsFeed | None = None
        self.rest_cache: dict[str, tuple[dict, float]] = {}
        self.realized_usd = 0.0
        self.halted = ""

    # 최신가: 웹소켓 → (3초 넘게 없으면) REST, REST 는 3초에 한 번까지만
    def quote(self, prd: str) -> dict:
        sym = self.info[prd]["symbol"]
        if self.ws and (px := self.ws.price(sym)) is not None:
            return dict(last=px, bid=None, ask=None, src="ws")
        c = self.rest_cache.get(prd)
        if c and time.time() - c[1] < 3:
            return c[0]
        q = super().quote(prd)
        q["src"] = "rest"
        self.rest_cache[prd] = (q, time.time())
        return q

    def size_for(self, prd: str, stop_ticks: int) -> int:
        p = R.PRODUCTS[prd]
        risk_usd_per = stop_ticks * p["tick_value"] * USD_PER[p["currency"]]
        q = math.floor(self.a.capital * self.a.risk_pct / 100 / max(risk_usd_per, 1e-9))
        return max(1, min(self.a.max_qty, q))

    def _send(self, prd: str, side: int, qty: int) -> tuple[str, str]:
        info, sname = self.info[prd], "buy" if side == 1 else "sell"
        if info.get("exchange", "") not in LIMIT_ONLY_EXCH:
            try:
                return str(self.api.order_market(info["symbol"], sname, qty).get("ord_no") or "").strip(), "market"
            except ApiFailure as e:
                if e.code != "7032":
                    raise
        tick = info.get("tick_size") or 1.0
        row = self.api.price(info["exch_cd"], info["symbol"])
        ref = num(row.get("occurs1_ask_pric")) if side == 1 else num(row.get("occurs1_bid_pric"))
        ref = ref or num(row.get("occurs1_last_pric"))
        px = ref + side * LIMIT_SLIP_TICKS * tick
        px_s = f"{px:.{decimals_of(tick)}f}"
        return str(self.api.order_limit(info["symbol"], sname, px_s, qty).get("ord_no") or "").strip(), f"limit {px_s}"

    def execute_q(self, prd: str, ses: str, side: int, qty: int, purpose: str, ref: float) -> tuple[float | None, int]:
        """(평균 체결가, 체결 수량). 일부만 체결되면 남은 수량은 취소하고 체결분만 돌려줌. 재전송 금지."""
        sname, t0 = ("buy" if side == 1 else "sell"), time.time()
        if self.a.dry_run:
            q = self.quote(prd)
            px = (q.get("ask") if side == 1 else q.get("bid")) or q["last"]
            self._order_row(prd, ses, purpose, sname, "", "sim", "6", px, qty, ref, t0, "simulated")
            return px, qty
        try:
            ord_no, how = self._send(prd, side, qty)   # ApiFailure(거부)는 부른 쪽에서 처리
        except NoResponse:
            self.j.event("order_no_response", product=prd, purpose=purpose, note="재전송하지 않음 — 잔고로 확인")
            self._order_row(prd, ses, purpose, sname, "", "", "NO_RESPONSE", "", "", ref, t0, "paper_account")
            return None, -1
        try:
            r = wait_fill(self.api, ord_no, timeout=15.0)
        except (ApiFailure, NoResponse) as e:   # 접수는 됐는데 체결 확인이 안 됨 → 잔고로 판단(재전송 금지)
            self.j.event("fill_check_failed", product=prd, ord_no=ord_no, err=str(e)[:120])
            return None, -1
        filled = int(num(r.get("occurs1_exec_qty")))
        rem = int(num(r.get("occurs1_ord_rem_qty")))
        if rem > 0:
            try:
                self.api.cancel(self.info[prd]["symbol"], ord_no, rem, sname)
                self.j.event("cancel_unfilled", product=prd, ord_no=ord_no, rem=rem)
                r = wait_fill(self.api, ord_no, timeout=3.0) or r   # 취소 직전 체결분까지 다시 읽기
                filled = int(num(r.get("occurs1_exec_qty")))
            except (ApiFailure, NoResponse) as e:
                self.j.event("cancel_failed", product=prd, ord_no=ord_no, err=str(e)[:120])
                return None, -1
        px = num(r.get("occurs1_exec_pric")) or None
        self._order_row(prd, ses, purpose, sname, ord_no, how, str(r.get("occurs1_ord_sts", "")).strip(),
                        px or "", filled, ref, t0, "paper_account")
        return (px if filled > 0 else None), filled

    def position(self, prd: str) -> int | None:
        if self.a.dry_run:
            st = self.state.get((prd, "late"), {})
            return st.get("side", 0) * st.get("qty", 0) if st.get("phase") == "in" else 0
        try:
            return self.api.net_position(self.info[prd]["symbol"])
        except (ApiFailure, NoResponse) as e:
            self.j.event("position_check_failed", product=prd, err=str(e)[:120]); return None

    def enter(self, prd: str, ses: str, sig: dict, bar: dict) -> None:
        st, p = self.state[(prd, ses)], R.PRODUCTS[prd]
        if self.halted:
            self.j.event("entry_blocked", product=prd, why=self.halted); st["phase"] = "done"; return
        side, qty = sig["side"], self.size_for(prd, sig["stop_ticks"])
        px = filled = None
        while qty >= 1:
            try:
                px, filled = self.execute_q(prd, ses, side, qty, "entry", bar["close"])
                break
            except ApiFailure as e:   # 증거금 부족 등 → 반으로 줄여 다시
                self.j.event("order_rejected", product=prd, qty=qty, code=e.code, msg=e.message)
                qty //= 2
        if not filled or filled < 0:
            pos = self.position(prd)
            if pos:   # 확인이 안 됐지만 체결된 경우 — 잔고 기준으로 보유 처리
                filled, px = abs(pos), px or bar["close"]
            else:
                self.j.event("entry_failed", product=prd, session=ses); st["phase"] = "done"; return
        stop = px - side * sig["stop_ticks"] * p["tick"]
        target = px + side * R.RULE["m"] * sig["stop_ticks"] * p["tick"]
        st.update(phase="in", side=side, qty=filled, entry=px, stop=stop, target=target, entry_ts=self.clock.now())
        risk_usd = sig["stop_ticks"] * p["tick_value"] * USD_PER[p["currency"]] * filled
        self.j.event("entered", product=prd, session=ses, side=side, qty=filled, entry=px, stop=stop, target=target,
                     risk_usd=round(risk_usd))
        self.reconcile(prd, side * filled)

    def step_position(self, prd: str, ses: str) -> None:
        st = self.state[(prd, ses)]
        if st["phase"] not in ("in", "exiting"):
            return
        if st["phase"] == "exiting":
            self.exit(prd, ses, st.get("exit_reason", "retry"), None); return
        reason, ref = "", None
        if R.hms(self.clock.now()) >= R.PRODUCTS[prd]["exit"][ses]:
            reason = "time"
        else:
            try:
                q = self.quote(prd)
            except (ApiFailure, NoResponse) as e:
                self.j.event("quote_failed", product=prd, err=str(e)[:120]); return
            last, side = q["last"], st["side"]
            if last and ((side == 1 and last <= st["stop"]) or (side == -1 and last >= st["stop"])):
                reason, ref = f"stop({q.get('src')})", st["stop"]
            elif last and ((side == 1 and last >= st["target"]) or (side == -1 and last <= st["target"])):
                reason, ref = f"target({q.get('src')})", st["target"]
        if reason:
            self.exit(prd, ses, reason, ref)

    def exit(self, prd: str, ses: str, reason: str, ref: float | None) -> None:
        """잔고가 0 이 될 때까지 청산. 한 번에 안 되면 'exiting' 으로 남겨 다음 반복에서 계속."""
        st, p = self.state[(prd, ses)], R.PRODUCTS[prd]
        side = st["side"]
        if ref is None:
            try:
                ref = self.quote(prd)["last"]
            except (ApiFailure, NoResponse):
                ref = st["entry"]
        for attempt in range(5):
            pos = self.position(prd)
            if pos is None:
                self.clock.sleep(2); continue
            remaining = abs(pos) if not self.a.dry_run else st["qty"]
            if remaining == 0:
                break
            try:
                px, filled = self.execute_q(prd, ses, -side, remaining, f"exit_{reason}", ref)
            except ApiFailure as e:
                self.j.event("exit_rejected", product=prd, code=e.code, msg=e.message); px, filled = None, 0
            if filled and filled > 0:
                st.setdefault("exit_fills", []).append((px, filled))
                if self.a.dry_run:
                    break
            self.clock.sleep(1)
        pos = 0 if self.a.dry_run else self.position(prd)
        if pos is None or pos != 0:   # 아직 남았거나 잔고를 못 읽음 → 멈추지 않고 다음 반복에서 다시
            st.update(phase="exiting", exit_reason=reason)
            self.j.event("exit_pending", product=prd, remaining=pos, note="다음 반복에서 다시 청산")
            return
        fills = st.get("exit_fills") or [(ref, st["qty"])]
        qty = sum(f for _, f in fills)
        avg = sum(px * f for px, f in fills) / max(qty, 1)
        ticks = round(side * (avg - st["entry"]) / p["tick"], 1)
        pnl = ticks * p["tick_value"] * st["qty"]
        self.realized_usd += pnl * USD_PER[p["currency"]]
        fx = self.fx.get(p["currency"]) or 0
        self.j.row("trades", product=prd, session=ses, side="long" if side == 1 else "short",
                   entry_ts=st["entry_ts"].isoformat(timespec="seconds"), entry=st["entry"], stop=st["stop"],
                   target=st["target"], exit_ts=self.clock.now().isoformat(timespec="seconds"), exit=round(avg, 2),
                   reason=reason, ticks=ticks, pnl=pnl, currency=p["currency"],
                   pnl_krw=round(pnl * fx) if fx else "",
                   hold_min=round((self.clock.now() - st["entry_ts"]).total_seconds() / 60, 1))
        self.j.event("exited", product=prd, session=ses, reason=reason, qty=st["qty"], exit=round(avg, 2),
                     ticks=ticks, pnl=f"{pnl:.0f} {p['currency']}", realized_usd=round(self.realized_usd))
        st["phase"] = "done"
        if -self.realized_usd >= self.a.capital * self.a.max_loss_pct / 100:
            self.halted = f"하루 손실 {self.realized_usd:.0f} USD ≥ 한도 {self.a.max_loss_pct}%"
            self.j.event("halted", why=self.halted)
        self.reconcile(prd, 0)

    def flatten_all(self, why: str) -> None:
        for (prd, ses), st in self.state.items():
            if st["phase"] in ("in", "exiting"):
                self.j.event("forced_exit", product=prd, session=ses, why=why)
                self.exit(prd, ses, "forced", None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", default="MHI")
    ap.add_argument("--sessions", default="late")
    ap.add_argument("--poll", type=float, default=1.0, help="반복 간격(초). 손절 판정은 웹소켓 가격, 없으면 REST(3초에 1번까지)")
    ap.add_argument("--capital", type=float, default=100_000, help="원금(USD)")
    ap.add_argument("--risk-pct", type=float, default=0.2, help="거래당 위험(원금 %%)")
    ap.add_argument("--max-qty", type=int, default=10)
    ap.add_argument("--max-loss-pct", type=float, default=0.6, help="하루 손실 한도(원금 %%) — 넘으면 신규 진입 중지")
    ap.add_argument("--until", default="1800", help="HHMM 까지 켜 둠")
    ap.add_argument("--no-ws", action="store_true", help="웹소켓 없이 REST 로만")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--usdkrw", type=float, default=0.0)
    ap.add_argument("--hkdkrw", type=float, default=0.0)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--test-at", default=None)
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args()

    clock = R.Clock(a.test_at, a.speed)
    products = [p.strip() for p in a.products.split(",") if p.strip()]
    sessions = [s.strip() for s in a.sessions.split(",") if s.strip()]
    out = Path("results") / f"contest_{datetime.now():%Y%m%d_%H%M%S}"
    j = R.Journal(out, clock)
    api = OverseasClient("paper", *keys_from_env(), out, base_url=a.base_url)
    r = ContestRunner(a, api, clock, j)

    meta = dict(run_id=out.name, started=clock.now().isoformat(), mode="dry-run" if a.dry_run else "paper",
                rule=R.RULE, sessions={s: R.SESSIONS[s] for s in sessions}, products={}, args=vars(a))
    for prd in products:
        info = product_info(api, prd)
        exch_cd, sym, row = resolve_front(api, prd, info.get("exchange", ""))
        info.update(exch_cd=exch_cd, symbol=sym, symbol_name=row.get("occurs1_sym_nm", "").strip())
        r.info[prd] = info
        meta["products"][prd] = {**info, **R.PRODUCTS[prd]}
        pos = 0 if a.dry_run else api.net_position(sym)
        j.event("start", product=prd, symbol=sym, exch_cd=exch_cd, position=pos,
                risk_pct=a.risk_pct, max_qty=a.max_qty, max_loss_pct=a.max_loss_pct)
        if pos != 0:
            raise SystemExit(f"{sym} 잔고가 {pos} 입니다. check_overseas.py --flatten 으로 정리한 뒤 시작하세요.")
        for ses in sessions:
            r.state[(prd, ses)] = dict(phase="wait", last_checked="")
    (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not a.no_ws and not a.test_at:
        r.ws = WsFeed(api, [r.info[p]["symbol"] for p in products], j, out)

    trade_end = R.add_time(max(R.PRODUCTS[p]["exit"][s] for p in products for s in sessions), 2)
    end_t = max(trade_end, a.until.ljust(6, "0"))
    j.event("plan", sessions=sessions, products=products, trade_until=trade_end, keep_alive_until=end_t)
    last_loop, errors, last_beat = time.time(), 0, ""
    try:
        while R.hms(clock.now()) < end_t:
            gap = time.time() - last_loop
            if gap > 60:
                j.event("pause", seconds=int(gap), note="실행이 멈췄다가 다시 돎")
            last_loop = time.time()
            now = clock.now()
            try:
                if R.hms(now) < trade_end:
                    for prd in products:
                        key = now.strftime("%H%M")
                        if now.second >= 2 and r.seen_bar.get(prd + "_fetch") != key:
                            r.seen_bar[prd + "_fetch"] = key
                            r.refresh_bars(prd)
                            for ses in sessions:
                                r.step_bars(prd, ses)
                for prd in products:
                    for ses in sessions:
                        r.step_position(prd, ses)
                beat = now.strftime("%H%M")
                if beat[-1] == "0" and beat != last_beat:   # 10분마다 상태
                    last_beat = beat
                    phases = {f"{p}_{s}": st["phase"] for (p, s), st in r.state.items()}
                    ws_age = {s: round(time.time() - v[1], 1) for s, v in (r.ws.last.items() if r.ws else [])}
                    j.event("heartbeat", phases=phases, realized_usd=round(r.realized_usd), ws_age_s=ws_age)
                errors = 0
            except (ApiFailure, NoResponse, OSError) as e:   # 멈추지 않음: 간격만 늘려 계속
                errors += 1
                j.event("api_error", err=str(e)[:200], streak=errors)
                clock.sleep(min(30, 2 * errors))
                continue
            clock.sleep(a.poll)
    except KeyboardInterrupt:
        j.event("stopped_by_user")
    finally:
        r.flatten_all("end")
        if r.ws:
            r.ws.stop = True
        for prd in products:
            r.reconcile(prd, 0)
        meta["finished"] = clock.now().isoformat()
        (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        j.event("end", out=str(out), realized_usd=round(r.realized_usd))


if __name__ == "__main__":
    main()
