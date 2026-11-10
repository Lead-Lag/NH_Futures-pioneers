"""지난 분봉 백테스트 — 실시간 실행과 **같은 pair_engine.py** 를 씁니다.

체결 가정: 판정한 봉의 종가에서 다리마다 불리하게 --slip 틱(삼성 tick_s · 하이닉스 tick_h).
금액 손절은 봉 종가로만 판정합니다(실행에서는 현재가로 수시 판정). 수수료는 설정값.

예)
  python backtest_pair.py --data data_dec                  # pair_analyze.py fetch 로 받은 폴더
  python backtest_pair.py --data data_dec --slip 0         # 비용 0 과 비교
  python backtest_pair.py --data data_dec --days 20261104,20261105,20261106,20261109
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from journal import RunJournal, summarize_run
from pair_engine import BUY, COMPLETE, PairEngine, PairIntent, PairParams, align_minutes, params_dict

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent


class _Fill:
    """백테스트용 결과 객체 (pair_broker.PairOutcome 과 같은 칸)."""

    def __init__(self, it: PairIntent, prices: dict[str, float], ticks: dict[str, float], slip: float):
        self.status = COMPLETE
        self.qty, self.avg = {}, {}
        for leg in it.legs:
            d = 1 if leg.side == BUY else -1
            self.qty[leg.name] = leg.qty
            self.avg[leg.name] = prices[leg.name] + d * slip * ticks[leg.name]
        self.unwind_pnl = 0.0
        self.message = "백테스트 체결 가정"


def load(path: Path) -> dict[str, list[dict]]:
    """bars_*.csv(date,time,open,high,low,close,volume) → 날짜별 봉 목록."""
    days: dict[str, list[dict]] = {}
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            t = datetime.strptime(r["date"] + r["time"].zfill(6), "%Y%m%d%H%M%S").replace(tzinfo=KST)
            days.setdefault(r["date"], []).append({"start": t, "close": float(r["close"]), "volume": float(r["volume"])})
    return days


def run(data: Path, cfg: dict, slip: float, only: list[str] | None, out: Path) -> Path:
    p = PairParams.from_dict(cfg)
    j = RunJournal(out, echo=False)
    j.meta({"run_id": out.name, "mode": "backtest", "mode_label": f"백테스트(봉 종가 ± {slip:g}틱)",
            "environment": "-", "symbols": {"S": cfg.get("sym_s"), "H": cfg.get("sym_h")}, "data": str(data),
            "params": params_dict(p), "slip_ticks": slip, "started_at": datetime.now(KST).isoformat(timespec="seconds")})
    eng = PairEngine(p, j)
    eng.warmup = False
    S, H = load(data / "bars_S.csv"), load(data / "bars_H.csv")
    ticks = {"S": p.tick_s, "H": p.tick_h}
    for day in sorted(set(S) & set(H)):
        if only and day not in only:
            continue
        last = {}
        upto = max(S[day][-1]["start"], H[day][-1]["start"]) + timedelta(minutes=1)
        for pb in align_minutes(S[day], H[day], None, upto, last):
            it = eng.on_bar_close(pb)
            if it is None and eng.direction:
                it = eng.on_prices(pb["close_s"], pb["close_h"], pb["start"] + timedelta(minutes=1))
            if it is not None:
                eng.on_pair_done(it, _Fill(it, {"S": pb["close_s"], "H": pb["close_h"]}, ticks, slip),
                                 pb["start"] + timedelta(minutes=1))
        if eng.direction:  # 그날 마지막 봉까지 보유가 남으면 마지막 종가로 정리
            it = eng.force_exit(upto, "백테스트 그날 마지막 봉")
            eng.on_pair_done(it, _Fill(it, dict(last), ticks, slip), upto)
    j.close()
    return summarize_run(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--strategy", default=str(HERE / "strategy_config.json"))
    ap.add_argument("--slip", type=float, default=1.0, help="다리마다 불리한 틱")
    ap.add_argument("--days", help="YYYYMMDD,YYYYMMDD,... 만")
    ap.add_argument("--out")
    a = ap.parse_args()
    cfg = json.loads(Path(a.strategy).read_text(encoding="utf-8"))
    out = Path(a.out) if a.out else HERE / "results" / f"backtest_{datetime.now():%Y%m%d_%H%M%S}_slip{a.slip:g}"
    s = run(Path(a.data), cfg, a.slip, a.days.split(",") if a.days else None, out)
    print(Path(s).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
