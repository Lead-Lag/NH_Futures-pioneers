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
MAX_BAR_PAGES = 10 # 분봉 응답은 최신 봉부터 한 쪽씩 옴 → 세션 시작 봉까지 연속조회


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
    ## 고가 - 저가
    ## |고가 - 이전 종가|
    ## |저가 - 이전 종가|
    ## 3개 중 큰 값을 모아둔 값들의 평균
    if i + 1 < n: ##봉이 충분한지 확인
        return None
    trs = []
    for j in range(i - n + 1, i + 1):
        b = bars[j]
        if j == 0:
            trs.append(b["high"] - b["low"])
        else:
            pc = bars[j - 1]["close"] ##이전 봉의 종가
            trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
    return sum(trs) / n


def session_range(bars: list[dict], session: str) -> tuple[float, float, int] | None:
    ses = SESSIONS[session] #구간의 최고가,최저가, 봉 개수 반환
    r_end = add_time(ses["open_t"], RULE["range_min"])
    rng = [b for b in bars if ses["open_t"] <= b["t"] < r_end]
    if len(rng) < RULE["range_min"] - 1:
        return None
    return max(b["high"] for b in rng), min(b["low"] for b in rng), len(rng)


def check_signal(bars: list[dict], i: int, product: str, session: str) -> dict | None:
    """bars[i](마감된 봉)가 진입 신호인지. bars 는 그날 세션 시작부터 시간순."""
    prd, ses = PRODUCTS[product], SESSIONS[session]
    b = bars[i]
    r_end = add_time(ses["open_t"], RULE["range_min"]) # 개장 범위 수집이 끝나는 시각
    if not (r_end <= b["t"] <= ses["last_entry"]):
        return None
    rg = session_range(bars, session) #구간의 최고가,최저가,봉 개수 반환
    if rg is None:
        return None
    hi, lo, _ = rg
    width = round((hi - lo) / prd["tick"])
    if width < RULE["min_range_ticks"]: #(최고가-최저가)가 8틱보다 좁다면 거래를 제외하겠다.
        return {"blocked": "range_narrow", "range_hi": hi, "range_lo": lo, "range_ticks": width}
    ## side = 1: 범위 최고가보다 높음 -> 매수 방향
    ## side = -1: 범위 최저가보다 낮음 -> 매도 방향
    ## side = 0: 범위 안이거나 경계와 같음 -> 신호 없음
    side = 1 if b["close"] > hi else (-1 if b["close"] < lo else 0)
    if side == 0:
        return None
    atr = atr_at(bars, i, RULE["atr_n"]) #i번봉을 포함한 최근 14개의 봉의 ATR
    if atr is None:
        return {"blocked": "atr_not_ready"}
    # ATR을 이용하여 손절 거리 계산, 최종적으로 몇 틱 떨어진 곳에 손절할 지
    stop_ticks = max(RULE["min_stop_ticks"], math.ceil(RULE["k"] * atr / prd["tick"]))
    return {"side": side, "range_hi": hi, "range_lo": lo, "range_ticks": width,
            "atr": round(atr, 3), "stop_ticks": stop_ticks, "signal_t": b["t"], "close": b["close"]}


# ── 기록 ──────────────────────────────────────────────────────
class Journal:
    def __init__(self, out: Path, clock: Clock):
        self.out, self.clock = out, clock
        out.mkdir(parents=True, exist_ok=True)
        self.ev = open(out / "events.jsonl", "a", encoding="utf-8") #사건 기록용 events.jsonl을 이어쓰기모드("a")로 열겠다
        self.files = {}
        self.cols = {
            "signals": ["ts", "product", "session", "signal_t", "side", "close", "range_hi", "range_lo",
                        "range_ticks", "atr", "stop_ticks", "action", "reason"],
            "orders": ["ts", "product", "session", "purpose", "side", "ord_no", "how", "status",
                       "fill_price", "fill_qty", "ref_price", "slip_ticks", "latency_ms", "source"],
            "trades": ["product", "session", "side", "entry_ts", "entry", "stop", "target", "exit_ts",
                       "exit", "reason", "ticks", "pnl", "currency", "pnl_krw", "hold_min"],
        }

    def event(self, kind: str, **kw) -> None: # 사건 기록
        rec = {"ts": self.clock.now().isoformat(timespec="seconds"), "kind": kind, **kw}
        self.ev.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.ev.flush()
        print(f"{self.clock.now():%H:%M:%S}  [{kind}] " + " ".join(f"{k}={v}" for k, v in kw.items()), flush=True)
        ##j.event("entered", product = "SFC", entry = 15000) 이렇게 호출하면
        ## 현재 시각과 함께 events.jsonl에 한 줄 추가
        ## {"ts": "2026-11-03T10:46:02", "kind": "entered", "product": "SFC", "entry": 15000}
    def row(self, name: str, **kw) -> None: ##표 형태의 CSV를 기록하겠다
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
        self.info: dict[str, dict] = {} #종목별 거래소, 계약 정보
        self.bars: dict[str, list[dict]] = {} #종목별 분봉 데이터
        self.seen_bar: dict[str, str] = {} #이미 처리하거나 기록한 봉 정보
        self.state: dict[tuple[str, str], dict] = {} #종목 세션별 대기,보유,종료 상태
        self.fx = {"USD": a.usdkrw, "HKD": a.hkdkrw} #원화 환산용 환율

    # 시세
    def refresh_bars(self, prd: str) -> list[dict]:
        info, now = self.info[prd], self.clock.now()
        today = now.strftime("%Y%m%d")
        rows, key = [], "99999999" ## 조회할 계약과 날짜 준비
        for _ in range(MAX_BAR_PAGES): #최대 조회 페이지 수(10회) 만큼 반복 조회하겠다.
            body = self.api.chart_minute(info["exch_cd"], info["symbol"], today, today, next_key=key)
            page = body.get("Occurs1") or []
            rows += page #응답의 Occurs1 목록을 rows에 이어붙임
            ## 이번 페이지에서 오늘 날짜에 해당하는 봉 중 가장 이른 시각을 찾는다.
            oldest = min((str(r.get("occurs1_exch_tm")).strip().zfill(6) for r in page
                          if str(r.get("occurs1_ref_dt")).strip() == today), default="000000")
            key = str(body.get("nxt_key") or "").strip()
            if not page or oldest <= PRODUCTS[prd]["session_start"] or not key or set(key) == {"9"}:
                break 
            #not page: 이번 응답에 봉이 없음
            #oldest <= PRODUCTS[prd]["session_start"]: 필요한 수집시각까지 도달했거나 더 이전 봉까지 받음
            #not key: 서버가 다음 조회 키를 주지 않음
            #set(key) == {"9"}: 키가 9로만 구성됨
        out, seen = [], set() 
        #out: 정리한 봉을 담을 list
        #seen: 이번 조회에서 이미 처리한 시각을 담을 집합
        for r in rows:
            #d: 날짜 t: 시각
            d, t = str(r.get("occurs1_ref_dt")).strip(), str(r.get("occurs1_exch_tm")).strip().zfill(6)
            if d != today or t < PRODUCTS[prd]["session_start"] or t in seen:
                continue #오늘 데이터가 아니거나, 상품별 수집 시작 시간보다 이전인 경우
            seen.add(t) #처리한 시각을 기억
            start = datetime.strptime(d + t, "%Y%m%d%H%M%S") #날짜 시각 문자열을 datetime으로 변환
            if start + timedelta(minutes=1) > now:   # 진행 중인 봉 제외
                continue
            out.append(dict(t=t, start=start, open=num(r.get("occurs1_open_pric")), high=num(r.get("occurs1_high_pric")),
                            low=num(r.get("occurs1_low_pric")), close=num(r.get("occurs1_close_pric")),
                            volume=num(r.get("occurs1_cum_trd_qty"))))
        out.sort(key=lambda b: b["t"]) #정리한 봉들을 시각이 이른 순서로 정렬
        # 새로 마감된 봉 기록
        path = self.j.out / f"bars_{info['symbol']}.csv"
        new = not path.exists()
        last = self.seen_bar.get(prd, "") 
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new: #새 파일일 경우 열 제목을 작성
                w.writerow(["date", "t", "open", "high", "low", "close", "volume"])
            for b in out: #이전 기록보다 새로운 봉만 저장
                if b["t"] > last:
                    w.writerow([today, b["t"], b["open"], b["high"], b["low"], b["close"], b["volume"]])
        if out: #out 이 비어있지 않으면 가장 마지막 봉의 시각을 저장한다.
            self.seen_bar[prd] = out[-1]["t"] #앞에서 시간 순 정렬했으므로 out[-1]은 가장 최근 봉이다.
        self.bars[prd] = out #메모리의 봉 목록을 갱신. 
        #이후 step_bars()가 이 목록을 읽어서 신호를 판단한다.
        return out

    def quote(self, prd: str) -> dict:
        """현재가, 매수가, 매도가 반환"""
        info = self.info[prd]
        r = self.api.price(info["exch_cd"], info["symbol"])
        return dict(last=num(r.get("occurs1_last_pric")), bid=num(r.get("occurs1_bid_pric")),
                    ask=num(r.get("occurs1_ask_pric"))) ##현재가, 매수가, 매도가

    # 주문 (체결가 반환, 실패 None)
    def execute(self, prd: str, ses: str, side: int, purpose: str, ref: float) -> float | None:
        info, sname = self.info[prd], "buy" if side == 1 else "sell"
        t0 = time.time() #실제 시각을 초 단위 숫자로 저장
        if self.a.dry_run: #주문 없는 시험 모드라면
            q = self.quote(prd) #현재가와 호가를 조회
            # 가상 체결가격을 선택
            # 매수라면 매도호가(ask)에 살 수 있다고 가정
            # 매도라면 매수호가(bid)에 팔 수 있다고 가정
            # 사용할 호가를 얻지 못한 경우 현재가로 가상 체결가격을 대체
            px = (q["ask"] if side == 1 else q["bid"]) or q["last"]
            self._order_row(prd, ses, purpose, sname, "", "sim", "6", px, 1, ref, t0, "simulated")
            # ord_no = "" : 실제 주문번호가 없으므로 빈 문자열
            # sts = "6" : 체결 상태로 기록
            # qty = 1 : 가상 체결수량 1계약
            # ref = ref: 슬리피지 비교에 사용할 기준가격
            # source = "simulated" : 프로그램 내부 가상 체결임을 표시
            return px
        try:
            ord_no, how = send_order(self.api, info, sname) # 모의계좌에 주문을 보내고, 반환된 주문번호와 주문 방식을 각각 저장
        except NoResponse:
            self.j.event("order_no_response", product=prd, purpose=purpose,
                         note="재전송하지 않음 — 주문내역·잔고로 확인")
            self._order_row(prd, ses, purpose, sname, "", "", "NO_RESPONSE", "", "", ref, t0, "paper_account")
            return self._fill_from_position(prd, side) #잔고 확인 함수를 호출
        except ApiFailure as e:
            self.j.event("order_rejected", product=prd, purpose=purpose, code=e.code, msg=e.message) 
            self._order_row(prd, ses, purpose, sname, "", "", f"REJECTED {e.code}", "", "", ref, t0, "paper_account")#주문기록에 상태 남기기
            return None
        r = wait_fill(self.api, ord_no, timeout=15.0)
        sts = str(r.get("occurs1_ord_sts", "")).strip()
        px = num(r.get("occurs1_exec_pric"))
        self._order_row(prd, ses, purpose, sname, ord_no, how, sts, px or "", r.get("occurs1_exec_qty", ""),
                        ref, t0, "paper_account")
        if sts == "6" and px: ##주문상태가 전체체결 -> 체결가격 반환
            return px
        rem = int(num(r.get("occurs1_ord_rem_qty")) or 1) #취소 요청 수량
        try:
            self.api.cancel(info["symbol"], ord_no, rem, sname)
            self.j.event("cancel_unfilled", product=prd, ord_no=ord_no)
        except (ApiFailure, NoResponse) as e:
            self.j.event("cancel_failed", product=prd, ord_no=ord_no, err=str(e))
        return None

    def _fill_from_position(self, prd: str, side: int) -> float | None:
        time.sleep(2)
        try: # 해당 계약의 순포지션 조회
            pos = self.api.net_position(self.info[prd]["symbol"])
        except (ApiFailure, NoResponse):
            return None
        self.j.event("position_after_no_response", product=prd, position=pos) #조회 결과를 event.jsonl에 기록
        return None

    def _order_row(self, prd, ses, purpose, side, ord_no, how, sts, px, qty, ref, t0, source):
        """주문 결과와 슬리피지,처리시간을 정리해서 orders.csv에 기록하는 메소드
           주문을 보내거나 체결을 결정하지는 않는다"""
        slip = "" #슬리피지 초기값을 비워둔다. 슬리피지는 체결가격이 기준가격보다 얼마나 유리하거나 불리했는지를 틱 단위로 나타냄.
        if px not in ("", None) and ref: #가격이 있으면 슬리피지를 계산하겠다.
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
        if pos != expected: #예상과 실제가 다르면 "position_mismatch"로 events.jsonl에 기록
            self.j.event("position_mismatch", product=prd, expected=expected, actual=pos)
        else: #예상과 실제가 같으면 "recondile_ok"를 기록
            self.j.event("reconcile_ok", product=prd, position=pos)

    # 한 종목·세션 처리
    def step_bars(self, prd: str, ses: str) -> None:
        st = self.state[(prd, ses)] #해당 상품 세션의 상태를 가져옴
        if st["phase"] != "wait": #세션의 상태가 wait 상태가 아니면 함수 종료
            return
        bars, now = self.bars.get(prd, []), self.clock.now()
        for i, b in enumerate(bars): # (0, 첫번째 봉 딕셔너리),(1, 두 번째 봉 딕셔너리) 
            if b["t"] <= st["last_checked"]: #이미 검사한 봉은 건너뛰겠다
                continue
            st["last_checked"] = b["t"] #이번 봉을 검사 대상으로 처리했다고 표시
            sig = check_signal(bars, i, prd, ses) # 봉i에 대해 signal이 있는지 확인
            if sig is None:
                continue
            ## 봉 마감 후 경과 시간을 초로 계산
            age = (now - (b["start"] + timedelta(minutes=1))).total_seconds()
            ## **: 항목들을 바깥 딕셔너리에 펼쳐 넣겠다. row 예시:
            ## {
            ##    "product": "SFC",
            ##    "session": "am",
            ##    "signal_t": "104500",
            ##    "close": 12835.0,
            ##    "range_hi": 12830.0,
            ##    "range_lo": 12800.0,
            ##    "range_ticks": 30,
            ##    "atr": 5.2,
            ##    "stop_ticks": 11
            ## }
            row = dict(product=prd, session=ses, signal_t=b["t"], close=b["close"], **{k: v for k, v in sig.items()
                       if k in ("range_hi", "range_lo", "range_ticks", "atr", "stop_ticks")})
            if "blocked" in sig: ##진입이 제외되었다면("range narrow", "atr_not_ready")
                if sig["blocked"] == "range_narrow":  #개장 범위가 8틱 미만인 경우
                    ## 이미 만든 row 항목을 **row로 펼쳐 전달.
                    ## action = "skip_session": 이 세션을 건너뛰겠다는 정보를 signals.csv에 기록
                    self.j.row("signals", **row, action="skip_session", reason="range_narrow")
                    ## events.jsonl과 터미털에도 남긴다
                    self.j.event("session_skipped", product=prd, session=ses, range_ticks=sig.get("range_ticks"))
                    st["phase"] = "done"
                    return
                continue
            row["side"] = "long" if sig["side"] == 1 else "short"
            if age > STALE_SEC:
                # 첫 돌파 봉을 제때 못 봤으면(실행 멈춤 등) 그 세션은 거래하지 않음 — 뒤 봉으로 들어가면 규칙과 달라짐
                # 세션을 건너뛴다는 내용과 stale_bar_120s 같은 이유 저장
                self.j.row("signals", **row, action="skip_session", reason=f"stale_bar_{int(age)}s")
                self.j.event("missed_first_signal", product=prd, session=ses, bar=b["t"], age_s=int(age))
                st["phase"] = "done"
                return
            self.j.row("signals", **row, action="enter", reason="")
            self.enter(prd, ses, sig, b)
            return

    def enter(self, prd: str, ses: str, sig: dict, bar: dict) -> None:
        st, prdc = self.state[(prd, ses)], PRODUCTS[prd]
        side = sig["side"]
        px = self.execute(prd, ses, side, "entry", bar["close"]) #purpose: entry(진입)
        if px is None: #지정가가 없다면
            self.j.event("entry_failed", product=prd, session=ses)
            st["phase"] = "done"
            self.reconcile(prd, 0) #실제 순포지션이 예상값0과 같은지 비교(취소가 잘 됐는지 확인)
            return
        stop = px - side * sig["stop_ticks"] * prdc["tick"] #손절 가격 계산
        target = px + side * RULE["m"] * sig["stop_ticks"] * prdc["tick"] #익절 가격 계산
        st.update(phase="in", side=side, entry=px, stop=stop, target=target, entry_ts=self.clock.now())
        self.j.event("entered", product=prd, session=ses, side=side, entry=px, stop=stop, target=target)
        self.reconcile(prd, side) 

    def step_position(self, prd: str, ses: str) -> None:
        st = self.state[(prd, ses)]
        if st["phase"] != "in": #상품,세션이 포지션 보유 상태가 아니라면 함수를 바로 끝낸다
            return
        now = hms(self.clock.now()) #현재 시각을 HHMMSS 문자열로 저장
        side = st["side"] #보유 포지션의 방향 가져오기
        reason = "" #청산 사유를 빈 문자열로 초기화
        if now >= PRODUCTS[prd]["exit"][ses]:  #정해둔 시간 청산 시각에 도달했으면
            reason, ref = "time", None #시간에 따른 청산, 손절 익절처럼 특정 가격을 기준으로 발생한 청산이 아니므로 아직 기준가격을 정하지 않음
        else:
            try:
                q = self.quote(prd)
            except (ApiFailure, NoResponse) as e:
                self.j.event("quote_failed", product=prd, err=str(e)); return
            last = q["last"] #현재가
            if last and ((side == 1 and last <= st["stop"]) or (side == -1 and last >= st["stop"])):
                #매도 포지션(side==1): 현재가가 손절가 이상이면 손절, 매수 포지션(side==-1): 현재가가 손절가 이하면 손절 
                reason, ref = "stop", st["stop"] #청산 사유, 기록 슬리피지 계산용 기준가격
            elif last and ((side == 1 and last >= st["target"]) or (side == -1 and last <= st["target"])):
                #손절이 아니면 익절 조건 확인
                #매수포지션: 현재가가 목표가 이상이면 익절
                #매도포지션: 현재가가 목표가 이하이면 익절
                reason, ref = "target", st["target"]
        if reason: #시간청산, 손절, 익절
            self.exit(prd, ses, reason, ref) #청산 주문

    def exit(self, prd: str, ses: str, reason: str, ref: float | None) -> None:
        st, prdc = self.state[(prd, ses)], PRODUCTS[prd]
        side = st["side"]
        if ref is None: #기준가격이 없다면(시간청산)
            try:
                ref = self.quote(prd)["last"] #현재가를 기준가격으로 사용
            except (ApiFailure, NoResponse):
                ref = st["entry"] #조회에 실패하면 진입가로 대체
        px = None #청산 체결가격을 초기화
        for attempt in range(5):   # 청산은 될 때까지 (지정가 미체결이면 다시)
            px = self.execute(prd, ses, -side, f"exit_{reason}", ref)
            if px is not None:
                break
            self.j.event("exit_retry", product=prd, attempt=attempt + 1)
            self.clock.sleep(2)
        if px is None: #최대 5번의 청산 시도 후에도 체결가격을 확인하지 못했을 때
            self.j.event("EXIT_FAILED", product=prd, session=ses, note="앱에서 직접 정리 필요")
            st["phase"] = "error"
            return #exit()을 끝낸다. 뒤의 손익계산과 정상 청산 기록은 실행하지 않는다.
        ticks = round(side * (px - st["entry"]) / prdc["tick"]) #손익을 틱 단위로 계산
        pnl = ticks * prdc["tick_value"] #틱 손익을 금액으로 변환
        fx = self.fx.get(prdc["currency"]) or 0 #원화 환산용 환율 가져오기
        held = (self.clock.now() - st["entry_ts"]).total_seconds() / 60 #보유시간을 분 단위로 계산
        ##완료거래를 trades.csv에 저장
        self.j.row("trades", product=prd, session=ses, side="long" if side == 1 else "short",
                   entry_ts=st["entry_ts"].isoformat(timespec="seconds"), entry=st["entry"], stop=st["stop"],
                   target=st["target"], exit_ts=self.clock.now().isoformat(timespec="seconds"), exit=px,
                   reason=reason, ticks=ticks, pnl=pnl, currency=prdc["currency"],
                   pnl_krw=round(pnl * fx) if fx else "", hold_min=round(held, 1))
        self.j.event("exited", product=prd, session=ses, reason=reason, exit=px, ticks=ticks,
                     pnl=f"{pnl} {prdc['currency']}") #청산 완료 사건 기록
        st["phase"] = "done" #세션 종료
        self.reconcile(prd, 0) #잔고 확인

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
    out = Path("results") / f"run_orb_{datetime.now():%Y%m%d_%H%M%S}"  #result파일 경로 
    j = Journal(out, clock)
    api = OverseasClient("paper", *keys_from_env(), out, base_url=a.base_url)
    r = Runner(a, api, clock, j)

    ## 이번 실행의 설정과 환경을 기록하기 위한 딕셔너리
    ## run_id: 결과 폴더 이름. 이번 실행을 구분하는 ID
    ## rule: ATR배수, 범위 길이 등 RULE의 전략설정
    ## sessions: 이번에 선택한 세션의 시간 설정
    ## products: 종목 정보를 나중에 채울 빈 딕셔너리
    ## poll_sec: 현재가 조회 간격
    ## fx: 원화 환산에 사용할 USD HKD 환율
    meta = dict(run_id=out.name, started=clock.now().isoformat(), mode="dry-run" if a.dry_run else "paper",
                env="paper", rule=RULE, sessions={s: SESSIONS[s] for s in sessions}, products={},
                poll_sec=a.poll, fx={"USDKRW": a.usdkrw, "HKDKRW": a.hkdkrw}, python=os.sys.version)
    
    for prd in products:
        info = product_info(api, prd) #api: OverseasClient 객체. prd: 조회할 상품코드. "SFC" 또는 "MHI"
        exch_cd, sym, row = resolve_front(api, prd, info.get("exchange", ""))
        info.update(exch_cd=exch_cd, symbol=sym, symbol_name=row.get("occurs1_sym_nm", "").strip())
        r.info[prd] = info
        meta["products"][prd] = {**info, **PRODUCTS[prd]}  ## 두 딕셔너리를 따로 보관하는게 아니라 
        ##내용을 펼쳐 합치는 문법. 같은 키가 양쪽에 있으면 뒤에 있는 PRODUCTS[prd]값이 우선한다.
        pos = 0 if a.dry_run else api.net_position(sym) 
        # pos = 0: 매수 매도 수량을 합산한 순포지션이 0
        # pos = 1: 순매수 1계약
        # pos = -1: 순매도 1계약
        
        ## 현재 상품의 실행 시작 정보를 기록(evnets.jsonl)
        j.event("start", product=prd, symbol=sym, exch_cd=exch_cd, tick=info.get("tick_size"),
                tick_value=info.get("tick_value"), currency=info.get("currency"), position=pos)
        if pos != 0: ## 기존 포지션이 있으면 프로그램을 종료
            raise SystemExit(f"{sym} 잔고가 {pos} 입니다. check_overseas.py --flatten 으로 정리한 뒤 시작하세요.")
        for ses in sessions:
            r.state[(prd, ses)] = dict(phase="wait", last_checked="")
    ## 실행 설정을 JSON 파일로 저장.(모든 상품의 준비가 끝난 뒤 실행 정보를 한 번 저장)
    (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    ## 실행할 모든 상품,세션의 시간 청산 시각 중 가장 늦은 시각 찾기
    end_t = max(PRODUCTS[p]["exit"][s] for p in products for s in sessions)
    end_t = add_time(end_t, 2) ##마지막 시간 청산 이후에도 2분간 실행을 유지하도록 종료 기준을 정함
    ## 사건 종류: "계획" + 실행할 세션, 실행할 상품, 종료 기준 시각 기록
    j.event("plan", sessions=sessions, products=products, ends_at=end_t)
    last_loop, errors = time.time(), 0 #반복문에 사용할 변수 초기화. last_loop: 현재의 실제 시각
    try:
        while hms(clock.now()) < end_t: ##마지막 청산시각까지 다음을 하겠다.
            gap = time.time() - last_loop   # 실제 시간 기준(시험용 배속과 무관)
            if gap > 60: # 노트북 절전이나 긴 API대기 등으로 반복 간격이 60초를 넘으면 pause 사건을 기록
                j.event("pause", seconds=int(gap), note="실행이 멈췄다가 다시 돎")
            last_loop = time.time() #다음 반복을 위한 기준 시각 갱신
            now = clock.now() # 이번 처리에 사용할 시각 저장
            try:
                # 봉: 매분 2초 이후 한 번씩
                for prd in products:
                    key = now.strftime("%H%M") #현재가 10:46:05라면, key는 "1046"
                    if now.second >= 2 and r.seen_bar.get(prd + "_fetch") != key: #이번 분에 아직 조회하지 않았다면
                        r.seen_bar[prd + "_fetch"] = key  #이번 분에 분봉을 조회함을 표시
                        r.refresh_bars(prd) #분봉 갱신
                        for ses in sessions:
                            r.step_bars(prd, ses) #한 종목 세션 처리, 완료 봉으로 진입 판단
                    for ses in sessions:
                        r.step_position(prd, ses) #반복마다 청산 판단
                errors = 0
            except (ApiFailure, NoResponse) as e:
                errors += 1
                j.event("api_error", err=str(e)[:200], streak=errors)
                if errors >= 20:
                    j.event("too_many_errors"); break
                clock.sleep(min(30, 2 * errors)) #오류가 반복될 수록 더 오래 기다리기
                continue
            clock.sleep(a.poll) #정상 처리 후 기본 간격만큼 대기(5초)
    except KeyboardInterrupt:
        j.event("stopped_by_user")
    finally:
        r.flatten_all("end") #내부 상태가 "in"인 상품세션을 찾아 exit()으로 청산 시도
        for prd in products:
            r.reconcile(prd, 0) #상품별 잔고 대조
        meta["finished"] = clock.now().isoformat() #종료 시각 추가
        ##종료 시각이 추가된 meta를 JSON으로 변환해 기존 run_meta.json을 덮어쓰기
        (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        j.event("end", out=str(out)) #종료 사건 기록


if __name__ == "__main__":
    main()
