"""백테스트: 실시간 실행과 **같은 Engine** 으로 지난 분봉을 돌립니다.

예)
  python backtest.py --days 5 --env real            # API 로 최근 5거래일 1분봉을 받아 실행
  python backtest.py --bars-csv results/backtest_xxx/bars_input.csv   # 저장한 봉으로 재현
  python backtest.py --synthetic                    # 코드 점검용 가짜 데이터(결과로 쓰지 않음)

체결 가정 (bar-model)
  - 봉 마감에서 난 신호·청산은 **다음 봉 시가**에 체결, 불리한 쪽으로 slippage 틱
  - 손절은 봉의 고가/저가가 손절가에 닿으면 손절가에(시가가 이미 넘었으면 시가에) 체결 − slippage
  - 진입한 봉에서도 손절 판정을 함. 익절(중심선)은 봉 마감 판정이라 같은 봉 손절·익절 충돌 없음
  - 그날 마지막 봉까지 보유가 남으면 마지막 종가로 정리
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import BUY, Engine, Params, params_dict
from journal import SimClockJournal

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent


def run_backtest(bars: list[dict], params: Params, folder: Path, *, slippage_ticks: int = 1, meta: dict | None = None) -> Path:
    j = SimClockJournal(folder)
    eng = Engine(params, j)
    eng.warmup = False
    slip = slippage_ticks * params.tick_size
    pending = None
    fills = 0

    def fill(intent, px, ts):
        nonlocal fills
        d = 1 if intent.side == BUY else -1
        px = round(px + d * slip, 4)
        j.fill({
            "intent_id": intent.id, "ord_no": f"BT-{intent.id}", "ts": ts.isoformat(), "purpose": intent.purpose,
            "side": intent.side, "qty": intent.qty, "price": px, "ref_price": intent.ref_price,
            "slippage_ticks": round((px - intent.ref_price) * d / params.tick_size, 1) if intent.ref_price else "",
            "source": "backtest_bar_model",
        })
        j.order({
            "intent_id": intent.id, "intent_ts": intent.ts.isoformat(), "purpose": intent.purpose, "side": intent.side,
            "qty": intent.qty, "order_type": "시장가(가정)", "reason": intent.reason, "ref_price": intent.ref_price,
            "sent_ts": ts.isoformat(), "ord_no": f"BT-{intent.id}", "status": "BT_FILLED", "filled_qty": intent.qty,
            "avg_price": px, "message": "",
        })
        eng.on_fill(intent, px, intent.qty, ts)
        fills += 1

    for i, b in enumerate(bars):
        j.now = b["start"]
        # 1) 직전 봉 마감의 주문을 이번 봉 시가에
        if pending is not None:
            fill(pending, b["open"], b["start"])
            pending = None
        # 2) 손절 (봉 안)
        if eng.position:
            trigger = b["low"] if eng.position > 0 else b["high"]
            it = eng.on_price(trigger, b["start"])
            if it is not None:
                gap = (b["open"] <= eng.stop_price) if eng.position > 0 else (b["open"] >= eng.stop_price)
                fill(it, b["open"] if gap else eng.stop_price, b["start"] + timedelta(seconds=30))
        # 3) 봉 마감 판정
        end = b["start"] + timedelta(minutes=params.bar_minutes)
        j.now = end
        pending = eng.on_bar_close(b)
        # 4) 그날 마지막 봉이면 남은 것 정리
        last_of_day = i == len(bars) - 1 or bars[i + 1]["start"].date() != b["start"].date()
        if last_of_day:
            if pending is not None and pending.is_entry:
                eng.on_order_failed(pending, "그날 마지막 봉의 진입 신호 — 체결할 다음 봉 없음")
                eng.fail_count = 0
                pending = None
            if pending is not None:
                fill(pending, b["close"], end)
                pending = None
            it = eng.force_exit(end, "데이터 끝/그날 끝 정리")
            if it is not None:
                fill(it, b["close"], end)

    info = {
        "run_id": folder.name, "mode": "backtest", "mode_label": "백테스트(봉 체결 가정)",
        "params": params_dict(params), "slippage_ticks": slippage_ticks, "bars": len(bars),
        "first_bar": bars[0]["start"].isoformat() if bars else "", "last_bar": bars[-1]["start"].isoformat() if bars else "",
        "fills": fills, **(meta or {}),
    }
    j.meta(info)
    j.close()
    return folder


# ---------------------------------------------------------------- 데이터
def bars_from_api(days: int, sym: str, env: str, config: str, minutes: int) -> list[dict]:
    from nh_client import NHClient, load_credentials

    client = NHClient(load_credentials(config, env), token_cache=HERE / ".token_cache.json")
    out, d, tried = [], datetime.now(KST).date(), 0
    got_days = 0
    while got_days < days and tried < days * 3 + 5:
        tried += 1
        if d.weekday() < 5:
            bars = client.minute_bars(sym, d.strftime("%Y%m%d"), minutes=minutes)
            now = datetime.now(KST)
            bars = [b for b in bars if b["start"] + timedelta(minutes=minutes) <= now]
            if bars:
                out = bars + out
                got_days += 1
                print(f"{d} : {len(bars)}봉")
        d -= timedelta(days=1)
    return out


def save_bars(bars: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["start", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b["start"].isoformat(), b["open"], b["high"], b["low"], b["close"], b.get("volume", "")])


def load_bars(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        return [
            {"start": datetime.fromisoformat(r["start"]), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]), "volume": r.get("volume", "")}
            for r in csv.DictReader(f)
        ]


def synthetic_bars(days: int = 3, seed: int = 7, start_price: float = 1385.0) -> list[dict]:
    """코드 점검용 가짜 1분봉(평균회귀 성분 + 잡음). 결과 보고에 쓰지 않습니다."""
    rnd = random.Random(seed)
    out, px = [], start_price
    day = datetime(2026, 10, 19, tzinfo=KST)
    for _ in range(days):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        anchor = px
        t = day.replace(hour=8, minute=45)
        while t < day.replace(hour=15, minute=45):
            o = px
            path = [o]
            for _ in range(6):
                px += rnd.gauss(0, 0.12) - 0.04 * (px - anchor)
                path.append(px)
            r = lambda v: round(round(v / 0.1) * 0.1, 1)
            out.append({"start": t, "open": r(o), "high": r(max(path)), "low": r(min(path)), "close": r(px), "volume": 100})
            px = r(px)
            t += timedelta(minutes=1)
        day += timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--days", type=int, help="API 로 최근 N거래일 분봉을 받음")
    src.add_argument("--bars-csv", help="저장한 봉 CSV 로 재현")
    src.add_argument("--synthetic", action="store_true", help="가짜 데이터(코드 점검용)")
    ap.add_argument("--symbol", default="A756B000")
    ap.add_argument("--env", default="real", choices=["paper", "real"])
    ap.add_argument("--config", default=str(HERE / "config.local.json"))
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--slippage", type=int, default=1, help="불리한 쪽 미끄러짐(틱)")
    ap.add_argument("--results", default=str(HERE / "results"))
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    params = Params.from_dict(json.loads(Path(args.strategy).read_text(encoding="utf-8")))
    meta: dict = {"symbol": args.symbol}
    if args.days:
        bars = bars_from_api(args.days, args.symbol, args.env, args.config, params.bar_minutes)
        meta["data_source"] = f"domestic_chart-minute ({args.env}) 최근 {args.days}거래일"
    elif args.bars_csv:
        bars = load_bars(Path(args.bars_csv))
        meta["data_source"] = f"CSV {args.bars_csv}"
    else:
        bars = synthetic_bars()
        meta["data_source"] = "SYNTHETIC — 코드 점검용 가짜 데이터, 보고서 결과로 쓰지 않음"
        meta["synthetic"] = True
    if not bars:
        print("봉이 없습니다.")
        return 1

    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    name = f"backtest_{'synthetic_' if args.synthetic else ''}{stamp}{('_' + args.tag) if args.tag else ''}"
    folder = Path(args.results) / name
    folder.mkdir(parents=True, exist_ok=True)
    save_bars(bars, folder / "bars_input.csv")
    run_backtest(bars, params, folder, slippage_ticks=args.slippage, meta=meta)

    from summarize import summarize_run

    print(f"요약: {summarize_run(folder)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
