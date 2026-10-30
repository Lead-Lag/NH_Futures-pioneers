"""주문 처리. 두 가지 중 하나를 씁니다 — README 「실행 환경」에 어느 쪽인지 적습니다.

PaperBroker  모의계좌로 실제 주문(domestic_order) → 주문내역(order-history)으로 체결 확인
             → 미체결이 남으면 취소(domestic_cancel). 기록의 source = "paper_account".
SimBroker    주문을 내지 않음. 판정 직후 현재가 API 의 매도/매수 1호가로 체결가를 정함.
             기록의 source = "simulated". **모의계좌 체결이 아닙니다.**
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from engine import BUY, Intent
from journal import RunJournal
from nh_client import BusinessError, NHApiError, NHClient, TransportError, num

KST = timezone(timedelta(hours=9))


@dataclass
class Result:
    ok: bool
    filled_qty: int = 0
    avg_price: float = 0.0
    ord_no: str = ""
    status: str = ""
    message: str = ""
    ts: datetime | None = None


class BaseBroker:
    source = ""

    def __init__(self, client: NHClient, sym: str, journal: RunJournal, tick: float) -> None:
        self.c = client
        self.sym = sym
        self.j = journal
        self.tick = tick

    def execute(self, intent: Intent) -> Result:
        sent = datetime.now(KST)
        res = self._execute(intent, sent)
        self.j.order(
            {
                "intent_id": intent.id, "intent_ts": intent.ts.isoformat(), "purpose": intent.purpose,
                "side": intent.side, "qty": intent.qty, "order_type": "시장가 FAS", "reason": intent.reason,
                "ref_price": intent.ref_price, "sent_ts": sent.isoformat(timespec="milliseconds"),
                "ord_no": res.ord_no, "status": res.status, "filled_qty": res.filled_qty,
                "avg_price": res.avg_price, "message": res.message,
            }
        )
        if res.ok and res.filled_qty > 0:
            d = 1 if intent.side == BUY else -1
            slip = round((res.avg_price - intent.ref_price) * d / self.tick, 1) if intent.ref_price else ""
            self.j.fill(
                {
                    "intent_id": intent.id, "ord_no": res.ord_no,
                    "ts": (res.ts or datetime.now(KST)).isoformat(timespec="milliseconds"),
                    "purpose": intent.purpose, "side": intent.side, "qty": res.filled_qty,
                    "price": res.avg_price, "ref_price": intent.ref_price, "slippage_ticks": slip,
                    "source": self.source,
                }
            )
        return res

    def _execute(self, intent: Intent, sent: datetime) -> Result:  # pragma: no cover
        raise NotImplementedError


class SimBroker(BaseBroker):
    source = "simulated"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.n = 0

    def _execute(self, intent: Intent, sent: datetime) -> Result:
        self.n += 1
        try:
            q = self.c.price(self.sym)
        except NHApiError as exc:
            return Result(False, status="QUOTE_FAIL", message=str(exc))
        last = num(q.get("last_pric"))
        ask, bid = num(q.get("ask_pric")), num(q.get("bid_pric"))
        if intent.side == BUY:
            px = ask if ask > 0 else last + self.tick
        else:
            px = bid if bid > 0 else last - self.tick
        if px <= 0:
            return Result(False, status="NO_QUOTE", message="호가·현재가 없음")
        return Result(True, intent.qty, round(px, 2), f"SIM-{self.n:04d}", "SIM_FILLED",
                      "현재가 API 1호가로 체결 가정", datetime.now(KST))


class PaperBroker(BaseBroker):
    source = "paper_account"

    def __init__(self, *a, fill_timeout: float = 10.0, **kw) -> None:
        super().__init__(*a, **kw)
        self.fill_timeout = fill_timeout
        self.known_orders: set[str] = set()

    def _execute(self, intent: Intent, sent: datetime) -> Result:
        try:
            body = self.c.order(self.sym, side=intent.side, qty=intent.qty)
            ord_no = str(body.get("ord_no") or "").strip()
        except BusinessError as exc:
            return Result(False, status="REJECTED", message=f"{exc.code} {exc}")
        except TransportError as exc:
            # 접수 여부 불명 → 다시 보내지 않고 주문내역에서 찾아봅니다.
            self.j.event("order_unknown", f"주문 응답 못 받음: {exc} — 주문내역 확인")
            ord_no = self._find_unknown(intent, sent)
            if not ord_no:
                return Result(False, status="UNKNOWN", message="응답 없음, 주문내역에도 없음 — 재전송 안 함")
        except NHApiError as exc:
            return Result(False, status="ERROR", message=str(exc))

        self.known_orders.add(ord_no)
        self.j.event("order", f"{intent.purpose} {intent.side} {intent.qty}계약 시장가 접수 ord_no={ord_no}")
        return self._wait_fill(intent, ord_no)

    def _wait_fill(self, intent: Intent, ord_no: str) -> Result:
        deadline = time.monotonic() + self.fill_timeout
        last = None
        while time.monotonic() < deadline:
            time.sleep(0.7)
            try:
                last = self._row_of(ord_no)
            except NHApiError as exc:
                self.j.event("api_error", f"주문내역 조회 실패: {exc}")
                continue
            if not last:
                continue
            filled = int(num(last.get("occurs1_exec_qty")))
            reject = str(last.get("occurs1_ord_rejt_rsn") or "").strip()
            if reject:
                return Result(False, filled, 0.0, ord_no, "REJECTED", reject)
            if filled >= intent.qty:
                return Result(True, filled, _avg(last), ord_no, str(last.get("occurs1_ord_sts_nm") or "체결"),
                              "", datetime.now(KST))

        # 시간 안에 다 안 채워짐 → 남은 수량 취소 후 최종 확인
        filled = int(num(last.get("occurs1_exec_qty"))) if last else 0
        remain = intent.qty - filled
        msg = f"{self.fill_timeout:.0f}초 안에 미체결 {remain}계약"
        if remain > 0:
            try:
                self.c.cancel(self.sym, org_ord_no=ord_no, side=intent.side, qty=remain)
                msg += " → 취소 요청"
            except NHApiError as exc:
                msg += f" → 취소 실패 {exc}"
            time.sleep(1.0)
            try:
                last = self._row_of(ord_no) or last
                filled = int(num(last.get("occurs1_exec_qty"))) if last else filled
            except NHApiError:
                pass
        self.j.event("unfilled", msg, ord_no=ord_no)
        if filled > 0:
            return Result(True, filled, _avg(last), ord_no, "PARTIAL", msg, datetime.now(KST))
        return Result(False, 0, 0.0, ord_no, "UNFILLED", msg)

    def _row_of(self, ord_no: str) -> dict | None:
        best = None
        for r in self.c.order_rows():
            if str(r.get("occurs1_ord_no", "")).strip() == ord_no:
                if best is None or num(r.get("occurs1_exec_qty")) >= num(best.get("occurs1_exec_qty")):
                    best = r
        return best

    def _find_unknown(self, intent: Intent, sent: datetime) -> str:
        time.sleep(1.5)
        try:
            rows = self.c.order_rows()
        except NHApiError:
            return ""
        hhmmss = sent.strftime("%H%M%S")
        side = "1" if intent.side == BUY else "2"
        for r in reversed(rows):
            no = str(r.get("occurs1_ord_no", "")).strip()
            if (
                no not in self.known_orders
                and str(r.get("occurs1_sym", "")).strip() == self.sym
                and str(r.get("occurs1_trd_div")) == side
                and str(r.get("occurs1_exch_rcv_tm", ""))[:6] >= hhmmss
            ):
                return no
        return ""


def _avg(row: dict | None) -> float:
    if not row:
        return 0.0
    avg = num(row.get("occurs1_avg_exec_pric"))
    return round(avg if avg > 0 else num(row.get("occurs1_exec_pric")), 2)
