"""한국투자증권(KIS) Open API 로 국내주식 **현물 1분봉**을 과거 일자별로 받아 이 저장소 형식으로 저장합니다.

왜: 블룸버그 장중 자료를 못 받은 동안, 삼성전자·SK하이닉스 현물 1분봉(약 1년)으로
    국면별 결과와 손절·시간 값의 안정성을 봅니다(비용 결론은 선물 자료로만).

준비
  1. 한국투자증권 계좌 → KIS Developers(apiportal.koreainvestment.com)에서 Open API 신청 → APP Key·Secret
     (모의투자 키로도 실전과 같은 시세를 받을 수 있다고 알려져 있음. 실전 키가 호출 한도가 넉넉함)
  2. PowerShell:  $env:KIS_APP_KEY="..." ; $env:KIS_APP_SECRET="..."
  3. python kis_minute.py --start 2025-12-01 --end 2026-11-27            (실전 키)
     python kis_minute.py --start 2025-12-01 --end 2026-11-27 --env paper (모의 키, 느리게)

결과
  kis_cache/<종목코드>/<YYYYMMDD>.csv   하루 하나(다시 실행하면 받은 날은 건너뜀 — 중간에 끊겨도 이어받기)
  kis_spot/bars_S.csv · bars_H.csv · meta.json   (pair_walkforward · risk_analyze · risk_regime 가 그대로 읽음)
  → python risk_regime.py --data kis_spot --walk

API: 주식일별분봉조회 FHKST03010230, GET /uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice
     한 번에 최대 120봉, 기준 시각(FID_INPUT_HOUR_1)부터 과거로. 가장 이른 봉의 1분 전으로 옮기며 반복.
     보존 기간은 약 1년(증권사 정책에 따라 바뀔 수 있음).
주의: 봉 시각(stck_cntg_hour)이 봉 시작인지 끝인지는 첫 실행 뒤 09:00 봉 유무로 확인하세요.
     두 종목에 똑같이 적용되므로 스프레드 계산에는 영향이 없습니다.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
DOMAIN = {"real": "https://openapi.koreainvestment.com:9443",
          "paper": "https://openapivts.koreainvestment.com:29443"}
PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
TR_ID = "FHKST03010230"
LEGS = {"S": "005930", "H": "000660"}
TOKEN_FILE = HERE / ".kis_token.json"       # 토큰 발급은 1분에 1번으로 제한 → 하루 동안 재사용 (저장소에 올리지 않음)


class KIS:
    def __init__(self, env: str, sleep: float, session=None):
        self.base = DOMAIN[env]
        self.key = os.environ.get("KIS_APP_KEY", "")
        self.secret = os.environ.get("KIS_APP_SECRET", "")
        if not self.key or not self.secret:
            sys.exit("환경변수 KIS_APP_KEY · KIS_APP_SECRET 를 먼저 넣어 주세요")
        self.env = env
        self.sleep = sleep
        self.s = session or requests.Session()
        self.token = self._token()
        self.calls = 0

    def _token(self) -> str:
        if TOKEN_FILE.exists():
            t = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if t.get("env") == self.env and t.get("key_tail") == self.key[-4:] and t["expires"] > time.time() + 600:
                return t["token"]
        r = self.s.post(self.base + "/oauth2/tokenP", timeout=15,
                        json={"grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret})
        r.raise_for_status()
        d = r.json()
        tok = d["access_token"]
        TOKEN_FILE.write_text(json.dumps({"env": self.env, "key_tail": self.key[-4:], "token": tok,
                                          "expires": time.time() + int(d.get("expires_in", 86400))}), encoding="utf-8")
        return tok

    def minute(self, code: str, day: str, hhmmss: str) -> list[dict]:
        hdr = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self.token}",
               "appkey": self.key, "appsecret": self.secret, "tr_id": TR_ID, "custtype": "P"}
        prm = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_HOUR_1": hhmmss,
               "FID_INPUT_DATE_1": day, "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""}
        for attempt in range(5):
            time.sleep(self.sleep)
            self.calls += 1
            try:
                r = self.s.get(self.base + PATH, headers=hdr, params=prm, timeout=15)
            except requests.RequestException as e:
                print(f"    연결 오류({e.__class__.__name__}) — {2 ** attempt}초 뒤 다시")
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                d = r.json()
                if str(d.get("rt_cd", "0")) == "0":
                    return d.get("output2") or []
                msg = f"{d.get('msg_cd')} {d.get('msg1')}"
                if "초당" in str(d.get("msg1", "")) or d.get("msg_cd") in ("EGW00201",):
                    time.sleep(1 + attempt)          # 호출 한도 → 쉬었다 다시
                    continue
                raise RuntimeError(f"API 오류: {msg}")
            if r.status_code in (429, 500, 502, 503):
                time.sleep(1 + attempt)
                continue
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        raise RuntimeError("다섯 번 다시 해도 실패")


def fetch_day(api: KIS, code: str, day: str) -> list[dict]:
    """그날 15:30 부터 과거로 120봉씩 거슬러 올라가며 하루치를 모음."""
    rows: dict[str, dict] = {}
    hh = "153000"
    for _ in range(10):                                   # 하루 약 380봉 → 4번이면 충분, 넉넉히 10번
        out = api.minute(code, day, hh)
        got = [o for o in out if o.get("stck_bsop_date") == day]
        if not got:
            break
        new = 0
        for o in got:
            t = o["stck_cntg_hour"]
            if t not in rows:
                rows[t] = o
                new += 1
        earliest = min(o["stck_cntg_hour"] for o in got)
        if new == 0 or earliest <= "090000":
            break
        hh = (datetime.strptime(earliest, "%H%M%S") - timedelta(minutes=1)).strftime("%H%M%S")
    return [rows[t] for t in sorted(rows)]


def save_day(path: Path, rows: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "time", "open", "high", "low", "close", "volume"])
        for o in rows:
            vol = float(o.get("cntg_vol") or 0)
            if vol <= 0:
                continue                                  # 체결 없는 분은 빼기(NH 분봉과 같은 방식)
            w.writerow([o["stck_bsop_date"], o["stck_cntg_hour"], o["stck_oprc"], o["stck_hgpr"],
                        o["stck_lwpr"], o["stck_prpr"], int(vol)])
            n += 1
    return n


def assemble(start: str, end: str) -> None:
    out = HERE / "kis_spot"
    out.mkdir(exist_ok=True)
    days = set()
    for leg, code in LEGS.items():
        files = sorted(p for p in (HERE / "kis_cache" / code).glob("*.csv") if start <= p.stem <= end)
        with (out / f"bars_{leg}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "time", "open", "high", "low", "close", "volume"])
            for p in files:
                with p.open(encoding="utf-8") as f:
                    r = csv.reader(f)
                    next(r, None)
                    rows = list(r)
                if rows:
                    days.add(p.stem)
                w.writerows(rows)
    (out / "meta.json").write_text(json.dumps({"source": "KIS 현물 1분봉 (FHKST03010230)", "legs": LEGS,
                                               "days": sorted(days)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ kis_spot/ 에 {len(days)}거래일 합침")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--env", choices=["real", "paper"], default="real")
    ap.add_argument("--sleep", type=float, help="호출 간격(초). 기본 실전 0.08 · 모의 0.55")
    a = ap.parse_args()
    sleep = a.sleep if a.sleep is not None else (0.08 if a.env == "real" else 0.55)
    api = KIS(a.env, sleep)
    d0, d1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    days = [d.strftime("%Y%m%d") for d in days if d.weekday() < 5]
    print(f"{len(days)}개 평일 × 종목 {list(LEGS.values())} · {a.env} · 호출 간격 {sleep}초")
    t0 = time.time()
    for k, day in enumerate(reversed(days), 1):          # 최근 날부터 — 중간에 멈춰도 최근 자료가 먼저 쌓임
        for leg, code in LEGS.items():
            path = HERE / "kis_cache" / code / f"{day}.csv"
            if path.exists():
                continue
            rows = fetch_day(api, code, day)
            n = save_day(path, rows)
            if n == 0:
                path.write_text("date,time,open,high,low,close,volume\n", encoding="utf-8")   # 휴장일 표시
        if k % 10 == 0 or k == len(days):
            el = time.time() - t0
            print(f"  {k}/{len(days)}일 ({day}) · 호출 {api.calls} · {el / 60:.1f}분")
    assemble(days[0], days[-1])


if __name__ == "__main__":
    main()
