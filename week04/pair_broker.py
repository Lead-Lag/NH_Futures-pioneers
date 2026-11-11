"""두 다리 주문을 **하나의 거래(페어)**로 처리합니다.

두 주문을 거의 동시에 보낸다고 함께 체결되는 것은 아닙니다. 그래서 한 페어를 아래 순서로 처리하고,
한쪽만 체결된 상태(레그 리스크)를 따로 다룹니다.

  진입   첫 다리 시장가 → 체결 확인(주문내역)
           ├ 0 체결 → 남은 수량 취소·최종 확인 → ABORTED (노출 없음)
           └ 체결 → 둘째 다리 시장가 → 체결 확인
                       ├ 다 체결 → COMPLETE
                       └ 덜 체결 → 남은 수량 한 번 더(앞 주문이 최종 상태일 때만)
                                     ├ 다 체결 → COMPLETE
                                     └ 실패 → 체결된 다리를 반대로 되돌림 → UNWOUND / 못 되돌리면 BROKEN
  청산   두 다리 모두 끝까지 체결될 때까지 남은 수량을 최대 3번 다시 보냄 → COMPLETE / BROKEN

지키는 것 (2주차와 같음)
- 응답을 못 받은 주문은 **다시 보내지 않고** 주문내역에서 찾습니다. 찾지 못하면 그 다리는 UNKNOWN —
  남은 수량을 보내지 않고, 실행 쪽이 잔고로 대조합니다.
- 「다시 보내기」는 앞 주문이 체결·취소로 **최종 상태임을 확인한 뒤** 남은 수량만큼 새 주문을 내는 것입니다.
- 취소를 요청한 뒤에도 그사이 체결될 수 있어(취소 직전 체결) 취소 뒤 주문내역을 다시 읽어 최종 체결수량을 씁니다.

PaperPairExecutor  모의계좌로 실제 주문. 기록의 source = "paper_account"
SimPairExecutor    주문 없음. 현재가 API 의 매도/매수 1호가로 체결 가정. source = "simulated"
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from nh_client import BusinessError, NHApiError, NHClient, TransportError, num
from pair_engine import ABORTED, BROKEN, BUY, COMPLETE, SELL, UNWOUND, Leg, PairIntent, tick_size

KST = timezone(timedelta(hours=9))


@dataclass
class LegResult:
    role: str              # entry1 · entry2 · retry · unwind · exit · flatten
    name: str              # S | H
    sym: str
    side: str
    qty: int
    ord_no: str = ""
    filled: int = 0
    avg: float = 0.0
    status: str = ""       # FILLED · PARTIAL · UNFILLED · REJECTED · UNKNOWN · ERROR · SIM_FILLED
    message: str = ""
    sent_ts: datetime | None = None
    fill_ts: datetime | None = None
    confirm: str = ""      # 체결을 어디서 확인했나: ws(주문/체결 통보 O1) · rest(주문내역 조회) · sim

    @property
    def final(self) -> bool:
        """앞 주문이 더 체결될 일이 없는지(남은 수량을 새로 보내도 되는지)."""
        return self.status in ("FILLED", "PARTIAL", "UNFILLED", "REJECTED", "SIM_FILLED")


@dataclass
class PairOutcome:
    pair_id: int
    purpose: str
    status: str = ""
    qty: dict[str, int] = field(default_factory=lambda: {"S": 0, "H": 0})     # 이번 목적대로 체결된 수량
    avg: dict[str, float] = field(default_factory=lambda: {"S": 0.0, "H": 0.0})
    unwind_pnl: float = 0.0
    legs: list[LegResult] = field(default_factory=list)
    first_fill_ts: datetime | None = None
    hedged_ts: datetime | None = None   # 두 다리가 맞춰진(또는 노출이 정리된) 시각
    message: str = ""

    @property
    def exposure_ms(self) -> float | str:
        if self.first_fill_ts and self.hedged_ts:
            return round((self.hedged_ts - self.first_fill_ts).total_seconds() * 1000)
        return ""


def _opp(side: str) -> str:
    return SELL if side == BUY else BUY


class BasePairExecutor:
    source = ""

    def __init__(self, client: NHClient, syms: dict[str, str], journal: Any, ticks: dict[str, float],
                 mults: dict[str, float], *, leg_timeout: float = 8.0, retries_entry: int = 1,
                 retries_exit: int = 3) -> None:
        self.c = client
        self.syms = syms
        self.j = journal
        self.ticks = ticks
        self.mults = mults
        self.leg_timeout = leg_timeout
        self.retries_entry = retries_entry
        self.retries_exit = retries_exit

    # ------------------------------------------------------------ 페어 하나
    def execute(self, it: PairIntent) -> PairOutcome:
        out = PairOutcome(it.id, it.purpose)
        if it.is_entry:
            self._entry(it, out)
        else:
            self._exit(it, out)
        self.j.pair({
            "pair_id": it.id, "intent_ts": it.ts.isoformat(), "purpose": it.purpose, "direction": it.direction,
            "reason": it.reason, "status": out.status, "qty_s": out.qty["S"], "qty_h": out.qty["H"],
            "avg_s": out.avg["S"], "avg_h": out.avg["H"], "ref_s": it.ref.get("S", ""), "ref_h": it.ref.get("H", ""),
            "orders": len(out.legs), "first_fill_ts": _iso(out.first_fill_ts), "hedged_ts": _iso(out.hedged_ts),
            "exposure_ms": out.exposure_ms, "unwind_pnl": round(out.unwind_pnl), "message": out.message,
            "source": self.source,
        })
        return out

    def _entry(self, it: PairIntent, out: PairOutcome) -> None:
        l1, l2 = it.legs
        r1 = self._leg(it, l1, "entry1", out)
        if r1.filled == 0:
            out.status = ABORTED
            out.message = f"첫 다리({l1.name}) 체결 없음: {r1.status} {r1.message}"
            return
        out.first_fill_ts = r1.fill_ts
        # 첫 다리가 일부만 체결되면 둘째 다리도 같은 비율로 줄임
        target2 = l2.qty if r1.filled == l1.qty else max(0, round(l2.qty * r1.filled / l1.qty))
        got2, legs2 = self._complete(it, Leg(l2.name, l2.side, target2), "entry2", self.retries_entry, out)
        if target2 > 0 and got2 == target2:
            out.status = COMPLETE
            out.qty = {l1.name: r1.filled, l2.name: got2}
            out.avg = {l1.name: r1.avg, l2.name: _vwap(legs2)}
            out.hedged_ts = legs2[-1].fill_ts if legs2 else None
            out.message = "두 다리 체결" + ("" if r1.filled == l1.qty else f" (첫 다리 부분체결 {r1.filled}/{l1.qty} → 비율 맞춤)")
            return
        # ---- 레그 리스크: 둘째 다리를 다 못 채움 → 체결된 것 전부 되돌림
        self.j.event("leg_risk", f"페어 {it.id}: {l1.name} {r1.filled}계약 체결, {l2.name} {got2}/{target2} — 되돌림 시작")
        ok1, px1 = self._unwind(it, l1, r1.filled, out)
        ok2, px2 = (True, 0.0) if got2 == 0 else self._unwind(it, l2, got2, out)
        out.unwind_pnl = (_pnl(l1, r1.filled, r1.avg, px1, self.mults[l1.name]) if ok1 else 0.0) + \
                         (_pnl(l2, got2, _vwap(legs2), px2, self.mults[l2.name]) if ok2 and got2 else 0.0)
        out.hedged_ts = datetime.now(KST)
        if ok1 and ok2:
            out.status = UNWOUND
            out.message = f"{l2.name} {got2}/{target2} 체결에 그쳐 체결된 다리를 되돌림"
        else:
            out.status = BROKEN
            out.message = "되돌리기를 끝내지 못함 — 잔고 확인 필요"

    def _exit(self, it: PairIntent, out: PairOutcome) -> None:
        ok, legs_by = True, {}
        for i, leg in enumerate(it.legs):
            got, legs = self._complete(it, leg, "exit", self.retries_exit, out)
            legs_by[leg.name] = legs
            out.qty[leg.name] = got
            out.avg[leg.name] = _vwap(legs)
            if i == 0 and legs:
                out.first_fill_ts = next((x.fill_ts for x in legs if x.fill_ts), None)
            if got != leg.qty:
                ok = False
        out.hedged_ts = datetime.now(KST)
        out.status = COMPLETE if ok else BROKEN
        out.message = "두 다리 청산" if ok else f"청산 미완료 {out.qty} — 잔고 확인 필요"

    def _complete(self, it: PairIntent, leg: Leg, role: str, retries: int, out: PairOutcome) -> tuple[int, list[LegResult]]:
        """leg.qty 를 다 채울 때까지(최대 retries 번 더) 남은 수량을 보냅니다."""
        got, legs = 0, []
        for attempt in range(retries + 1):
            need = leg.qty - got
            if need <= 0:
                break
            r = self._leg(it, Leg(leg.name, leg.side, need), role if attempt == 0 else "retry", out)
            legs.append(r)
            got += r.filled
            if not r.final:   # 응답 불명 → 다시 보내지 않음
                self.j.event("order_unknown", f"페어 {it.id} {leg.name} 주문 상태 불명 — 남은 수량 재전송 안 함")
                break
        return got, [x for x in legs if x.filled > 0]

    def _unwind(self, it: PairIntent, leg: Leg, qty: int, out: PairOutcome) -> tuple[bool, float]:
        got, legs = self._complete(it, Leg(leg.name, _opp(leg.side), qty), "unwind", 2, out)
        return got == qty, _vwap(legs)

    def flatten(self, positions: dict[str, int], reason: str) -> dict[str, int]:
        """잔고 기준으로 남은 노출을 시장가로 정리. 돌려주는 값은 각 종목에서 정리한 수량."""
        dummy = PairIntent(0, datetime.now(KST), "FLATTEN", 0, reason, [], {})
        out = PairOutcome(0, "FLATTEN")
        done = {}
        for name, pos in positions.items():
            if pos == 0:
                continue
            got, _ = self._complete(dummy, Leg(name, SELL if pos > 0 else BUY, abs(pos)), "flatten", 2, out)
            done[name] = got
        self.j.event("flatten", f"잔고 정리({reason}): {positions} → 정리 {done}")
        return done

    # ------------------------------------------------------------ 다리 하나
    def _leg(self, it: PairIntent, leg: Leg, role: str, out: PairOutcome) -> LegResult:
        r = LegResult(role, leg.name, self.syms[leg.name], leg.side, leg.qty, sent_ts=datetime.now(KST))
        self._send(r)
        out.legs.append(r)
        ref = it.ref.get(leg.name) or 0.0
        d = 1 if leg.side == BUY else -1
        slip = round((r.avg - ref) * d / tick_size(ref, self.ticks[leg.name]), 2) if (r.filled and ref) else ""
        self.j.leg({
            "pair_id": it.id, "purpose": it.purpose, "role": role, "leg": leg.name, "sym": r.sym,
            "side": leg.side, "qty": leg.qty, "ord_no": r.ord_no, "status": r.status, "filled": r.filled,
            "avg": r.avg, "ref": ref, "slippage_ticks": slip, "sent_ts": _iso(r.sent_ts), "fill_ts": _iso(r.fill_ts),
            "latency_ms": round((r.fill_ts - r.sent_ts).total_seconds() * 1000) if r.fill_ts and r.sent_ts else "",
            "confirm": r.confirm, "message": r.message, "source": self.source,
        })
        return r

    def _send(self, r: LegResult) -> None:  # pragma: no cover
        raise NotImplementedError


class SimPairExecutor(BasePairExecutor):
    source = "simulated"

    def _send(self, r: LegResult) -> None:
        try:
            q = self.c.price(r.sym)
        except NHApiError as exc:
            r.status, r.message = "ERROR", f"현재가 조회 실패 {exc}"
            return
        last, ask, bid = num(q.get("last_pric")), num(q.get("ask_pric")), num(q.get("bid_pric"))
        t = tick_size(last, self.ticks[r.name])
        px = (ask if ask > 0 else last + t) if r.side == BUY else (bid if bid > 0 else last - t)
        if px <= 0:
            r.status, r.message = "ERROR", "호가·현재가 없음"
            return
        r.ord_no, r.filled, r.avg, r.status, r.confirm = "SIM", r.qty, round(px, 2), "SIM_FILLED", "sim"
        r.fill_ts = datetime.now(KST)


class PaperPairExecutor(BasePairExecutor):
    source = "paper_account"
    POLL = 0.5

    def __init__(self, *a, feed: Any = None, **kw) -> None:
        super().__init__(*a, **kw)
        self.known_orders: set[str] = set()
        self.feed = feed   # ws_feed.RealtimeFeed — 있으면 주문/체결 통보(O1)로 먼저 확인하고, 주문내역 조회는 보조

    def _send(self, r: LegResult) -> None:
        try:
            body = self.c.order(r.sym, side=r.side, qty=r.qty)
            r.ord_no = str(body.get("ord_no") or "").strip()
        except BusinessError as exc:
            r.status, r.message = "REJECTED", f"{exc.code} {exc}"
            return
        except TransportError as exc:
            self.j.event("order_unknown", f"{r.name} 주문 응답 못 받음: {exc} — 주문내역 확인")
            r.ord_no = self._find_unknown(r)
            if not r.ord_no:
                r.status, r.message = "UNKNOWN", "응답 없음, 주문내역에도 없음 — 재전송 안 함(잔고로 대조)"
                return
        except NHApiError as exc:
            r.status, r.message = "ERROR", str(exc)
            return
        if not r.ord_no:
            r.status, r.message = "ERROR", f"주문번호 없는 응답 {body}"
            return
        self.known_orders.add(r.ord_no)
        self._wait(r)

    def _wait(self, r: LegResult) -> None:
        deadline = time.monotonic() + self.leg_timeout
        st = None
        ws = self.feed is not None and self.feed.healthy()
        next_rest = time.monotonic() + (1.5 if ws else self.POLL)
        while time.monotonic() < deadline:
            time.sleep(0.05 if ws else self.POLL)
            # ① 실시간 주문/체결 통보(O1) — 조회 없이 바로
            if ws and self.feed.healthy():
                o = self.feed.order_state(r.ord_no)
                if o:
                    if o["status"] == "9" or o["err"]:
                        r.status, r.message, r.confirm = "REJECTED", f"실시간 통보 거부 {o['err']}", "ws"
                        r.filled, r.avg = o["filled"], o["avg"]
                        return
                    if o["filled"] >= r.qty and o["avg"] > 0:
                        r.filled, r.avg, r.status, r.confirm = o["filled"], o["avg"], "FILLED", "ws"
                        r.fill_ts = datetime.now(KST)
                        return
            # ② 주문내역 조회 — 실시간이 없거나 늦을 때의 대체
            if time.monotonic() < next_rest:
                continue
            next_rest = time.monotonic() + (1.0 if ws else self.POLL)
            try:
                st = self.c.order_fills(r.ord_no)
            except NHApiError as exc:
                self.j.event("api_error", f"주문내역 조회 실패: {exc}")
                continue
            if not st:
                continue
            if st["reject"]:
                r.status, r.message, r.filled, r.confirm = "REJECTED", st["reject"], st["filled"], "rest"
                r.avg = st["avg"]
                return
            if st["filled"] >= r.qty:
                r.filled, r.avg, r.status, r.fill_ts = st["filled"], st["avg"], "FILLED", datetime.now(KST)
                r.confirm = "rest"
                return
        # 시간 안에 다 안 채워짐 → 남은 수량 취소 → 취소 뒤 다시 읽어 최종 체결수량 확정(취소 직전 체결 대비)
        filled = st["filled"] if st else 0
        remain = r.qty - filled
        msg = f"{self.leg_timeout:.0f}초 안에 {filled}/{r.qty} 체결"
        cancel_ok = False
        try:
            self.c.cancel(r.sym, org_ord_no=r.ord_no, side=r.side, qty=remain)
            cancel_ok = True
            msg += " → 남은 수량 취소 요청"
        except NHApiError as exc:
            msg += f" → 취소 실패 {exc}"
        final = None
        for _ in range(4):
            time.sleep(0.8)
            try:
                final = self.c.order_fills(r.ord_no) or final
            except NHApiError:
                continue
            if final and (final["filled"] + final["cancelled"] >= r.qty):
                break
        if final:
            r.filled, r.avg = final["filled"], final["avg"]
            msg += f" → 최종 체결 {final['filled']} · 취소 {final['cancelled']}"
            settled = final["filled"] + final["cancelled"] >= r.qty
        else:
            settled = False
        r.message = msg
        r.confirm = "rest"
        if r.filled >= r.qty:
            r.status, r.fill_ts = "FILLED", datetime.now(KST)
        elif settled or (cancel_ok and final):
            r.status = "PARTIAL" if r.filled else "UNFILLED"
            r.fill_ts = datetime.now(KST) if r.filled else None
        else:
            r.status = "UNKNOWN"   # 취소·체결 최종 상태를 확인하지 못함 → 남은 수량 재전송 금지

    def _find_unknown(self, r: LegResult) -> str:
        time.sleep(1.5)
        try:
            rows = self.c.order_rows()
        except NHApiError:
            return ""
        hhmmss = (r.sent_ts or datetime.now(KST)).strftime("%H%M%S")
        side = "1" if r.side == BUY else "2"
        for row in reversed(rows):
            no = str(row.get("occurs1_ord_no", "")).strip()
            if (no not in self.known_orders and str(row.get("occurs1_sym", "")).strip() == r.sym
                    and str(row.get("occurs1_trd_div")) == side
                    and str(row.get("occurs1_exch_rcv_tm", ""))[:6] >= hhmmss):
                return no
        return ""


# ---------------------------------------------------------------- 도우미
def _iso(ts: datetime | None) -> str:
    return ts.isoformat(timespec="milliseconds") if ts else ""


def _vwap(legs: list[LegResult]) -> float:
    q = sum(x.filled for x in legs)
    return round(sum(x.filled * x.avg for x in legs) / q, 2) if q else 0.0


def _pnl(leg: Leg, qty: int, entry: float, exit_: float, mult: float) -> float:
    d = 1 if leg.side == BUY else -1
    return d * (exit_ - entry) * qty * mult
