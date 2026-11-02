"""지난 월물(만기 지난 종목) 1분봉 내려받기 — 근월물 전환 전 날짜를 백테스트에 쓰기 위해.

    python download_contracts.py --exch FSGX --symbols SFCV26,SFCU26
    python download_contracts.py --exch FHKE --symbols MHIV26,MHIU26
"""
import argparse
from datetime import datetime
from pathlib import Path

from check_overseas import download_bars, log
from nh_overseas import ApiFailure, NoResponse, OverseasClient, keys_from_env

ap = argparse.ArgumentParser()
ap.add_argument("--exch", required=True)
ap.add_argument("--symbols", required=True)
ap.add_argument("--days", type=int, default=60)
a = ap.parse_args()
out = Path("results") / f"bars_{datetime.now():%Y%m%d_%H%M%S}"
api = OverseasClient("paper", *keys_from_env(), out)
for sym in [s.strip() for s in a.symbols.split(",") if s.strip()]:
    try:
        h = download_bars(api, a.exch, sym, a.days, out / f"bars_{sym}.csv")
        log(f"{sym}: {h}")
    except (ApiFailure, NoResponse) as e:
        log(f"{sym}: 실패 {e}")
log(f"끝. 기록: {out}")
