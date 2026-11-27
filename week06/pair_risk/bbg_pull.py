"""(선택) 블룸버그 단말기 PC 에서 파이썬으로 1분봉·틱 받기 — 엑셀 Spreadsheet Builder 가 느리거나 틱이 너무 많을 때.

준비: 블룸버그 단말기에 로그인된 PC, 파이썬 3.
      pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
      (학교 PC 에서 설치가 막히면 엑셀 Spreadsheet Builder 를 쓰세요)

티커는 단말기에서 확인한 것을 그대로 넣습니다(아래 예시는 형식만 보여 주는 자리표시자).
  python bbg_pull.py bars  --ticker "<삼성 9월물 티커>" --leg S --month 202609 --start 2026-06-15 --end 2026-11-27
  python bbg_pull.py ticks --ticker "<삼성 12월물 티커>" --leg S --day 2026-11-12

블룸버그 API 의 요청·응답 시각은 GMT(UTC) 입니다. 이 스크립트는 한국 시간(UTC+9)으로 바꿔 저장하므로
bbg_import.py 는 --tz-shift 0 으로 쓰면 됩니다.
결과: bbg_raw/<leg>_<month>.csv · bbg_raw/tick_<leg>_<YYYYMMDD>.csv
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path

import blpapi  # 단말기 PC 에만 있음

KST = timedelta(hours=9)
OUT = Path(__file__).resolve().parent / "bbg_raw"


def session() -> blpapi.Session:
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost")
    opts.setServerPort(8194)
    s = blpapi.Session(opts)
    if not s.start() or not s.openService("//blp/refdata"):
        raise SystemExit("블룸버그 연결 실패 — 단말기에 로그인되어 있는지 확인")
    return s


def _run(s: blpapi.Session, req) -> list:
    s.sendRequest(req)
    msgs = []
    while True:
        ev = s.nextEvent(5000)
        for m in ev:
            if m.hasElement("responseError"):
                raise SystemExit(f"요청 오류: {m.getElement('responseError')}")
            msgs.append(m)
        if ev.eventType() == blpapi.Event.RESPONSE:
            return msgs


def bars(a) -> None:
    s = session()
    svc = s.getService("//blp/refdata")
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{a.leg}_{a.month}.csv"
    start = datetime.fromisoformat(a.start)
    end = datetime.fromisoformat(a.end)
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Dates", "Open", "High", "Low", "Last Price", "Volume"])
        cur = start
        while cur < end:                       # 한 번에 너무 길면 거절될 수 있어 20일씩
            nxt = min(cur + timedelta(days=20), end)
            req = svc.createRequest("IntradayBarRequest")
            req.set("security", a.ticker)
            req.set("eventType", "TRADE")
            req.set("interval", 1)
            req.set("startDateTime", cur - KST)  # 한국 시간 → GMT
            req.set("endDateTime", nxt - KST)
            for m in _run(s, req):
                if not m.hasElement("barData"):
                    continue
                arr = m.getElement("barData").getElement("barTickData")
                for i in range(arr.numValues()):
                    b = arr.getValueAsElement(i)
                    t = b.getElementAsDatetime("time") + KST
                    w.writerow([t.strftime("%Y-%m-%d %H:%M:%S"), b.getElementAsFloat("open"), b.getElementAsFloat("high"),
                                b.getElementAsFloat("low"), b.getElementAsFloat("close"), b.getElementAsInteger("volume")])
                    n += 1
            cur = nxt
    print(f"{path.name}: {n:,}봉")


def ticks(a) -> None:
    s = session()
    svc = s.getService("//blp/refdata")
    OUT.mkdir(exist_ok=True)
    day = datetime.fromisoformat(a.day)
    path = OUT / f"tick_{a.leg}_{day:%Y%m%d}.csv"
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Time", "Type", "Value", "Size"])
        for h0, h1 in ((8, 12), (12, 16)):      # 반나절씩
            req = svc.createRequest("IntradayTickRequest")
            req.set("security", a.ticker)
            for e in ("TRADE", "BID", "ASK"):
                req.getElement("eventTypes").appendValue(e)
            req.set("startDateTime", day.replace(hour=h0) - KST)
            req.set("endDateTime", day.replace(hour=h1) - KST)
            req.set("includeConditionCodes", False)
            for m in _run(s, req):
                if not m.hasElement("tickData"):
                    continue
                arr = m.getElement("tickData").getElement("tickData")
                for i in range(arr.numValues()):
                    tk = arr.getValueAsElement(i)
                    t = tk.getElementAsDatetime("time") + KST
                    w.writerow([t.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], tk.getElementAsString("type"),
                                tk.getElementAsFloat("value"), tk.getElementAsInteger("size")])
                    n += 1
    print(f"{path.name}: {n:,}건")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("bars")
    b.add_argument("--ticker", required=True)
    b.add_argument("--leg", choices=["S", "H"], required=True)
    b.add_argument("--month", required=True, help="결제월 YYYYMM (파일 이름용)")
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True)
    t = sp.add_parser("ticks")
    t.add_argument("--ticker", required=True)
    t.add_argument("--leg", choices=["S", "H"], required=True)
    t.add_argument("--day", required=True)
    a = ap.parse_args()
    bars(a) if a.cmd == "bars" else ticks(a)


if __name__ == "__main__":
    main()
