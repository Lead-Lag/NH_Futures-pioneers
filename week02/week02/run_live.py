"""실시간 실행: 미국달러 선물 볼린저 밴드 평균회귀.

예)
  # 모의계좌 주문까지 (모의 APP Key)
  python run_live.py --mode paper --symbol A756B000
  # 모의계좌 주문 왕복만 먼저 확인 (매수 1 → 매도 1)
  python run_live.py --mode paper --smoke-order --run-minutes 1
  # 프로그램 안 시뮬레이션 (실전 APP Key 로 시세만, 주문 없음)
  python run_live.py --mode sim --env real

흐름: 인증 → 종목·품목 확인 → (모의) 잔고 확인 → 오늘 분봉으로 지표 준비
     → [반복] 현재가 조회(손절 판정) · 봉 마감 시 분봉 조회(신호 판정) · 마감 시각 확인
     → 주문 → 체결 확인 → 잔고 대조 → 종료 시 남은 포지션 정리 → 요약 작성
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from broker import PaperBroker, SimBroker
from engine import BUY, SELL, Engine, Intent, Params, params_dict
from journal import RunJournal
from nh_client import NHApiError, NHClient, load_credentials, num

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
GRACE_SEC = 2.0  # 봉 마감 뒤 이만큼 기다렸다가 분봉을 조회(서버 반영 시간)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["paper", "sim"], required=True, help="paper=모의계좌 주문, sim=프로그램 안 체결")
    ap.add_argument("--env", choices=["paper", "real"], help="접속 환경(기본: paper 모드→paper, sim 모드→real)")
    ap.add_argument("--symbol", default="A756B000", help="종목코드 (기본 A756B000 = 미국달러 2026년 11월물)")
    ap.add_argument("--config", default=str(HERE / "config.local.json"))
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--poll", type=float, default=2.0, help="현재가 조회 간격(초)")
    ap.add_argument("--timeout", type=float, default=10.0, help="API 응답을 기다리는 최대 시간(초)")
    ap.add_argument("--until", default="15:40", help="이 시각에 종료(KST)")
    ap.add_argument("--run-minutes", type=float, default=0, help="0 이 아니면 이 시간만 돌고 종료(시험용)")
    ap.add_argument("--smoke-order", action="store_true", help="시작 직후 매수1→매도1 주문 왕복으로 API 확인(paper 모드)")
    ap.add_argument("--adopt", action="store_true", help="시작 시 이미 있는 잔고를 무시하지 않고 진행")
    ap.add_argument("--results", default=str(HERE / "results"))
    args = ap.parse_args()

    env = args.env or ("paper" if args.mode == "paper" else "real")
    if args.mode == "paper" and env != "paper":
        print("paper 모드는 모의 환경(--env paper)에서만 주문합니다. 실전 계좌 주문은 하지 않습니다.")
        return 2

    params = Params.from_dict(json.loads(Path(args.strategy).read_text(encoding="utf-8")))
    started = datetime.now(KST)
    run_id = f"run_{args.mode}_{started:%Y%m%d_%H%M%S}"
    j = RunJournal(Path(args.results) / run_id)
    creds = load_credentials(args.config, env)
    client = NHClient(creds, token_cache=HERE / ".token_cache.json", call_logger=j.api_call, timeout=args.timeout)
    broker = (PaperBroker if args.mode == "paper" else SimBroker)(client, args.symbol, j, params.tick_size)
    eng = Engine(params, j)

    meta = {
        "run_id": run_id,
        "mode": args.mode,
        "mode_label": "모의계좌 주문" if args.mode == "paper" else "프로그램 안 시뮬레이션(주문 없음)",
        "environment": env,
        "base_url": creds.url,
        "symbol": args.symbol,
        "strategy": "볼린저 밴드 이탈 뒤 복귀 (1분봉)",
        "params": params_dict(params),
        "started_at": started.isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
    }
    j.meta(meta)
    j.event("state", f"실행 시작 {run_id} · {meta['mode_label']} · {env} · {args.symbol}")

    try:
        _check_instrument(client, args.symbol, params, j, meta)
        if args.mode == "paper":
            pos = client.position_of(args.symbol)
            j.event("position", f"시작 잔고 {pos}계약")
            if pos != 0 and not args.adopt:
                j.event("state", "이미 잔고가 있어 시작하지 않습니다(--adopt 로 무시 가능)")
                return 3
        if args.smoke_order:
            _smoke(broker, client, args.symbol, j, args.mode)

        last_start = _warmup(client, eng, args.symbol, params, j)
        eng.warmup = False
        j.event("state", "감시 시작")
        _loop(args, client, broker, eng, params, j, last_start, started)
    except KeyboardInterrupt:
        j.event("state", "사용자 중지(Ctrl+C)")
    except NHApiError as exc:
        j.event("api_error", f"치명적 API 오류로 종료: {exc}")
    finally:
        _shutdown(args, client, broker, eng, j)
        meta["ended_at"] = datetime.now(KST).isoformat(timespec="seconds")
        meta["final_engine_position"] = eng.position
        j.meta(meta)
        j.event("state", "실행 종료")
        j.close()
        try:
            from summarize import summarize_run

            out = summarize_run(j.folder)
            print(f"\n요약: {out}")
        except Exception as exc:  # 요약 실패가 실행 기록을 해치지 않도록
            print(f"요약 작성 실패: {exc}")
    return 0


def _check_instrument(client: NHClient, sym: str, p: Params, j: RunJournal, meta: dict) -> None:
    q = client.price(sym)
    info = {
        "sym_nm": q.get("sym_nm"),
        "last_pric": q.get("last_pric"),
        "lst_trd_dt": q.get("lst_trd_dt"),
        "rem_dy": q.get("rem_dy"),
        "mltplr": q.get("mltplr"),
    }
    meta["instrument"] = info
    j.event("instrument", f"{sym} {info['sym_nm']} 현재가 {info['last_pric']} 최종거래일 {info['lst_trd_dt']} 잔존 {info['rem_dy']}일")
    if "달러" not in str(info["sym_nm"] or ""):
        j.event("warning", f"종목명이 미국달러 선물이 아닙니다: {info['sym_nm']}")
    try:
        for r in client.product_detail().get("Occurs1") or []:
            if str(r.get("occurs1_dtl_cd_ko_nm", "")).strip() == "미국달러선물(USD)":
                tick, tval = num(r.get("occurs1_cd_val_inf_1")), num(r.get("occurs1_tick_val"))
                meta["product_detail"] = {
                    "tick_size": tick, "tick_value": tval, "hours": r.get("occurs1_cd_val_inf_4"),
                    "closing_auction": r.get("occurs1_cd_val_inf_3"), "init_margin_per_contract": r.get("occurs1_ctr_init_mrgn"),
                    "expiry": r.get("occurs1_exp_dt"),
                }
                j.event("instrument", f"품목 세부: 틱 {tick} · 틱가치 {tval:,.0f}원 · 거래시간 {r.get('occurs1_cd_val_inf_4')} · "
                                      f"계약당 증거금 {r.get('occurs1_ctr_init_mrgn')}")
                if abs(tick - p.tick_size) > 1e-9 or abs(tval - p.tick_value) > 1e-6:
                    j.event("warning", f"설정 틱({p.tick_size}/{p.tick_value})과 품목 세부({tick}/{tval})가 다릅니다")
    except NHApiError as exc:
        j.event("api_error", f"품목별세부정보 조회 실패(계속 진행): {exc}")


def _warmup(client: NHClient, eng: Engine, sym: str, p: Params, j: RunJournal) -> datetime | None:
    now = datetime.now(KST)
    bars = client.minute_bars(sym, now.strftime("%Y%m%d"), minutes=p.bar_minutes)
    done = [b for b in bars if b["start"] + timedelta(minutes=p.bar_minutes) <= now - timedelta(seconds=GRACE_SEC)]
    for b in done:
        eng.on_bar_close(b)
    j.event("ready", f"준비 봉 {len(done)}개 (받은 봉 {len(bars)}, 지표에 필요한 봉 {p.bb_period})")
    return done[-1]["start"] if done else None


def _loop(args, client, broker, eng: Engine, p: Params, j: RunJournal, last_start, started) -> None:
    until = datetime.combine(started.date(), datetime.strptime(args.until, "%H:%M").time(), KST)
    stop_at = started + timedelta(minutes=args.run_minutes) if args.run_minutes else until
    bar = timedelta(minutes=p.bar_minutes)
    due = (last_start + 2 * bar if last_start else _next_boundary(datetime.now(KST), p.bar_minutes)) + timedelta(seconds=GRACE_SEC)
    errors = 0
    last_loop = datetime.now(KST)

    while True:
        now = datetime.now(KST)
        if now - last_loop > timedelta(seconds=60):
            # 10/29: PC 가 09:27~13:13 멈춘 뒤 깨어나며 밀린 봉으로 18초 동안 7번 사고팜 → 공백을 알리고 아래에서 밀린 봉은 거래에 쓰지 않음
            j.event("pause", f"실행이 {(now - last_loop).total_seconds() / 60:.1f}분 멈췄다가 다시 돎 "
                             f"({last_loop:%H:%M:%S}→{now:%H:%M:%S}, PC 절전·네트워크 끊김 추정)")
        last_loop = now
        if now >= stop_at or now >= until:
            j.event("state", "종료 시각 도달")
            return
        try:
            q = client.price(args.symbol)
            last = num(q.get("last_pric"))
            _do(broker, client, eng, eng.on_price(last, now), args, j)

            if now >= due:
                rows = client.chart_minute(args.symbol, now.strftime("%Y%m%d"), minutes=p.bar_minutes).get("Occurs1") or []
                from nh_client import parse_bar

                bars = sorted((b for b in (parse_bar(r, now.strftime("%Y%m%d")) for r in rows) if b), key=lambda b: b["start"])
                fresh = [b for b in bars if (last_start is None or b["start"] > last_start) and b["start"] + bar <= now - timedelta(seconds=1)]
                # 마감된 지 「한 봉 + 30초」가 넘은 봉은 밀린 봉입니다. 그 봉의 신호는 이미 지난 가격이라
                # 지금 주문하면 규칙과 다른 거래가 됩니다 → 지표만 갱신하고 주문하지 않습니다.
                stale = [b for b in fresh if b["start"] + 2 * bar < now - timedelta(seconds=30)]
                if stale:
                    j.event("gap", f"밀린 봉 {len(stale)}개({stale[0]['start']:%H:%M}~{stale[-1]['start']:%H:%M})는 "
                                   "지표만 갱신하고 주문하지 않음")
                for b in fresh:
                    if last_start and b["start"] - last_start > bar:
                        j.event("gap", f"봉 공백: {last_start:%H:%M} 다음이 {b['start']:%H:%M}")
                    last_start = b["start"]
                    if b in stale:
                        eng.warmup = True
                        eng.on_bar_close(b)
                        eng.warmup = False
                        continue
                    _do(broker, client, eng, eng.on_bar_close(b), args, j)
                if fresh:
                    due = last_start + 2 * bar + timedelta(seconds=GRACE_SEC)
            _do(broker, client, eng, eng.on_clock(now), args, j)
            errors = 0
        except NHApiError as exc:
            errors += 1
            j.event("api_error", f"{exc} (연속 {errors}회)")
            if errors >= 20:
                j.event("state", "API 오류 연속 20회 — 실행 중지")
                return
            time.sleep(min(30, 2 * errors))
        time.sleep(args.poll)


def _do(broker, client: NHClient, eng: Engine, intent: Intent | None, args, j: RunJournal) -> None:
    if intent is None:
        return
    j.event("intent", f"{intent.purpose} {intent.side} {intent.qty}계약 — {intent.reason}")
    res = broker.execute(intent)
    if res.ok and res.filled_qty > 0:
        j.event("fill", f"{intent.purpose} {res.filled_qty}계약 @ {res.avg_price} ({broker.source}, ord {res.ord_no})")
        eng.on_fill(intent, res.avg_price, res.filled_qty, res.ts or datetime.now(KST))
        if args.mode == "paper":
            try:
                eng.sync_position(client.position_of(args.symbol), datetime.now(KST))
            except NHApiError as exc:
                j.event("api_error", f"잔고 대조 실패: {exc}")
    else:
        eng.on_order_failed(intent, f"{res.status} {res.message}")


def _smoke(broker, client: NHClient, sym: str, j: RunJournal, mode: str) -> None:
    j.event("smoke", "주문 왕복 확인 시작: 매수 1 → 매도 1 (시장가)")
    now = datetime.now(KST)
    for side in (BUY, SELL):
        it = Intent(-1 if side == BUY else -2, now, side, 1, "SMOKE_TEST", "주문 API 왕복 확인", 0.0)
        res = broker.execute(it)
        j.event("smoke", f"{side} → {res.status} {res.filled_qty}계약 @ {res.avg_price} {res.message}")
        if not res.ok:
            break
    if mode == "paper":
        j.event("smoke", f"왕복 뒤 잔고 {client.position_of(sym)}계약")


def _shutdown(args, client, broker, eng: Engine, j: RunJournal) -> None:
    for attempt in range(3):
        it = eng.force_exit(datetime.now(KST), "실행 종료 시 정리")
        if it is None:
            break
        _do(broker, client, eng, it, args, j)
        time.sleep(1)
    if args.mode == "paper":
        try:
            pos = client.position_of(args.symbol)
            j.event("position", f"종료 잔고 {pos}계약")
            if pos != 0:
                j.event("warning", "종료 시 잔고가 남아 있습니다 — HTS/앱에서 직접 확인하세요")
        except NHApiError as exc:
            j.event("api_error", f"종료 잔고 조회 실패: {exc}")


def _next_boundary(now: datetime, minutes: int) -> datetime:
    base = now.replace(second=0, microsecond=0)
    return base + timedelta(minutes=minutes - (base.minute % minutes))


if __name__ == "__main__":
    raise SystemExit(main())
