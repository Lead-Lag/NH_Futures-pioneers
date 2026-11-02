"""현물 개장 범위 돌파 — 실시간 실행 (해외 모의계좌).

    python run_orb.py                       # 두 종목(SFC·MHI) × 두 세션(10:30·14:00), 16:57 에 스스로 종료
    python run_orb.py --products MHI --sessions pm
    python run_orb.py --dry-run             # 주문 없이 시세로만 (체결은 호가로 가정, source=simulated)

규칙은 backtest_orb.py 와 같은 값(아래 RULE·PRODUCTS·SESSIONS)을 씁니다. 바꾸면 둘 다 바꿀 것.
  - 기준 범위: 세션 개장(10:30 / 14:00)부터 15분, 1분봉 고가·저가
  - 진입: 범위 뒤 마감된 1분봉 종가가 범위를 벗어난 첫 봉 (세션 시작 59분 봉까지), 종목·세션마다 1회
  - 손절: 진입 체결가에서 ATR(1분봉 14개) × 2, 틱 올림, 최소 2틱 / 익절: 손절폭 × 1.5
  - 시간 청산: 오전 = 현물 점심 5분 전(SFC 12:25, MHI 12:55), 오후 = 현물 마감 5분 전(SFC 15:55, MHI 16:55)
  - 범위 폭 < 8틱이면 그 세션은 거래 안 함
  - 주문: SFC 시장가, MHI(HKEX 시장가 불가) 체결될 만한 지정가. 응답 없으면 재전송하지 않음
  - 손절·익절은 현재가 조회(기본 5초)마다 판정 — 백테스트(봉 고가·저가)와 다른 점
기록: results/run_orb_<시각>/ events.jsonl · signals.csv · orders.csv · trades.csv · bars_<종목>.csv · api_calls.csv · run_meta.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from check_overseas import product_info, resolve_front, send_order, wait_fill
from nh_overseas import ApiFailure, NoResponse, OverseasClient, keys_from_env, num

RULE = dict(k=2.0, m=1.5, range_min=15, atr_n=14, min_stop_ticks=2, min_range_ticks=8)
PRODUCTS = {
    "SFC": dict(tick=1.0, tick_value=1.0, currency="USD", session_start="100000",
                exit={"am": "122500", "pm": "155500"}),
    "MHI": dict(tick=1.0, tick_value=10.0, currency="HKD", session_start="101500",
                exit={"am": "125500", "pm": "165500"}),
}
SESSIONS = {"am": dict(open_t="103000", last_entry="112900"),
            "pm": dict(open_t="140000", last_entry="145900")}
STALE_SEC = 90   # 마감된 지 이보다 오래된 봉으로는 주문하지 않음 (2주차 10/29 교훈)


# ── 시계 (시험용 가상 시계: --test-at HH:MM --speed N) ──────────
class Clock:
    def __init__(self, test_at: str | None, speed: float):
        self.t0, self.speed = time.time(), speed
        if test_at:
            h, m = map(int, test_at.split(":"))
            self.base = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
        else:
            self.base, self.speed = None, 1.0

    def now(self) -> datetime:
        if self.base is None:
            return datetime.now()
        return self.base + timedelta(seconds=(time.time() - self.t0) * self.speed)

    def sleep(self, sec: float) -> None:
        time.sleep(sec / self.speed)


def hms(dt: datetime) -> str:
    return dt.strftime("%H%M%S")


def add_time(t: str, minutes: int) -> str:
    tot = int(t[:2]) * 60 + int(t[2:4]) + minutes
    return f"{tot // 60:02d}{tot % 60:02d}00"


# ── 규칙 (순수 함수: 백테스트와 대조 시험용) ─────────────────────
def atr_at(bars: list[dict], i: int, n: int) -> float | None:
    """bars[i] 까지의 ATR(단순평균 TR, 그날 세션 봉만)."""
    if i + 1 < n:
        return None
    trs = []
    for j in range(i - n + 1, i + 1):
        b = bars[j]
        if j == 0:
            trs.append(b["high"] - b["low"])
        else:
            pc = bars[j - 1]["close"]
            trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return sum(trs) / n


def session_range(bars: list[dict], session: str) -> tuple[float, float, int] | None:
    ses = SESSIONS[session]
    r_end = add_time(ses["open_t"], RULE["range_min"])
    rng = [b for b in bars if ses["open_t"] <= b["t"] < r_end]
    if len(rng) < RULE["range_min"] - 1:
        return None
    return max(b["high"] for b in rng), min(b["low"] for b in rng), len(rng)


def check_signal(bars: list[dict], i: int, product: str, session: str) -> dict | None:
    """bars[i](마감된 봉)가 진입 신호인지. bars 는 그날 세션 시작부터 시간순."""
    prd, ses = PRODUCTS[product], SESSIONS[session]
    b = bars[i]
    r_end = add_time(ses["open_t"], RULE["range_min"])
    if not (r_end <= b["t"] <= ses["last_entry"]):
        return None
    rg = session_range(bars, session)
    if rg is None:
        return None
    hi, lo, _ = rg
    width = round((hi - lo) / prd["tick"])
    if width < RULE["min_range_ticks"]:
        return {"blocked": "range_narrow", "range_hi": hi, "range_lo": lo, "range_ticks": width}
    side = 1 if b["close"] > hi else (-1 if b["close"] < lo else 0)
    if side == 0:
        return None
    atr = atr_at(bars, i, RULE["atr_n"])
    if atr is None:
        return {"blocked": "atr_not_ready"}
    stop_ticks = max(RULE["min_stop_ticks"], math.ceil(RULE["k"] * atr / prd["tick"]))
    return {"side": side, "range_hi": hi, "range_lo": lo, "range_ticks": width,
            "atr": round(atr, 3), "stop_ticks": stop_ticks, "signal_t": b["t"], "close": b["close"]}


# ── 기록 ──────────────────────────────────────────────────────
class Journal:
    def __init__(self, out: Path, clock: Clock):
        self.out, self.clock = out, clock
        out.mkdir(parents=True, exist_ok=True)
        self.ev = open(out / "events.jsonl", "a", encoding="utf-8")
        self.files = {}
        self.cols = {
            "signals": ["ts", "product", "session", "signal_t", "side", "close", "range_hi", "range_lo",
                        "range_ticks", "atr", "stop_ticks", "action", "reason"],
            "orders": ["ts", "product", "session", "purpose", "side", "ord_no", "how", "status",
                       "fill_price", "fill_qty", "ref_price", "slip_ticks", "latency_ms", "source"],
            "trades": ["product", "session", "side", "entry_ts", "entry", "stop", "target", "exit_ts",
                       "exit", "reason", "ticks", "pnl", "currency", "pnl_krw", "hold_min"],
        }

    def event(self, kind: str, **kw) -> None:
        rec = {"ts": self.clock.now().isoformat(timespec="seconds"), "kind": kind, **kw}
        self.ev.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.ev.flush()
        print(f"{self.clock.now():%H:%M:%S}  [{kind}] " + " ".join(f"{k}={v}" for k, v in kw.items()), flush=True)

    def row(self, name: str, **kw) -> None:
        if name not in self.files:
            path = self.out / f"{name}.csv"
            new = not path.exists()
            f = open(path, "a", newline="", encoding="utf-8")
            w = csv.DictWriter(f, fieldnames=self.cols[name], extrasaction="ignore")
            if new:
                w.writeheader()
            self.files[name] = (f, w)
        f, w = self.files[name]
        w.writerow({"ts": self.clock.now().isoformat(timespec="seconds"), **kw})
        f.flush()


# ── 실행기 ─────────────────────────────────────────────────────
class Runner:
    def __init__(self, a, api: OverseasClient | None, clock: Clock, j: Journal):
        self.a, self.api, self.clock, self.j = a, api, clock, j
        self.info: dict[str, dict] = {}
        self.bars: dict[str, list[dict]] = {}
        self.seen_bar: dict[str, str] = {}
        self.state: dict[tuple[str, str], dict] = {}
        self.fx = {"USD": a.usdkrw, "HKD": a.hkdkrw}

    # 시세
    def refresh_bars(self, prd: str) -> list[dict]:
        info, now = self.info[prd], self.clock.now()
        today = now.strftime("%Y%m%d")
        body = self.api.chart_minute(info["exch_cd"], info["symbol"], today, today)
        rows = body.get("Occurs1") or []
        out = []
        for r in rows:
            d, t = str(r.get("occurs1_ref_dt")).strip(), str(r.get("occurs1_exch_tm")).strip().zfill(6)
            if d != today or t < PRODUCTS[prd]["session_start"]:
                continue
            start = datetime.strptime(d + t, "%Y%m%d%H%M%S")
            if start + timedelta(minutes=1) > now:   # 진행 중인 봉 제외
                continue
            out.append(dict(t=t, start=start, open=num(r.get("occurs1_open_pric")), high=num(r.get("occurs1_high_pric")),
                            low=num(r.get("occurs1_low_pric")), close=num(r.get("occurs1_close_pric")),
                            volume=num(r.get("occurs1_cum_trd_qty"))))
        out.sort(key=lambda b: b["t"])
        # 새로 마감된 봉 기록
        path = self.j.out / f"bars_{info['symbol']}.csv"
        new = not path.exists()
        last = self.seen_bar.get(prd, "")
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "t", "open", "high", "low", "close", "volume"])
            for b in out:
                if b["t"] > last:
                    w.writerow([today, b["t"], b["open"], b["high"], b["low"], b["close"], b["volume"]])
        if out:
            self.seen_bar[prd] = out[-1]["t"]
        self.bars[prd] = out
        return out

    def quote(self, prd: str) -> dict:
        info = self.info[prd]
        r = self.api.price(info["exch_cd"], info["symbol"])
        return dict(last=num(r.get("occurs1_last_pric")), bid=num(r.get("occurs1_bid_pric")),
                    ask=num(r.get("occurs1_ask_pric")))

    # 주문 (체결가 반환, 실패 None)
    def execute(self, prd: str, ses: str, side: int, purpose: str, ref: float) -> float | None:
        info, sname = self.info[prd], "buy" if side == 1 else "sell"
        t0 = time.time()
        if self.a.dry_run:
            q = self.quote(prd)
            px = (q["ask"] if side == 1 else q["bid"]) or q["last"]
            self._order_row(prd, ses, purpose, sname, "", "sim", "6", px, 1, ref, t0, "simulated")
            return px
        try:
            ord_no, how = send_order(self.api, info, sname)
        except NoResponse:
            self.j.event("order_no_response", product=prd, purpose=purpose,
                         note="재전송하지 않음 — 주문내역·잔고로 확인")
            self._order_row(prd, ses, purpose, sname, "", "", "NO_RESPONSE", "", "", ref, t0, "paper_account")
            return self._fill_from_position(prd, side)
        except ApiFailure as e:
            self.j.event("order_rejected", product=prd, purpose=purpose, code=e.code, msg=e.message)
            self._order_row(prd, ses, purpose, sname, "", "", f"REJECTED {e.code}", "", "", ref, t0, "paper_account")
            return None
        r = wait_fill(self.api, ord_no, timeout=15.0)
        sts = str(r.get("occurs1_ord_sts", "")).strip()
        px = num(r.get("occurs1_exec_pric"))
        self._order_row(prd, ses, purpose, sname, ord_no, how, sts, px or "", r.get("occurs1_exec_qty", ""),
                        ref, t0, "paper_account")
        if sts == "6" and px:
            return px
        rem = int(num(r.get("occurs1_ord_rem_qty")) or 1)
        try:
            self.api.cancel(info["symbol"], ord_no, rem, sname)
            self.j.event("cancel_unfilled", product=prd, ord_no=ord_no)
        except (ApiFailure, NoResponse) as e:
            self.j.event("cancel_failed", product=prd, ord_no=ord_no, err=str(e))
        return None

    def _fill_from_position(self, prd: str, side: int) -> float | None:
        time.sleep(2)
        try:
            pos = self.api.net_position(self.info[prd]["symbol"])
        except (ApiFailure, NoResponse):
            return None
        self.j.event("position_after_no_response", product=prd, position=pos)
        return None

    def _order_row(self, prd, ses, purpose, side, ord_no, how, sts, px, qty, ref, t0, source):
        slip = ""
        if px not in ("", None) and ref:
            sgn = 1 if side == "buy" else -1
            slip = round(sgn * (float(px) - ref) / PRODUCTS[prd]["tick"], 1)
        self.j.row("orders", product=prd, session=ses, purpose=purpose, side=side, ord_no=ord_no, how=how,
                   status=sts, fill_price=px, fill_qty=qty, ref_price=ref, slip_ticks=slip,
                   latency_ms=int((time.time() - t0) * 1000), source=source)

    def reconcile(self, prd: str, expected: int) -> None:
        if self.a.dry_run:
            return
        try:
            pos = self.api.net_position(self.info[prd]["symbol"])
        except (ApiFailure, NoResponse) as e:
            self.j.event("reconcile_failed", product=prd, err=str(e)); return
        if pos != expected:
            self.j.event("position_mismatch", product=prd, expected=expected, actual=pos)
        else:
            self.j.event("reconcile_ok", product=prd, position=pos)

    # 한 종목·세션 처리
    def step_bars(self, prd: str, ses: str) -> None:
        st = self.state[(prd, ses)]
        if st["phase"] != "wait":
            return
        bars, now = self.bars.get(prd, []), self.clock.now()
        for i, b in enumerate(bars):
            if b["t"] <= st["last_checked"]:
                continue
            st["last_checked"] = b["t"]
            sig = check_signal(bars, i, prd, ses)
            if sig is None:
                continue
            age = (now - (b["start"] + timedelta(minutes=1))).total_seconds()
            row = dict(product=prd, session=ses, signal_t=b["t"], close=b["close"], **{k: v for k, v in sig.items()
                       if k in ("range_hi", "range_lo", "range_ticks", "atr", "stop_ticks")})
            if "blocked" in sig:
                if sig["blocked"] == "range_narrow":
                    self.j.row("signals", **row, action="skip_session", reason="range_narrow")
                    self.j.event("session_skipped", product=prd, session=ses, range_ticks=sig.get("range_ticks"))
                    st["phase"] = "done"
                    return
                continue
            row["side"] = "long" if sig["side"] == 1 else "short"
            if age > STALE_SEC:
                self.j.row("signals", **row, action="ignored", reason=f"stale_bar_{int(age)}s")
                self.j.event("stale_signal_ignored", product=prd, session=ses, bar=b["t"], age_s=int(age))
                continue
            self.j.row("signals", **row, action="enter", reason="")
            self.enter(prd, ses, sig, b)
            return

    def enter(self, prd: str, ses: str, sig: dict, bar: dict) -> None:
        st, prdc = self.state[(prd, ses)], PRODUCTS[prd]
        side = sig["side"]
        px = self.execute(prd, ses, side, "entry", bar["close"])
        if px is None:
            self.j.event("entry_failed", product=prd, session=ses)
            st["phase"] = "done"
            self.reconcile(prd, 0)
            return
        stop = px - side * sig["stop_ticks"] * prdc["tick"]
        target = px + side * RULE["m"] * sig["stop_ticks"] * prdc["tick"]
        st.update(phase="in", side=side, entry=px, stop=stop, target=target, entry_ts=self.clock.now())
        self.j.event("entered", product=prd, session=ses, side=side, entry=px, stop=stop, target=target)
        self.reconcile(prd, side)

    def step_position(self, prd: str, ses: str) -> None:
        st = self.state[(prd, ses)]
        if st["phase"] != "in":
            return
        now = hms(self.clock.now())
        side = st["side"]
        reason = ""
        if now >= PRODUCTS[prd]["exit"][ses]:
            reason, ref = "time", None
        else:
            try:
                q = self.quote(prd)
            except (ApiFailure, NoResponse) as e:
                self.j.event("quote_failed", product=prd, err=str(e)); return
            last = q["last"]
            if last and ((side == 1 and last <= st["stop"]) or (side == -1 and last >= st["stop"])):
                reason, ref = "stop", st["stop"]
            elif last and ((side == 1 and last >= st["target"]) or (side == -1 and last <= st["target"])):
                reason, ref = "target", st["target"]
        if reason:
            self.exit(prd, ses, reason, ref)

    def exit(self, prd: str, ses: str, reason: str, ref: float | None) -> None:
        st, prdc = self.state[(prd, ses)], PRODUCTS[prd]
        side = st["side"]
        if ref is None:
            try:
                ref = self.quote(prd)["last"]
            except (ApiFailure, NoResponse):
                ref = st["entry"]
        px = None
        for attempt in range(5):   # 청산은 될 때까지 (지정가 미체결이면 다시)
            px = self.execute(prd, ses, -side, f"exit_{reason}", ref)
            if px is not None:
                break
            self.j.event("exit_retry", product=prd, attempt=attempt + 1)
            self.clock.sleep(2)
        if px is None:
            self.j.event("EXIT_FAILED", product=prd, session=ses, note="앱에서 직접 정리 필요")
            st["phase"] = "error"
            return
        ticks = round(side * (px - st["entry"]) / prdc["tick"])
        pnl = ticks * prdc["tick_value"]
        fx = self.fx.get(prdc["currency"]) or 0
        held = (self.clock.now() - st["entry_ts"]).total_seconds() / 60
        self.j.row("trades", product=prd, session=ses, side="long" if side == 1 else "short",
                   entry_ts=st["entry_ts"].isoformat(timespec="seconds"), entry=st["entry"], stop=st["stop"],
                   target=st["target"], exit_ts=self.clock.now().isoformat(timespec="seconds"), exit=px,
                   reason=reason, ticks=ticks, pnl=pnl, currency=prdc["currency"],
                   pnl_krw=round(pnl * fx) if fx else "", hold_min=round(held, 1))
        self.j.event("exited", product=prd, session=ses, reason=reason, exit=px, ticks=ticks,
                     pnl=f"{pnl} {prdc['currency']}")
        st["phase"] = "done"
        self.reconcile(prd, 0)

    def flatten_all(self, why: str) -> None:
        for (prd, ses), st in self.state.items():
            if st["phase"] == "in":
                self.j.event("forced_exit", product=prd, session=ses, why=why)
                self.exit(prd, ses, "forced", None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", default="SFC,MHI")
    ap.add_argument("--sessions", default="am,pm")
    ap.add_argument("--poll", type=float, default=5.0, help="현재가 조회 간격(초)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--usdkrw", type=float, default=0.0, help="원화 환산 환율(그날 시작 때 정한 값)")
    ap.add_argument("--hkdkrw", type=float, default=0.0)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--test-at", default=None, help="시험용 가상 시계 시작(HH:MM)")
    ap.add_argument("--speed", type=float, default=1.0, help="시험용 시계 배속")
    a = ap.parse_args()

    clock = Clock(a.test_at, a.speed)
    products = [p.strip() for p in a.products.split(",") if p.strip()]
    sessions = [s.strip() for s in a.sessions.split(",") if s.strip()]
    out = Path("results") / f"run_orb_{datetime.now():%Y%m%d_%H%M%S}"
    j = Journal(out, clock)
    api = OverseasClient("paper", *keys_from_env(), out, base_url=a.base_url)
    r = Runner(a, api, clock, j)

    meta = dict(run_id=out.name, started=clock.now().isoformat(), mode="dry-run" if a.dry_run else "paper",
                env="paper", rule=RULE, sessions={s: SESSIONS[s] for s in sessions}, products={},
                poll_sec=a.poll, fx={"USDKRW": a.usdkrw, "HKDKRW": a.hkdkrw}, python=os.sys.version)
    for prd in products:
        info = product_info(api, prd)
        exch_cd, sym, row = resolve_front(api, prd, info.get("exchange", ""))
        info.update(exch_cd=exch_cd, symbol=sym, symbol_name=row.get("occurs1_sym_nm", "").strip())
        r.info[prd] = info
        meta["products"][prd] = {**info, **PRODUCTS[prd]}
        pos = 0 if a.dry_run else api.net_position(sym)
        j.event("start", product=prd, symbol=sym, exch_cd=exch_cd, tick=info.get("tick_size"),
                tick_value=info.get("tick_value"), currency=info.get("currency"), position=pos)
        if pos != 0:
            raise SystemExit(f"{sym} 잔고가 {pos} 입니다. check_overseas.py --flatten 으로 정리한 뒤 시작하세요.")
        for ses in sessions:
            r.state[(prd, ses)] = dict(phase="wait", last_checked="")
    (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    end_t = max(PRODUCTS[p]["exit"][s] for p in products for s in sessions)
    end_t = add_time(end_t, 2)
    j.event("plan", sessions=sessions, products=products, ends_at=end_t)
    last_loop, errors = time.time(), 0
    try:
        while hms(clock.now()) < end_t:
            gap = (time.time() - last_loop) * clock.speed
            if gap > 60:
                j.event("pause", seconds=int(gap), note="실행이 멈췄다가 다시 돎")
            last_loop = time.time()
            now = clock.now()
            try:
                # 봉: 매분 2초 이후 한 번씩
                for prd in products:
                    key = now.strftime("%H%M")
                    if now.second >= 2 and r.seen_bar.get(prd + "_fetch") != key:
                        r.seen_bar[prd + "_fetch"] = key
                        r.refresh_bars(prd)
                        for ses in sessions:
                            r.step_bars(prd, ses)
                    for ses in sessions:
                        r.step_position(prd, ses)
                errors = 0
            except (ApiFailure, NoResponse) as e:
                errors += 1
                j.event("api_error", err=str(e)[:200], streak=errors)
                if errors >= 20:
                    j.event("too_many_errors"); break
                clock.sleep(min(30, 2 * errors))
                continue
            clock.sleep(a.poll)
    except KeyboardInterrupt:
        j.event("stopped_by_user")
    finally:
        r.flatten_all("end")
        for prd in products:
            r.reconcile(prd, 0)
        meta["finished"] = clock.now().isoformat()
        (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        j.event("end", out=str(out))


if __name__ == "__main__":
    main()
