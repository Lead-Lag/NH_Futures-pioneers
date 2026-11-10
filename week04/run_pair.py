"""실시간 실행: 삼성전자·SK하이닉스 주식선물 페어(스프레드) 평균회귀.

예)
  # 페어 주문 왕복만 먼저 확인 (삼성 1·하이닉스 1 진입 → 청산 → 잔고 0)
  python run_pair.py --mode paper --smoke-order --run-minutes 1
  # 본 실행 (모의계좌 주문, 국내 모의 APP Key) — 09:00 전에 켜 두면 15:40 에 스스로 끝남
  python run_pair.py --mode paper
  # 프로그램 안 시뮬레이션 (실전 APP Key 로 시세만, 주문 없음)
  python run_pair.py --mode sim --env real

  # WebSocket 없이 REST 조회만으로 (비교·비상용)
  python run_pair.py --mode paper --no-ws

흐름 (시세는 처음에만 REST, 이후 WebSocket — 운영진 안내)
  [REST]  인증 → 두 종목 확인 → (모의) 잔고 0·주문가능수량 → 오늘 분봉으로 지표 준비
  [WS]    실시간 연결: 시세체결(KA) 두 종목 + 주문/체결 통보(O1)
  [반복]  실시간 체결가로 금액 손절 판정(0.2초마다) · 실시간 체결을 1분봉으로 묶어 신호 판정
          → 페어 주문(REST 주문 API) → 체결 확인(O1 통보 우선, 늦으면 주문내역 조회) → 두 종목 잔고 대조(REST)
  [대체]  실시간이 끊기면: 현재가는 REST 로 --poll 초마다, 끊긴 동안의 분봉은 REST 분봉으로 보충, 자동 재연결
  종료 시 남은 포지션 정리 → 요약
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from journal import RunJournal, summarize_run
from nh_client import NHApiError, NHClient, load_credentials, num
from pair_broker import PaperPairExecutor, SimPairExecutor
from ws_feed import RealtimeFeed
from pair_engine import BROKEN, BUY, SELL, Leg, PairEngine, PairIntent, PairParams, align_minutes, params_dict

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
GRACE_SEC = 2.0     # 봉 마감 뒤 이만큼 기다렸다가 분봉 조회. 이 뒤에도 봉이 없으면 「그 분 체결 없음」으로 봄
BAR = timedelta(minutes=1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["paper", "sim"], required=True)
    ap.add_argument("--env", choices=["paper", "real"])
    ap.add_argument("--config", default=str(HERE / "config.local.json"))
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--poll", type=float, default=5.0, help="실시간이 끊겼을 때 REST 현재가 대체 조회 간격(초)")
    ap.add_argument("--no-ws", action="store_true", help="WebSocket 을 쓰지 않고 REST 조회만으로 실행(비교·비상용)")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--leg-timeout", type=float, default=8.0, help="다리 하나의 체결을 기다리는 최대 시간(초)")
    ap.add_argument("--until", default="15:40")
    ap.add_argument("--run-minutes", type=float, default=0)
    ap.add_argument("--smoke-order", action="store_true", help="시작 직후 페어 1:1 진입→청산으로 주문 API 확인")
    ap.add_argument("--adopt", action="store_true", help="시작 잔고가 있어도 진행(기본은 시작 안 함)")
    ap.add_argument("--results", default=str(HERE / "results"))
    args = ap.parse_args()

    env = args.env or ("paper" if args.mode == "paper" else "real")
    if args.mode == "paper" and env != "paper":
        print("paper 모드는 모의 환경(--env paper)에서만 주문합니다.")
        return 2

    cfg = json.loads(Path(args.strategy).read_text(encoding="utf-8"))
    p = PairParams.from_dict(cfg)
    syms = {"S": cfg["sym_s"], "H": cfg["sym_h"]}
    names = {"S": cfg.get("name_s", "삼성전자"), "H": cfg.get("name_h", "SK하이닉스")}
    started = datetime.now(KST)
    run_id = f"pair_{args.mode}_{started:%Y%m%d_%H%M%S}"
    j = RunJournal(Path(args.results) / run_id)
    creds = load_credentials(args.config, env)
    client = NHClient(creds, token_cache=HERE / ".token_cache.json", call_logger=j.api_call, timeout=args.timeout)
    Exec = PaperPairExecutor if args.mode == "paper" else SimPairExecutor
    ex = Exec(client, syms, j, {"S": p.tick_s, "H": p.tick_h}, {"S": p.mult_s, "H": p.mult_h},
              leg_timeout=args.leg_timeout)
    eng = PairEngine(p, j)
    feed = None

    meta = {
        "run_id": run_id, "mode": args.mode,
        "mode_label": "모의계좌 주문" if args.mode == "paper" else "프로그램 안 시뮬레이션(주문 없음)",
        "environment": env, "base_url": creds.url, "symbols": syms,
        "strategy": "삼성전자·SK하이닉스 주식선물 페어 평균회귀 (1분봉, rolling z)",
        "params": params_dict(p), "leg_timeout": args.leg_timeout, "poll": args.poll,
        "websocket": {"enabled": not args.no_ws, "connects": 0, "gaps": 0, "bars_ws": 0, "bars_rest": 0,
                      "rest_quote_fallbacks": 0},
        "started_at": started.isoformat(timespec="seconds"), "python": sys.version.split()[0],
    }
    j.meta(meta)
    j.event("state", f"실행 시작 {run_id} · {meta['mode_label']} · {env} · 삼성 {syms['S']} · 하이닉스 {syms['H']}")

    try:
        _check(client, syms, names, p, j, meta, args.mode)
        if args.mode == "paper":
            pos = _positions(client, syms)
            j.event("position", f"시작 잔고 {pos}")
            if any(pos.values()) and not args.adopt:
                j.event("state", "이미 잔고가 있어 시작하지 않습니다(--adopt 로 무시 가능)")
                return 3
        if not args.no_ws:
            feed = RealtimeFeed(client, syms, j, orders=args.mode == "paper")
            if feed.start():
                j.event("state", "실시간 연결 완료 — 이후 시세는 WebSocket, REST 는 빠진 분 보충·비상 대체에만 사용")
            else:
                j.event("warning", "실시간 연결을 10초 안에 열지 못함 — 연결될 때까지 REST 로 대체하며 계속 시도")
            if isinstance(ex, PaperPairExecutor):
                ex.feed = feed
        if args.smoke_order:
            _smoke(ex, client, syms, p, j, args.mode)
        last_start = _warmup(client, eng, syms, j)
        eng.warmup = False
        j.event("state", "감시 시작")
        _loop(args, client, ex, eng, syms, j, last_start, started, feed, meta)
    except KeyboardInterrupt:
        j.event("state", "사용자 중지(Ctrl+C)")
    except NHApiError as exc:
        j.event("api_error", f"치명적 API 오류로 종료: {exc}")
    finally:
        _shutdown(args, client, ex, eng, syms, j)
        if feed is not None:
            feed.stop()
            offs = sorted(feed.clock_offsets)
            meta["websocket"].update({"connects": feed.connects, "gaps": sum(1 for g in feed.gaps if g[1] is not None), "ticks": feed.ticks,
                                      "clock_offset_median": round(offs[len(offs) // 2], 3) if offs else None})
        meta["ended_at"] = datetime.now(KST).isoformat(timespec="seconds")
        meta["final_engine_direction"] = eng.direction
        j.meta(meta)
        j.event("state", "실행 종료")
        j.close()
        try:
            print(f"\n요약: {summarize_run(j.folder)}")
        except Exception as exc:
            print(f"요약 작성 실패: {exc}")
    return 0


# ---------------------------------------------------------------- 시작
def _check(client: NHClient, syms, names, p: PairParams, j: RunJournal, meta: dict, mode: str) -> None:
    meta["instruments"] = {}
    for k, sym in syms.items():
        q = client.price(sym)
        info = {f: q.get(f) for f in ("sym_nm", "last_pric", "ask_pric", "bid_pric", "lst_trd_dt", "rem_dy", "mltplr")}
        meta["instruments"][k] = info
        j.event("instrument", f"{k} {sym} {info['sym_nm']} 현재가 {info['last_pric']} "
                              f"호가 {info['bid_pric']}/{info['ask_pric']} 최종거래일 {info['lst_trd_dt']} 승수 {info['mltplr']}")
        if names[k].replace(" ", "") not in str(info["sym_nm"] or "").replace(" ", ""):
            j.event("warning", f"종목명이 {names[k]} 이(가) 아닙니다: {info['sym_nm']}")
        mult = num(info["mltplr"])
        cfg_mult = p.mult_s if k == "S" else p.mult_h
        if mult and abs(mult - cfg_mult) > 1e-9:
            j.event("warning", f"{k} 승수 설정 {cfg_mult} ≠ 조회 {mult}")
        if mode == "paper":
            for side in (BUY, SELL):
                try:
                    body = client.orderable_quantity(sym, side=side, price=str(info["last_pric"] or "0"))
                    keep = {kk: v for kk, v in body.items() if "qty" in kk or "amt" in kk or "mrgn" in kk}
                    meta.setdefault("orderable", {})[f"{k}_{side}"] = keep or body
                    j.event("orderable", f"{k} {side} 주문가능: {json.dumps(keep or body, ensure_ascii=False)[:300]}")
                except NHApiError as exc:
                    j.event("api_error", f"{k} 주문가능수량 조회 실패(계속 진행): {exc}")


def _positions(client: NHClient, syms) -> dict[str, int]:
    return {k: client.position_of(sym) for k, sym in syms.items()}


def _smoke(ex, client, syms, p: PairParams, j: RunJournal, mode: str) -> None:
    j.event("smoke", "페어 주문 왕복 확인: 스프레드 매수(삼성 매수 1 · 하이닉스 매도 1) → 청산")
    now = datetime.now(KST)
    q = {k: num(client.price(s).get("last_pric")) for k, s in syms.items()}
    enter = PairIntent(-1, now, "ENTRY_LONG_SPREAD", +1, "주문 API 왕복 확인",
                       [Leg("H", SELL, 1), Leg("S", BUY, 1)], q)
    out = ex.execute(enter)
    j.event("smoke", f"진입 → {out.status} {out.message}")
    if out.status == "COMPLETE":
        leave = PairIntent(-2, datetime.now(KST), "EXIT_MANUAL", +1, "주문 API 왕복 확인",
                           [Leg("H", BUY, out.qty["H"]), Leg("S", SELL, out.qty["S"])], q)
        out2 = ex.execute(leave)
        j.event("smoke", f"청산 → {out2.status} {out2.message}")
    if mode == "paper":
        pos = _positions(client, syms)
        j.event("smoke", f"왕복 뒤 잔고 {pos}")
        if any(pos.values()):
            ex.flatten(pos, "왕복 확인 뒤 남은 잔고")


_pair_bars = align_minutes
_last_close: dict[str, float] = {}


def _warmup(client: NHClient, eng: PairEngine, syms, j: RunJournal) -> datetime | None:
    now = datetime.now(KST)
    day = now.strftime("%Y%m%d")
    bs, bh = client.minute_bars(syms["S"], day), client.minute_bars(syms["H"], day)
    rows = _pair_bars(bs, bh, None, now - timedelta(seconds=GRACE_SEC), _last_close)
    for r in rows:
        eng.on_bar_close(r)
    j.event("ready", f"준비 봉 {len(rows)}개 (삼성 {len(bs)} · 하이닉스 {len(bh)}, 지표에 필요한 봉 {eng.p.window})")
    return rows[-1]["start"] if rows else None


# ---------------------------------------------------------------- 반복
def _loop(args, client, ex, eng: PairEngine, syms, j: RunJournal, last_start, started, feed, meta) -> None:
    """반복. 실시간(WebSocket)이 살아 있으면 시세·분봉 모두 실시간 값을 쓰고 REST 시세 조회는 하지 않습니다.

    - 현재가(금액 손절·주문 직전 z): 실시간 최신 체결가. 끊긴 동안만 REST 현재가를 --poll 초마다 대체 조회
    - 1분봉(신호 판정): 실시간 체결을 분으로 묶은 봉. 연결 전·끊긴 동안에 걸친 분만 REST 분봉으로 보충
    """
    until = datetime.combine(started.date(), datetime.strptime(args.until, "%H:%M").time(), KST)
    stop_at = started + timedelta(minutes=args.run_minutes) if args.run_minutes else until
    due = _next_boundary(datetime.now(KST)) + timedelta(seconds=GRACE_SEC)
    errors = 0
    last_loop = datetime.now(KST)
    next_rest_quote = datetime.now(KST)
    was_live = feed.healthy() if feed else False
    ws_meta = meta["websocket"]
    while True:
        now = datetime.now(KST)
        if now - last_loop > timedelta(seconds=60):
            j.event("pause", f"실행이 {(now - last_loop).total_seconds() / 60:.1f}분 멈췄다가 다시 돎 "
                             f"({last_loop:%H:%M:%S}→{now:%H:%M:%S})")
        last_loop = now
        if now >= stop_at or now >= until:
            j.event("state", "종료 시각 도달")
            return
        live = bool(feed and feed.healthy())
        if feed and live != was_live:
            j.event("ws", "실시간 시세 사용 재개" if live else "실시간 끊김 — REST 현재가로 대체")
            was_live = live
        try:
            # ---- 현재가: 실시간 우선
            ps = ph = 0.0
            if live:
                qs, qh = feed.quote("S"), feed.quote("H")
                ps, ph = (qs or {}).get("last", 0.0), (qh or {}).get("last", 0.0)
            elif now >= next_rest_quote:
                ps = num(client.price(syms["S"]).get("last_pric"))
                ph = num(client.price(syms["H"]).get("last_pric"))
                next_rest_quote = now + timedelta(seconds=args.poll)
                ws_meta["rest_quote_fallbacks"] += 1
            if ps > 0 and ph > 0:
                _do(ex, client, eng, eng.on_prices(ps, ph, now), syms, args, j, feed)

            # ---- 봉 마감 판정
            if now >= due:
                upto = now - timedelta(seconds=1)
                rows = _collect_bars(client, feed, syms, last_start, upto, now, ws_meta, j)
                # 마감된 지 「한 봉 + 30초」 넘은 봉 = 밀린 봉 → 지표만 갱신, 주문 안 함 (2주차 10/29 교훈)
                stale = [r for r in rows if r["start"] + 2 * BAR < now - timedelta(seconds=30)]
                if stale:
                    j.event("gap", f"밀린 봉 {len(stale)}개({stale[0]['start']:%H:%M}~{stale[-1]['start']:%H:%M})는 "
                                   "지표만 갱신하고 주문하지 않음")
                for r in rows:
                    last_start = r["start"]
                    if r in stale:
                        eng.warmup = True
                        eng.on_bar_close(r)
                        eng.warmup = False
                        continue
                    _do(ex, client, eng, eng.on_bar_close(r), syms, args, j, feed)
                due = _next_boundary(now) + timedelta(seconds=GRACE_SEC + _late_by(feed))
            _do(ex, client, eng, eng.on_clock(now), syms, args, j, feed)
            errors = 0
        except NHApiError as exc:
            errors += 1
            j.event("api_error", f"{exc} (연속 {errors}회)")
            if errors >= 20:
                j.event("state", "API 오류 연속 20회 — 실행 중지")
                return
            time.sleep(min(30, 2 * errors))
        # 실시간이면 0.2초마다 확인(금액 손절이 즉시 반응). 아니면 다음 판정 시각이나 --poll 중 가까운 쪽까지
        wait = (due - datetime.now(KST)).total_seconds()
        if live:
            time.sleep(0.2)
        else:
            time.sleep(max(0.2, min(args.poll, wait)) if wait > 0 else 0.2)


def _late_by(feed) -> float:
    """PC 시계가 거래소보다 빠르면 그만큼 더 기다렸다가 봉을 닫음(그 분 체결이 아직 올 수 있으므로). 최대 3초."""
    if not feed or not feed.clock_offsets:
        return 0.0
    offs = sorted(feed.clock_offsets[-300:])
    return min(3.0, max(0.0, offs[len(offs) // 2]))


def _collect_bars(client, feed, syms, last_start, upto, now, ws_meta, j) -> list[dict]:
    """last_start 다음 분부터 끝난 분까지의 페어 봉. 실시간으로 온전히 받은 분은 실시간 봉, 아니면 REST 분봉."""
    one = BAR
    t = (last_start + one) if last_start else None
    need_rest = feed is None or t is None
    minutes = []
    while t is not None and t + one <= upto:
        minutes.append(t)
        if feed is None or not feed.minute_complete(t):
            need_rest = True
        t += one
    if not minutes and t is not None:
        return []
    bars = {"S": {}, "H": {}}
    rest_used = set()
    if need_rest:
        day = now.strftime("%Y%m%d")
        since = (last_start - timedelta(minutes=2)).strftime("%H%M%S") if last_start else ""
        for k in ("S", "H"):
            for b in client.minute_bars(syms[k], day, since=since):
                if feed is None or last_start is None or not feed.minute_complete(b["start"]):
                    bars[k][b["start"]] = b
                    rest_used.add(b["start"])
        if feed is not None and minutes:
            j.event("rest_fill", f"실시간으로 온전히 받지 못한 분을 REST 분봉으로 보충 "
                                 f"({minutes[0]:%H:%M}~{minutes[-1]:%H:%M} 중 {sum(1 for m in minutes if not feed.minute_complete(m))}개)",
                    quiet=True)
    if feed is not None:
        for k in ("S", "H"):
            for b in feed.bars_of(k):
                if (last_start is None or b["start"] > last_start) and feed.minute_complete(b["start"]):
                    bars[k][b["start"]] = b
    rows = _pair_bars(list(bars["S"].values()), list(bars["H"].values()), last_start, upto, _last_close)
    for r in rows:
        if feed is not None and feed.minute_complete(r["start"]):
            ws_meta["bars_ws"] += 1
        else:
            ws_meta["bars_rest"] += 1
    return rows


def _do(ex, client, eng: PairEngine, intent: PairIntent | None, syms, args, j: RunJournal, feed=None) -> None:
    if intent is None:
        return
    legs = " → ".join(f"{l.name} {l.side} {l.qty}" for l in intent.legs)
    j.event("intent", f"페어 {intent.id} {intent.purpose} [{legs}] — {intent.reason}")
    if intent.is_entry and eng.last_mu is not None and eng.last_sd:
        # 기록만: 주문 직전 현재가로 다시 계산한 z (판정 봉 종가와 차이 = 신호가 주문 전에 얼마나 사라졌나)
        try:
            qs, qh = (feed.quote("S"), feed.quote("H")) if feed else (None, None)
            if qs and qh:
                ps, ph = qs["last"], qh["last"]
            else:
                ps = num(client.price(syms["S"]).get("last_pric"))
                ph = num(client.price(syms["H"]).get("last_pric"))
            z_now = (eng.spread(ps, ph) - eng.last_mu) / eng.last_sd
            intent.snapshot["z_at_send"] = round(z_now, 3)
            j.event("presend", f"페어 {intent.id} 주문 직전 현재가 삼성 {ps:,.0f} · 하이닉스 {ph:,.0f} → z {z_now:.2f} "
                               f"(판정 z {intent.snapshot.get('z')})")
        except NHApiError:
            pass
    out = ex.execute(intent)
    eng.on_pair_done(intent, out, datetime.now(KST))
    if args.mode != "paper":
        return
    _reconcile(ex, client, eng, syms, j, broken=out.status == BROKEN)


def _reconcile(ex, client, eng: PairEngine, syms, j: RunJournal, broken: bool = False) -> None:
    """두 종목 잔고를 엔진과 대조. 어긋나면 잔고 기준으로 정리하고 엔진을 무포지션으로 맞춤."""
    try:
        pos = _positions(client, syms)
    except NHApiError as exc:
        j.event("api_error", f"잔고 대조 실패: {exc}")
        return
    if eng.sync_positions(pos, datetime.now(KST)) and not broken:
        return
    j.event("risk", f"잔고 {pos} 기준으로 남은 노출 정리")
    if any(pos.values()):
        ex.flatten(pos, "잔고 불일치·레그 불일치")
        time.sleep(1.0)
        pos = _positions(client, syms)
    eng.mark_flat()
    j.event("position", f"정리 뒤 잔고 {pos} — 엔진 무포지션으로 맞춤, 신규 진입은 계속 중지({eng.halted})")


def _shutdown(args, client, ex, eng: PairEngine, syms, j: RunJournal) -> None:
    for _ in range(2):
        it = eng.force_exit(datetime.now(KST), "실행 종료 시 정리")
        if it is None:
            break
        _do(ex, client, eng, it, syms, args, j)
        time.sleep(1)
    if args.mode == "paper":
        try:
            pos = _positions(client, syms)
            j.event("position", f"종료 잔고 {pos}")
            if any(pos.values()):
                ex.flatten(pos, "종료 시 남은 잔고")
                pos = _positions(client, syms)
                j.event("position", f"정리 뒤 종료 잔고 {pos}")
                if any(pos.values()):
                    j.event("warning", "종료 시 잔고가 남아 있습니다 — 앱에서 직접 확인하세요")
        except NHApiError as exc:
            j.event("api_error", f"종료 잔고 조회 실패: {exc}")


def _next_boundary(now: datetime) -> datetime:
    return now.replace(second=0, microsecond=0) + BAR


if __name__ == "__main__":
    raise SystemExit(main())
