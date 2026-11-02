"""해외 API 점검 + 과거 1분봉 내려받기 + (선택) 주문 왕복 시험.

    python check_overseas.py                      # 상품 정보 · 근월물 · 현재가 · 과거 분봉
    python check_overseas.py --smoke-order        # 위 + 종목마다 1계약 매수→매도 왕복, 잔고 대조
    python check_overseas.py --products SFC --history-days 10

키: 환경변수 NH_OVS_APP_KEY / NH_OVS_APP_SECRET (해외 모의 APP Key)
결과: results/check_<시각>/  (api_calls.csv · run_meta.json · bars_<품목>_<종목>.csv · orders.csv)
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from nh_overseas import ApiFailure, NoResponse, OverseasClient, keys_from_env, num

MONTHS = "FGHJKMNQUVXZ"
# 품목 → 거래소 (장운영정보 응답에 없을 때 쓰는 기본값)
EXCH_HINT = {"SFC": "SGX", "MHI": "HKEX"}
# 시장가를 받지 않는 거래소: 「체결될 만한 지정가」(매수 = 매도1호가 + N틱, 매도 = 매수1호가 − N틱)로 대신
LIMIT_ONLY_EXCH = {"HKEX", "HKE"}
LIMIT_SLIP_TICKS = 3


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {msg}", flush=True)


def candidates(prd: str, today: datetime, n: int = 3) -> list[str]:
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append(f"{prd}{MONTHS[m - 1]}{y % 100:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def product_info(api: OverseasClient, prd: str) -> dict:
    info: dict = {"product": prd}
    def pick(rows):
        return [r for r in rows if str(r.get("occurs1_prd_cd")).strip() == prd
                and str(r.get("occurs1_fut_opt_div_cd")).strip() == "F"]
    try:
        ops = pick(api.market_operations(prd_cd=prd))
        if not ops and prd in EXCH_HINT:
            ops = pick(api.market_operations(exch_cd=EXCH_HINT[prd]))
    except (ApiFailure, NoResponse) as e:
        log(f"   장운영정보 실패: {e}")
        ops = []
    if not ops:
        log("   장운영정보에 이 품목이 없음 → 기본 거래소 사용")
    if ops:
        r = ops[0]
        info.update(exchange=r["occurs1_exch_cd"].strip(), name=r.get("occurs1_prd_nm", "").strip(),
                    currency=r.get("occurs1_cur_cd", "").strip(),
                    hours_1=r.get("occurs1_trd_tm_inf", "").strip(),
                    hours_2=r.get("occurs1_trd_tm_inf_2", "").strip(),
                    hours_next=r.get("occurs1_nxd_trd_tm_inf", "").strip(),
                    settle_time=r.get("occurs1_sett_pric_tm_inf", "").strip())
    try:
        det = [r for r in api.product_detail(prd_cd=prd) if str(r.get("occurs1_prd_cd")).strip() == prd]
        if not det and prd in EXCH_HINT:
            det = [r for r in api.product_detail(exch_cd=EXCH_HINT[prd]) if str(r.get("occurs1_prd_cd")).strip() == prd]
    except (ApiFailure, NoResponse) as e:
        log(f"   품목세부정보 실패: {e}")
        det = []
    if not det:
        log("   품목세부정보에 이 품목이 없음 → 틱·승수는 나중에 확인 필요")
    if det:
        r = det[0]
        info.update(tick_size=num(r.get("occurs1_tick_unt")), tick_value=num(r.get("occurs1_tick_val")),
                    contract_unit=num(r.get("occurs1_ctr_unt")),
                    currency=info.get("currency") or r.get("occurs1_cur_cd", "").strip(),
                    exchange=info.get("exchange") or r.get("occurs1_exch_cd", "").strip())
    if not info.get("exchange"):
        info["exchange"] = EXCH_HINT.get(prd, "")
    return info


def resolve_front(api: OverseasClient, prd: str, exch: str) -> tuple[str, str, dict]:
    """후보 월물 중 거래량이 가장 많은 종목을 근월물로."""
    best = ("", "", {}, -1.0)
    if not exch:
        raise SystemExit(f"{prd}: 거래소를 모릅니다. EXCH_HINT 에 추가해 주세요.")
    for exch_cd in dict.fromkeys((f"F{exch}", f"F{exch[:3]}")):
        for sym in candidates(prd, datetime.now()):
            try:
                row = api.price(exch_cd, sym)
            except (ApiFailure, NoResponse) as e:
                log(f"   {exch_cd} {sym}: 없음 ({e})")
                continue
            if not row or num(row.get("occurs1_last_pric")) <= 0:
                log(f"   {exch_cd} {sym}: 시세 없음")
                continue
            vol = num(row.get("occurs1_cum_trd_qty"))
            log(f"   {exch_cd} {sym}: {row.get('occurs1_sym_nm','').strip()} "
                f"현재가 {row.get('occurs1_last_pric')} 누적거래량 {vol:,.0f} 시각 {row.get('occurs1_trd_tm')}")
            if vol > best[3]:
                best = (exch_cd, sym, row, vol)
        if best[0]:
            break
    if not best[0]:
        raise SystemExit(f"{prd}: 시세를 받을 수 있는 월물을 찾지 못했습니다.")
    return best[0], best[1], best[2]


def download_bars(api: OverseasClient, exch_cd: str, sym: str, days: int, out: Path) -> dict:
    today = datetime.now()
    start = (today - timedelta(days=days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    key, pages, seen = "99999999", 0, {}
    while pages < 200:
        body = api.chart_minute(exch_cd, sym, start, end, next_key=key)
        rows = body.get("Occurs1") or []
        pages += 1
        for r in rows:
            k = (str(r.get("occurs1_ref_dt")).strip(), str(r.get("occurs1_exch_tm")).strip())
            seen[k] = r
        key = str(body.get("nxt_key") or "").strip()
        if not rows or not key or set(key) == {"9"}:
            break
    bars = sorted(seen.items())
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ref_dt", "exch_tm", "glb_biz_dt", "open", "high", "low", "close", "volume"])
        for (d, t), r in bars:
            w.writerow([d, t, str(r.get("occurs1_glb_biz_dt")).strip(), r.get("occurs1_open_pric"),
                        r.get("occurs1_high_pric"), r.get("occurs1_low_pric"),
                        r.get("occurs1_close_pric"), r.get("occurs1_cum_trd_qty")])
    biz = sorted({str(r.get("occurs1_glb_biz_dt")).strip() for _, r in bars})
    return {"bars": len(bars), "pages": pages,
            "first": " ".join(bars[0][0]) if bars else "", "last": " ".join(bars[-1][0]) if bars else "",
            "business_days": len(biz), "first_biz_dt": biz[0] if biz else "", "last_biz_dt": biz[-1] if biz else ""}


def wait_fill(api: OverseasClient, ord_no: str, timeout: float = 15.0) -> dict:
    """주문내역에서 그 주문을 찾아 체결을 기다림."""
    t_end = time.time() + timeout
    last: dict = {}
    while time.time() < t_end:
        for r in api.order_history("0"):
            if str(r.get("occurs1_ord_no")).strip().lstrip("0") == ord_no.lstrip("0"):
                last = r
                if str(r.get("occurs1_ord_sts")).strip() == "6" or num(r.get("occurs1_ord_rem_qty")) == 0 and num(r.get("occurs1_exec_qty")) > 0:
                    return r
        time.sleep(0.7)
    return last


def decimals_of(tick: float) -> int:
    t = f"{tick:.10f}".rstrip("0").rstrip(".")
    return len(t.split(".")[1]) if "." in t else 0


def send_order(api: OverseasClient, info: dict, side: str) -> tuple[str, str]:
    """(ord_no, 주문 방식). 시장가를 못 받는 거래소면 체결될 만한 지정가."""
    sym, exch = info["symbol"], info.get("exchange", "")
    if exch not in LIMIT_ONLY_EXCH:
        try:
            return str(api.order_market(sym, side).get("ord_no") or "").strip(), "market"
        except ApiFailure as e:
            if e.code != "7032":
                raise
            log("   시장가 미지원(7032) → 지정가로 대신")
    tick = info.get("tick_size") or 1.0
    row = api.price(info["exch_cd"], sym)
    ref = num(row.get("occurs1_ask_pric")) if side == "buy" else num(row.get("occurs1_bid_pric"))
    ref = ref or num(row.get("occurs1_last_pric"))
    px = ref + LIMIT_SLIP_TICKS * tick if side == "buy" else ref - LIMIT_SLIP_TICKS * tick
    px_s = f"{px:.{decimals_of(tick)}f}"
    return str(api.order_limit(sym, side, px_s).get("ord_no") or "").strip(), f"limit {px_s}"


def one_order(api: OverseasClient, info: dict, side: str, w: csv.writer) -> bool:
    sym, t = info["symbol"], datetime.now()
    try:
        ord_no, how = send_order(api, info, side)
        log(f"   {side} 접수 ord_no={ord_no} ({how})")
    except NoResponse:
        log(f"   {side} 응답 없음 → 재전송하지 않음. 앱에서 주문·잔고를 확인하세요.")
        w.writerow([t.isoformat(), sym, side, "", "NO_RESPONSE", "", ""]); return False
    except ApiFailure as e:
        log(f"   {side} 거부: {e}")
        w.writerow([t.isoformat(), sym, side, "", f"REJECTED {e.code}", "", e.message]); return False
    r = wait_fill(api, ord_no)
    sts, px, qty = str(r.get("occurs1_ord_sts", "")).strip(), r.get("occurs1_exec_pric", ""), r.get("occurs1_exec_qty", "")
    log(f"   {side} 상태 {sts or '?'} 체결 {qty} @ {px}")
    w.writerow([t.isoformat(), sym, side, ord_no, sts, px, qty])
    if sts != "6" and num(r.get("occurs1_ord_rem_qty")) > 0:
        try:
            api.cancel(sym, ord_no, int(num(r.get("occurs1_ord_rem_qty"))), side)
            log("   미체결 남은 수량 취소")
        except (ApiFailure, NoResponse) as e:
            log(f"   취소 실패: {e} — 앱에서 확인하세요")
    return sts == "6"


def flatten(api: OverseasClient, info: dict, w: csv.writer) -> None:
    sym = info["symbol"]
    pos = api.net_position(sym)
    log(f"   현재 잔고 {sym}: {pos}")
    if pos == 0:
        return
    side = "sell" if pos > 0 else "buy"
    for _ in range(abs(pos)):
        one_order(api, info, side, w)
    log(f"   정리 뒤 잔고 {sym}: {api.net_position(sym)}")


def smoke_order(api: OverseasClient, info: dict, w: csv.writer) -> None:
    sym = info["symbol"]
    start_pos = api.net_position(sym)
    log(f"   시작 잔고 {sym}: {start_pos}")
    if start_pos != 0:
        log("   잔고가 0 이 아니라 왕복 시험을 건너뜀 → 먼저 --flatten 으로 정리하세요"); return
    if not one_order(api, info, "buy", w):
        log("   매수 체결 확인 실패 → 왕복 중단"); return
    one_order(api, info, "sell", w)
    end_pos = api.net_position(sym)
    log(f"   왕복 뒤 잔고 {sym}: {end_pos}  {'OK' if end_pos == start_pos else '불일치! 앱에서 확인'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="paper", choices=["paper", "real"])
    ap.add_argument("--products", default="SFC,MHI")
    ap.add_argument("--history-days", type=int, default=45)
    ap.add_argument("--smoke-order", action="store_true")
    ap.add_argument("--flatten", action="store_true", help="보유 잔고를 0 으로 정리")
    ap.add_argument("--skip-history", action="store_true", help="과거 분봉 내려받기 생략")
    ap.add_argument("--base-url", default=None, help="시험용(가짜 서버)")
    a = ap.parse_args()
    if (a.smoke_order or a.flatten) and a.env != "paper":
        raise SystemExit("주문 시험은 모의(--env paper)에서만 합니다.")

    out = Path("results") / f"check_{datetime.now():%Y%m%d_%H%M%S}"
    key, secret = keys_from_env()
    api = OverseasClient(a.env, key, secret, out, base_url=a.base_url)
    meta = {"started": datetime.now().isoformat(), "env": a.env, "products": {}}
    orders_f = open(out / "orders.csv", "w", newline="", encoding="utf-8")
    ow = csv.writer(orders_f)
    ow.writerow(["ts", "sym", "side", "ord_no", "status", "exec_price", "exec_qty"])

    for prd in [p.strip() for p in a.products.split(",") if p.strip()]:
        log(f"[{prd}] 장운영정보·품목세부정보")
        info = product_info(api, prd)
        log(f"   {info}")
        log(f"[{prd}] 근월물 찾기")
        exch_cd, sym, row = resolve_front(api, prd, info.get("exchange", ""))
        info.update(exch_cd=exch_cd, symbol=sym, symbol_name=row.get("occurs1_sym_nm", "").strip(),
                    last_price=row.get("occurs1_last_pric"), price_time=row.get("occurs1_trd_tm"),
                    checked_at=datetime.now().isoformat())
        log(f"   → {exch_cd} {sym}")
        if not a.skip_history:
            log(f"[{prd}] 과거 1분봉 ({a.history_days}일 창)")
            h = download_bars(api, exch_cd, sym, a.history_days, out / f"bars_{prd}_{sym}.csv")
            info["history"] = h
            log(f"   {h}")
        if a.flatten:
            log(f"[{prd}] 잔고 정리")
            flatten(api, info, ow)
            orders_f.flush()
        if a.smoke_order:
            log(f"[{prd}] 주문 왕복 시험 (1계약)")
            smoke_order(api, info, ow)
            orders_f.flush()
        meta["products"][prd] = info

    meta["finished"] = datetime.now().isoformat()
    (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"끝. 기록: {out}")


if __name__ == "__main__":
    main()
