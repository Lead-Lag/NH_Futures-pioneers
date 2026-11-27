"""4주차 실행 전 분석 — 삼성전자·SK하이닉스 주식선물 페어(스프레드).

본 실행 전에 「이 페어로 가도 되는가」를 숫자로 확인하는 스크립트입니다.
주문은 내지 않습니다. 조회 API(차트·현재가·호가)만 씁니다.

하위 명령 네 개
---------------
1) find     종목 마스터에서 주식선물 종목코드 찾기 (키 불필요)
      python pair_analyze.py find 삼성전자 SK하이닉스

2) fetch    두 종목(+선택: 미국달러 선물) 1분봉을 최근 N 영업일 받아 data/ 에 저장
      python pair_analyze.py fetch --sym-s <삼성 코드> --sym-h <하이닉스 코드> --days 10 --fx-sym A756C000

3) quotes   두 종목 호가를 몇 분간 표본 조사 → 실제 매수·매도 호가 차(거래 비용) 측정
      python pair_analyze.py quotes --sym-s <삼성 코드> --sym-h <하이닉스 코드> --minutes 10 --every 5

4) analyze  저장한 분봉으로 분석 → results/analysis_<시각>/analysis.md · CSV · 그림
      python pair_analyze.py analyze --data data

키는 2주차와 같이 환경변수 NH_APP_KEY · NH_APP_SECRET 에서 읽습니다
(또는 config.local.json 의 app_key · app_secret). 기본 접속은 모의(apidemo).
실전 키로 시세만 받을 때는 --env real.

분석이 답하는 질문
------------------
① 유동성   두 종목 1분봉에 거래 없는 봉이 얼마나 되나 (가짜 괴리 위험)
② 관계     1분 수익률 기준 헤지비율 β, 날마다 β 가 얼마나 흔들리나
③ 괴리     |z| ≥ 2 괴리가 하루 몇 번 나고, 몇 %가 되돌아왔나(손절선 전에), 반감기
④ 비용     되돌아온 폭이 네 번(진입·청산 × 두 다리)의 비용을 넘나
⑤ 수량     정수 계약 조합과 남는 노출
⑥ 백테스트 설계용 날 / 검증용 날을 나눠 변형 4개(z 방식 2 × 진입 방식 2) 비교
⑦ 선행·후행 두 종목 사이, 그리고 원/달러와 반도체 공통 움직임 사이의 시차 상관

필요 패키지: requests, pandas, numpy (그림은 선택: matplotlib)
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 공통 설정
# ---------------------------------------------------------------------------

BASE_URLS = {"paper": "https://apidemo.futures.co.kr", "real": "https://api.futures.co.kr"}
MASTER_URL = "https://apidev.futures.co.kr/guide/master/stk_fut_opt.mst.gz"
TOKEN_PATH = "/auth-service/v1/token"
CHART_MINUTE_PATH = "/trade/v1/domestic/chart-minute"
PRICE_PATH = "/trade/v1/domestic/price"
PRICE_BID_PATH = "/trade/v1/domestic/price-bid"

# errorCode 가 실려 와도 정상인 값 (NH 저장소 error_codes.OK_CODES)
OK_CODES = {"EGW002", "EGW013", "0"}
MIN_INTERVAL = 0.12  # 시세 조회 초당 10건 등급 → 호출 사이 최소 간격(초)

# 마스터 파일 컬럼 (NH 저장소 domestic_futureoption/master/__init__.py 와 같은 순서)
MASTER_FIELDS = (
    "mrkt_tp", "fo_tp", "opt_tp", "under_name", "exp_ym", "cme_tp", "class", "kospi_key",
    "series", "rank_serie", "name_kor", "name_eng", "upper_p_lv1", "lower_p_lv1", "strike_p",
    "inst_grp_cd", "p_dec", "ctrt_size", "rem_cnt", "dynmc_yn", "dynmc_up_p", "dynmc_dn_p",
    "real_Code", "kofex_Code", "mrkt_code", "trd_code",
)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# NH API 최소 클라이언트 (조회 전용)
# ---------------------------------------------------------------------------

class ApiError(RuntimeError):
    pass


def load_keys() -> tuple[str, str]:
    key = os.environ.get("NH_APP_KEY", "").strip()
    secret = os.environ.get("NH_APP_SECRET", "").strip()
    if key and secret:
        return key, secret
    cfg = Path("config.local.json")
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        key = str(data.get("app_key") or data.get("NH_APP_KEY") or "").strip()
        secret = str(data.get("app_secret") or data.get("NH_APP_SECRET") or "").strip()
        if key and secret:
            return key, secret
    sys.exit("키가 없습니다. 환경변수 NH_APP_KEY · NH_APP_SECRET 를 먼저 넣어 주세요.")


class Api:
    """토큰 발급 + 조회 호출. 조회만 하므로 시간 초과 때 3번까지 다시 부릅니다."""

    def __init__(self, env: str, base_url: str | None = None, timeout: float = 10.0):
        import requests  # 지연 import: analyze 만 쓸 때는 requests 가 없어도 됨

        self._requests = requests
        self.base = (base_url or BASE_URLS[env]).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._last = 0.0
        self._auth = ""
        self.calls = 0
        self.errors = 0

    def _token(self) -> None:
        key, secret = load_keys()
        r = self.session.post(
            self.base + TOKEN_PATH,
            data={"grant_type": "client_credentials", "appkey": key, "appsecret": secret},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise ApiError(f"토큰 발급 실패 HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        tok = str(body.get("access_token") or "").strip()
        if not tok:
            raise ApiError(f"토큰 응답에 access_token 이 없습니다: {body}")
        self._auth = f"{str(body.get('token_type') or 'Bearer').strip()} {tok}"

    def post(self, path: str, payload: dict) -> dict:
        if not self._auth:
            self._token()
        refreshed = False
        for attempt in range(4):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                r = self.session.post(
                    self.base + path, json=payload,
                    headers={"Authorization": self._auth}, timeout=self.timeout,
                )
            except self._requests.RequestException as exc:  # 시간 초과·연결 오류
                self.errors += 1
                if attempt < 3:
                    time.sleep(1 + attempt)
                    continue
                raise ApiError(f"{path} 연결 실패: {exc}") from exc
            if r.status_code == 401 and not refreshed:
                refreshed = True
                self._token()
                continue
            try:
                body = r.json()
            except ValueError:
                raise ApiError(f"{path} HTTP {r.status_code}, 본문을 읽을 수 없음: {r.text[:200]}")
            code = str(body.get("errorCode") or "").strip() if isinstance(body, dict) else ""
            if code and code not in OK_CODES:
                if code.startswith("EGW05") and not refreshed:  # 토큰 문제
                    refreshed = True
                    self._token()
                    continue
                raise ApiError(f"{path} 실패 errorCode={code} {body.get('errorMessage') or body}")
            msg = str(body.get("msg_cd") or "").strip() if isinstance(body, dict) else ""
            if msg:
                raise ApiError(f"{path} 거부 msg_cd={msg} {body.get('msg_cntn')}")
            if r.status_code != 200:
                raise ApiError(f"{path} HTTP {r.status_code}: {str(body)[:200]}")
            return body
        raise ApiError(f"{path} 재시도 소진")


# ---------------------------------------------------------------------------
# find — 종목 마스터에서 주식선물 찾기
# ---------------------------------------------------------------------------

def cmd_find(args) -> None:
    import requests

    log(f"종목 마스터를 받는 중: {MASTER_URL}")
    r = requests.get(MASTER_URL, timeout=30)
    r.raise_for_status()
    raw = r.content
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass  # 이미 풀린 채로 온 경우
    text = raw.decode("utf-8", errors="replace")
    rows = []
    for line in text.split("\n"):
        vals = line.rstrip("\r").split(";")
        if len(vals) < len(MASTER_FIELDS):
            continue
        rows.append(dict(zip(MASTER_FIELDS, vals)))
    log(f"주식선물·옵션 마스터 {len(rows)}줄")

    def norm(s: str) -> str:
        return s.replace(" ", "").lower()

    for q in args.names:
        hits = [x for x in rows if norm(q) in norm(x["name_kor"])
                and x["fo_tp"] in (("F", "C") if args.spread else ("F",))]
        hits.sort(key=lambda x: (x["fo_tp"], x["exp_ym"]))
        log(f"\n[{q}] {len(hits)}건 (선물{'·스프레드' if args.spread else ''})")
        log(f"  {'종목코드':<10} {'구분':<4} {'최종거래일':<10} {'승수':>4} {'잔존':>4}  종목명")
        for x in hits:
            log(f"  {x['series']:<10} {x['fo_tp']:<4} {x['exp_ym']:<10} {x['ctrt_size']:>4} "
                f"{x['rem_cnt']:>4}  {x['name_kor'].strip()}")
    log("\n※ 모의 서버는 종목이 실전과 다를 수 있습니다. 고른 코드로 fetch 를 돌려 봉이 오는지 확인하세요.")


# ---------------------------------------------------------------------------
# fetch — 1분봉 수집
# ---------------------------------------------------------------------------

# 한 응답이 최대 300봉이라 하루를 두 창으로 나눠 부릅니다(각 240봉 이하).
# NH 저장소 메모: 이어받기 키(nxt_key)는 믿을 수 없어 시각 창으로 나눕니다.
DAY_WINDOWS = (("080000", "115959"), ("120000", "165959"))


def business_days(end: date, n: int) -> list[date]:
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def fetch_day(api: Api, sym: str, day: date) -> list[dict]:
    ds = day.strftime("%Y%m%d")
    bars: dict[str, dict] = {}
    for st, ed in DAY_WINDOWS:
        body = api.post(CHART_MINUTE_PATH, {
            "sym": sym, "inq_strt_dt": ds, "inq_ed_dt": ds, "strt_tm": st, "ed_tm": ed,
            "dcnt": "1", "nght_tp": "1", "cidx_yn": "0", "fut_cidx_exp_mod": "0",
            "nxt_key": "99999999", "req_qty": "300",
        })
        for o in body.get("Occurs1") or []:
            if str(o.get("occurs1_ref_dt", ds)) != ds:
                continue
            t = str(o.get("occurs1_exch_tm") or "").zfill(6)
            try:
                bars[t] = {
                    "date": ds, "time": t,
                    "open": float(o["occurs1_open_pric"]), "high": float(o["occurs1_high_pric"]),
                    "low": float(o["occurs1_low_pric"]), "close": float(o["occurs1_close_pric"]),
                    "volume": float(o.get("occurs1_cum_trd_qty") or 0),
                }
            except (KeyError, ValueError):
                continue
    out = [bars[t] for t in sorted(bars)]
    # 진행 중인 봉은 버림(봉 시작 + 1분 > 지금)
    now = datetime.now()
    out = [b for b in out
           if datetime.strptime(b["date"] + b["time"], "%Y%m%d%H%M%S") + timedelta(minutes=1) <= now]
    return out


def fix_volume(rows: list[dict]) -> list[dict]:
    """거래량 칸 이름이 cum_trd_qty 라서, 혹시 누적값으로 오면 봉별로 바꿉니다.

    NH 응답 기록에서는 봉별 값으로 보였지만(앞뒤 봉이 오르내림), 하루 안에서 95% 넘게
    줄지 않고 커지기만 하면 누적으로 보고 차분합니다.
    """
    if len(rows) < 20:
        return rows
    v = [r["volume"] for r in rows]
    nondec = sum(1 for a, b in zip(v, v[1:]) if b >= a) / (len(v) - 1)
    if nondec > 0.95 and v[-1] > 5 * max(v[0], 1):
        prev = 0.0
        for r in rows:
            cur = r["volume"]
            r["volume"] = max(cur - prev, 0.0)
            prev = cur
    return rows


def symbol_info(api: Api, sym: str) -> dict:
    try:
        b = api.post(PRICE_PATH, {"sym": sym})
    except ApiError as exc:
        return {"sym": sym, "error": str(exc)}
    keep = ("sym_exch", "sym_nm", "lst_trd_dt", "rem_dy", "mltplr", "last_pric", "spt_pric",
            "theo_pric", "trd_qty", "opint", "ask_pric", "bid_pric")
    return {"sym": sym, **{k: b.get(k) for k in keep if k in b}}


def cmd_fetch(args) -> None:
    api = Api(args.env, args.base_url)
    out = Path(args.data)
    out.mkdir(parents=True, exist_ok=True)
    legs = {"S": args.sym_s, "H": args.sym_h}
    if args.fx_sym:
        legs["FX"] = args.fx_sym
    end = datetime.strptime(args.end, "%Y%m%d").date() if args.end else date.today()
    days = business_days(end, args.days)
    meta = {"env": args.env, "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "days": [d.isoformat() for d in days], "legs": {}}
    for label, sym in legs.items():
        info = symbol_info(api, sym)
        meta["legs"][label] = info
        log(f"[{label}] {sym} {info.get('sym_nm', '')} 최종거래일 {info.get('lst_trd_dt', '?')} "
            f"승수 {info.get('mltplr', '?')} 현재가 {info.get('last_pric', '?')}"
            + (f"  ※ {info['error']}" if "error" in info else ""))
        rows: list[dict] = []
        for d in days:
            got = fix_volume(fetch_day(api, sym, d))
            log(f"    {d}  {len(got):>4}봉")
            rows += got
        path = out / f"bars_{label}.csv"
        with path.open("w", encoding="utf-8") as f:
            f.write("date,time,open,high,low,close,volume\n")
            for r in rows:
                f.write(f"{r['date']},{r['time']},{r['open']},{r['high']},{r['low']},{r['close']},{r['volume']}\n")
        log(f"    → {path} ({len(rows)}봉)")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"API 호출 {api.calls}회 · 재시도한 오류 {api.errors}회 · 메타 → {out / 'meta.json'}")


# ---------------------------------------------------------------------------
# quotes — 호가 차 표본 조사
# ---------------------------------------------------------------------------

def best_quote(body: dict) -> tuple[float, float, float, float] | None:
    rows = body.get("Occurs1") or []
    if not rows:
        return None
    r = rows[0]
    try:
        return (float(r["occurs1_bid_best_pric"]), float(r["occurs1_ask_best_pric"]),
                float(r.get("occurs1_bid_qty") or 0), float(r.get("occurs1_ask_qty") or 0))
    except (KeyError, ValueError):
        return None


def cmd_quotes(args) -> None:
    api = Api(args.env, args.base_url)
    out = Path(args.data)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "quotes.csv"
    new = not path.exists()
    end_at = time.time() + args.minutes * 60
    n = 0
    with path.open("a", encoding="utf-8") as f:
        if new:
            f.write("ts,leg,sym,bid,ask,bid_qty,ask_qty\n")
        while time.time() < end_at:
            ts = datetime.now().isoformat(timespec="seconds")
            for label, sym in (("S", args.sym_s), ("H", args.sym_h)):
                try:
                    q = best_quote(api.post(PRICE_BID_PATH, {"sym": sym, "exch_cd": "KSP"}))
                except ApiError as exc:
                    log(f"  {ts} {label} 오류: {exc}")
                    continue
                if q and q[0] > 0 and q[1] > 0:
                    f.write(f"{ts},{label},{sym},{q[0]},{q[1]},{q[2]},{q[3]}\n")
                    n += 1
            f.flush()
            log(f"  {ts} 표본 {n}개")
            time.sleep(args.every)
    log(f"→ {path}  (analyze 가 이 파일을 읽어 실제 호가 차로 비용을 계산합니다)")


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

@dataclass
class Params:
    session_start: str = "0900"     # 분석에 쓰는 봉 시작 시각 범위 (현물 장중)
    session_end: str = "1529"
    entry_start: str = "0910"       # 신규 진입 허용 시각
    entry_end: str = "1500"
    force_exit: str = "1515"        # 이 시각 봉 마감에 전량 정리
    window: int = 40                # rolling z 창(봉)
    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 3.5
    max_hold: int = 60              # 시간 청산(봉)
    cooldown: int = 3               # 손절 뒤 대기(봉)
    require_fresh: bool = True      # 진입 봉에 두 종목 모두 체결이 있어야 진입
    slip_ticks: float = 1.0         # 비용 있는 백테스트: 다리마다 불리하게 몇 틱
    design_days: int = 0            # 0 이면 앞 60%


def hhmm_to_min(s: str) -> int:
    return int(s[:2]) * 60 + int(s[2:4])


def load_bars(path: Path):
    import pandas as pd

    df = pd.read_csv(path, dtype={"date": str, "time": str})
    df["time"] = df["time"].str.zfill(6)
    df["minute"] = df["time"].str[:2].astype(int) * 60 + df["time"].str[2:4].astype(int)
    return df


def build_frame(bars: dict, p: Params):
    """두 다리(+FX)를 같은 1분 격자에 맞춥니다. 봉이 없으면 직전 종가로 채우고 'stale' 표시."""
    import numpy as np
    import pandas as pd

    lo, hi = hhmm_to_min(p.session_start), hhmm_to_min(p.session_end)
    days = sorted(set(bars["S"]["date"]) & set(bars["H"]["date"]))
    frames = []
    for d in days:
        grid = pd.DataFrame({"minute": range(lo, hi + 1)})
        grid["date"] = d
        for label, df in bars.items():
            sub = df[(df["date"] == d) & (df["minute"] >= lo) & (df["minute"] <= hi)]
            sub = sub[["minute", "close", "volume"]].rename(
                columns={"close": f"c_{label}", "volume": f"v_{label}"})
            grid = grid.merge(sub, on="minute", how="left")
            grid[f"present_{label}"] = grid[f"c_{label}"].notna()
            grid[f"traded_{label}"] = grid[f"present_{label}"] & (grid[f"v_{label}"].fillna(0) > 0)
            grid[f"c_{label}"] = grid[f"c_{label}"].ffill()
        frames.append(grid)
    f = pd.concat(frames, ignore_index=True)
    f = f.dropna(subset=["c_S", "c_H"]).reset_index(drop=True)
    for label in bars:
        f[f"ln_{label}"] = np.log(f[f"c_{label}"])
        f[f"r_{label}"] = f.groupby("date")[f"ln_{label}"].diff()
    f["fresh"] = f["traded_S"] & f["traded_H"]
    f["fresh_ret"] = f["fresh"] & f.groupby("date")["fresh"].shift(1, fill_value=False)
    f["hhmm"] = f["minute"].map(lambda m: f"{m // 60:02d}:{m % 60:02d}")
    return f, days


def liquidity_table(bars: dict, f, days, p: Params):
    import pandas as pd

    lo, hi = hhmm_to_min(p.session_start), hhmm_to_min(p.session_end)
    n_grid = hi - lo + 1
    rows = []
    for label, df in bars.items():
        for d in days:
            sub = df[(df["date"] == d) & (df["minute"] >= lo) & (df["minute"] <= hi)]
            traded = (sub["volume"] > 0).sum()
            # 가장 길게 이어진 '체결 없는 분'
            tr = f[f["date"] == d][f"traded_{label}"].tolist() if f"traded_{label}" in f else []
            run = best = 0
            for t in tr:
                run = 0 if t else run + 1
                best = max(best, run)
            rows.append({"leg": label, "date": d, "grid": n_grid, "bars": len(sub),
                         "traded_pct": round(100 * traded / n_grid, 1),
                         "median_vol": float(sub["volume"].median()) if len(sub) else 0.0,
                         "longest_gap_min": best})
    return pd.DataFrame(rows)


def ols(x, y):
    import numpy as np

    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < 10 or x.var() == 0:
        return float("nan"), float("nan"), len(x)
    beta = float(((x - x.mean()) * (y - y.mean())).sum() / ((x - x.mean()) ** 2).sum())
    corr = float(np.corrcoef(x, y)[0, 1])
    return beta, corr, len(x)


def add_z(f, beta: float, design_mask, p: Params):
    """스프레드 s = ln S − β ln H 와 z 두 가지(rolling, fixed)."""
    f = f.copy()
    f["s"] = f["ln_S"] - beta * f["ln_H"]
    g = f.groupby("date")["s"]
    f["mu_roll"] = g.transform(lambda x: x.rolling(p.window, min_periods=p.window).mean())
    f["sd_roll"] = g.transform(lambda x: x.rolling(p.window, min_periods=p.window).std(ddof=0))
    f["z_rolling"] = (f["s"] - f["mu_roll"]) / f["sd_roll"]
    mu, sd = float(f.loc[design_mask, "s"].mean()), float(f.loc[design_mask, "s"].std(ddof=0))
    f["z_fixed"] = (f["s"] - mu) / sd
    return f, mu, sd


def half_life(f, mode: str, mask):
    """AR(1): Δd_t = a + b·d_{t-1} (하루 안에서만). 반감기 = −ln2 / ln(1+b) 봉."""
    import numpy as np

    d = f["s"] - (f["mu_roll"] if mode == "rolling" else f.loc[mask, "s"].mean())
    prev = d.groupby(f["date"]).shift(1)
    dd = d - prev
    m = mask & prev.notna() & dd.notna()
    b, _, n = ols(prev[m], dd[m])
    if not (np.isfinite(b) and -1 < b < 0):
        return float("nan"), b, n
    return float(-math.log(2) / math.log(1 + b)), b, n


def find_events(f, mode: str, entry: str, p: Params, mask):
    """괴리 사건: 진입 조건이 난 봉 → 이후 수렴(|z|≤exit)·손절(|z|≥stop)·시간·장마감 중 무엇이 먼저였나."""
    zc = f"z_{mode}"
    es, ee, fx = hhmm_to_min(p.entry_start), hhmm_to_min(p.entry_end), hhmm_to_min(p.force_exit)
    out = []
    for d, sub in f[mask].groupby("date"):
        z = sub[zc].to_numpy()
        s = sub["s"].to_numpy()
        mins = sub["minute"].to_numpy()
        fresh = sub["fresh"].to_numpy()
        hh = sub["hhmm"].to_numpy()
        i, n = 1, len(sub)
        while i < n:
            zp, zt = z[i - 1], z[i]
            sig = 0
            if math.isfinite(zp) and math.isfinite(zt) and es <= mins[i] <= ee:
                if entry == "immediate":
                    if zt >= p.entry_z > zp:
                        sig = -1          # 스프레드 매도: 삼성 매도·하이닉스 매수
                    elif zt <= -p.entry_z < zp:
                        sig = +1          # 스프레드 매수: 삼성 매수·하이닉스 매도
                else:  # reentry: 밖으로 나갔다가 다시 안으로 들어온 봉
                    if zp > p.entry_z >= zt > p.exit_z:
                        sig = -1
                    elif zp < -p.entry_z <= zt < -p.exit_z:
                        sig = +1
            if sig == 0:
                i += 1
                continue
            stale = not fresh[i]
            outcome, j = "eod", i
            for j in range(i + 1, n):
                zj = z[j]
                if not math.isfinite(zj):
                    continue
                if (sig == -1 and zj <= p.exit_z) or (sig == 1 and zj >= -p.exit_z):
                    outcome = "converge"
                    break
                if (sig == -1 and zj >= p.stop_z) or (sig == 1 and zj <= -p.stop_z):
                    outcome = "stop"
                    break
                if j - i >= p.max_hold:
                    outcome = "time"
                    break
                if mins[j] >= fx:
                    outcome = "eod"
                    break
            gain_bp = sig * (s[j] - s[i]) * 1e4
            out.append({"date": d, "time": hh[i], "dir": "매수(삼성↑)" if sig > 0 else "매도(삼성↓)",
                        "z_entry": round(float(z[i]), 2), "stale_entry": stale,
                        "outcome": outcome, "bars": int(j - i), "gain_bp": round(float(gain_bp), 2)})
            i = j + 1
    return out


def summarize_events(ev, n_days: int, cost_bp: float):
    import numpy as np

    if not ev:
        return {"events": 0, "per_day": 0.0}
    use = ev
    g = np.array([e["gain_bp"] for e in use])
    oc = [e["outcome"] for e in use]
    conv = [e for e in use if e["outcome"] == "converge"]
    stop = [e for e in use if e["outcome"] == "stop"]
    return {
        "events": len(use), "per_day": round(len(use) / max(n_days, 1), 2),
        "converge_pct": round(100 * oc.count("converge") / len(use), 1),
        "stop_pct": round(100 * oc.count("stop") / len(use), 1),
        "time_eod_pct": round(100 * (oc.count("time") + oc.count("eod")) / len(use), 1),
        "median_bars_to_converge": float(np.median([e["bars"] for e in conv])) if conv else float("nan"),
        "mean_gain_bp": round(float(g.mean()), 2),
        "mean_gain_converge_bp": round(float(np.mean([e["gain_bp"] for e in conv])), 2) if conv else float("nan"),
        "mean_loss_stop_bp": round(float(np.mean([e["gain_bp"] for e in stop])), 2) if stop else float("nan"),
        "mean_net_bp": round(float(g.mean()) - cost_bp, 2),
        "stale_entry_pct": round(100 * sum(e["stale_entry"] for e in use) / len(use), 1),
    }


def estimate_tick(series) -> float:
    import numpy as np

    v = np.unique(np.round(np.asarray(series, float), 6))
    dif = np.diff(v)
    dif = dif[dif > 1e-9]
    return float(np.round(dif.min(), 6)) if len(dif) else float("nan")


def size_combos(beta: float, ps: float, ph: float, ms: float, mh: float, top: int = 6):
    """정수 계약 조합: 하이닉스 명목금액 ≈ β × 삼성 명목금액 (수익률 기준 β 헤지)."""
    out = []
    for qh in range(1, 5):
        for qs in range(1, 41):
            ns, nh = qs * ms * ps, qh * mh * ph
            target = abs(beta) * ns
            err = (nh - target) / target if target else float("inf")
            out.append({"q_S": qs, "q_H": qh, "notional_S": round(ns), "notional_H": round(nh),
                        "hedge_error_pct": round(100 * err, 1)})
    out.sort(key=lambda x: (abs(x["hedge_error_pct"]), x["q_S"] + x["q_H"]))
    return out[:top]


def backtest(f, mode: str, entry: str, p: Params, mask, qs: int, qh: int, ms: float, mh: float,
             tick_s: float, tick_h: float, slip: float):
    """정수 계약 실제 포트폴리오 백테스트. 판정·체결은 봉 마감 종가 ± 미끄러짐(다리마다)."""
    zc = f"z_{mode}"
    es, ee, fx = hhmm_to_min(p.entry_start), hhmm_to_min(p.entry_end), hhmm_to_min(p.force_exit)
    trades = []
    for d, sub in f[mask].groupby("date"):
        z = sub[zc].to_numpy()
        cs, ch = sub["c_S"].to_numpy(), sub["c_H"].to_numpy()
        mins, fresh, hh = sub["minute"].to_numpy(), sub["fresh"].to_numpy(), sub["hhmm"].to_numpy()
        pos, k0, es_px, eh_px, cool = 0, 0, 0.0, 0.0, -1
        for k in range(1, len(sub)):
            zt, zp = z[k], z[k - 1]
            if pos != 0:
                reason = None
                if math.isfinite(zt):
                    if (pos == -1 and zt <= p.exit_z) or (pos == 1 and zt >= -p.exit_z):
                        reason = "수렴"
                    elif (pos == -1 and zt >= p.stop_z) or (pos == 1 and zt <= -p.stop_z):
                        reason = "손절"
                if reason is None and k - k0 >= p.max_hold:
                    reason = "시간"
                if reason is None and (mins[k] >= fx or k == len(sub) - 1):
                    reason = "장마감"
                if reason:
                    # pos=+1: 삼성 매수·하이닉스 매도 보유 → 청산은 삼성 매도·하이닉스 매수
                    xs = cs[k] - pos * slip * tick_s
                    xh = ch[k] + pos * slip * tick_h
                    pnl = pos * (xs - es_px) * qs * ms - pos * (xh - eh_px) * qh * mh
                    trades.append({"date": d, "entry": hh[k0], "exit": hh[k], "side": pos,
                                   "bars": int(k - k0), "reason": reason, "pnl": round(pnl)})
                    pos = 0
                    if reason == "손절":
                        cool = k + p.cooldown
                continue
            if k <= cool or not (es <= mins[k] <= ee):
                continue
            if p.require_fresh and not fresh[k]:
                continue
            if not (math.isfinite(zt) and math.isfinite(zp)):
                continue
            sig = 0
            if entry == "immediate":
                if zt >= p.entry_z > zp:
                    sig = -1
                elif zt <= -p.entry_z < zp:
                    sig = 1
            else:
                if zp > p.entry_z >= zt > p.exit_z:
                    sig = -1
                elif zp < -p.entry_z <= zt < -p.exit_z:
                    sig = 1
            if sig:
                pos, k0 = sig, k
                es_px = cs[k] + sig * slip * tick_s
                eh_px = ch[k] - sig * slip * tick_h
    return trades


def bt_summary(trades):
    import numpy as np

    if not trades:
        return {"trades": 0, "win_pct": float("nan"), "net": 0, "per_trade": float("nan"),
                "avg_win": float("nan"), "avg_loss": float("nan"), "reasons": ""}
    p = np.array([t["pnl"] for t in trades], float)
    w, l = p[p > 0], p[p < 0]
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {"trades": len(trades), "win_pct": round(100 * len(w) / len(p), 1), "net": int(p.sum()),
            "per_trade": int(p.mean()), "avg_win": int(w.mean()) if len(w) else 0,
            "avg_loss": int(l.mean()) if len(l) else 0,
            "reasons": " · ".join(f"{k} {v}" for k, v in reasons.items())}


def xcorr(a, b, lags):
    """corr(a_t, b_{t+L}). L>0 이면 'a 가 먼저, b 가 L분 뒤'."""
    import numpy as np

    out = []
    for L in lags:
        bb = b.shift(-L)
        m = a.notna() & bb.notna()
        n = int(m.sum())
        c = float(np.corrcoef(a[m], bb[m])[0, 1]) if n > 30 else float("nan")
        out.append((L, c, n))
    return out


def md_table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    def fmt(v):
        if isinstance(v, float):
            if math.isnan(v):
                return "—"
            return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"
        if isinstance(v, (int,)) and not isinstance(v, bool):
            return f"{v:,}"
        return str(v)

    head = "| " + " | ".join(c[1] for c in cols) + " |\n| " + " | ".join("---" for _ in cols) + " |\n"
    return head + "".join("| " + " | ".join(fmt(r.get(c[0], "")) for c in cols) + " |\n" for r in rows)


def verdict(value, good, warn, higher_is_better=True) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "판단 불가"
    if higher_is_better:
        return "통과" if value >= good else ("주의" if value >= warn else "미달")
    return "통과" if value <= good else ("주의" if value <= warn else "미달")


def cmd_analyze(args) -> None:
    import numpy as np
    import pandas as pd

    p = Params(**{k: v for k, v in vars(args).items() if k in Params.__dataclass_fields__ and v is not None})
    data = Path(args.data)
    bars = {"S": load_bars(data / "bars_S.csv"), "H": load_bars(data / "bars_H.csv")}
    has_fx = (data / "bars_FX.csv").exists()
    if has_fx:
        bars["FX"] = load_bars(data / "bars_FX.csv")
    meta = json.loads((data / "meta.json").read_text(encoding="utf-8")) if (data / "meta.json").exists() else {}
    legs_meta = meta.get("legs", {})

    f, days = build_frame(bars, p)
    if len(days) < 2:
        sys.exit("두 종목이 겹치는 날이 2일 이상 필요합니다.")
    nd = p.design_days or max(1, min(len(days) - 1, round(len(days) * 0.6)))
    d_days, v_days = days[:nd], days[nd:]
    dmask, vmask = f["date"].isin(d_days), f["date"].isin(v_days)
    allmask = f["date"].isin(days)

    out = Path(args.out) if args.out else Path("results") / f"analysis_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)

    # ① 유동성
    liq = liquidity_table({k: v for k, v in bars.items() if k in ("S", "H")}, f, days, p)
    liq.to_csv(out / "liquidity.csv", index=False, encoding="utf-8-sig")
    fresh_pct = 100 * float(f["fresh"].mean())

    # ② 관계
    beta, corr, nobs = ols(f.loc[dmask & f["fresh_ret"], "r_H"], f.loc[dmask & f["fresh_ret"], "r_S"])
    daily_beta = []
    for d in days:
        m = (f["date"] == d) & f["fresh_ret"]
        b, c, n = ols(f.loc[m, "r_H"], f.loc[m, "r_S"])
        daily_beta.append({"date": d, "set": "설계" if d in d_days else "검증", "beta": b, "corr": c, "n": n})
    db = np.array([x["beta"] for x in daily_beta if math.isfinite(x["beta"])])
    beta_cv = float(db.std() / abs(db.mean())) if len(db) > 1 and db.mean() else float("nan")
    if not math.isfinite(beta):
        sys.exit("β 를 추정할 수 없습니다(두 종목이 함께 체결된 봉이 너무 적음).")

    f, mu_fix, sd_fix = add_z(f, beta, dmask, p)

    # ④ 비용(다리마다 한 번 거래할 때 비용, 가격 대비 비율)
    ps, ph = float(f["c_S"].iloc[-1]), float(f["c_H"].iloc[-1])
    tick_s = args.tick_s or estimate_tick(bars["S"]["close"])
    tick_h = args.tick_h or estimate_tick(bars["H"]["close"])
    cost_src = f"가정: 다리마다 {p.slip_ticks:g}틱"
    c_s, c_h = p.slip_ticks * tick_s / ps, p.slip_ticks * tick_h / ph
    qpath = data / "quotes.csv"
    qstats = {}
    if qpath.exists():
        q = pd.read_csv(qpath)
        q = q[(q["bid"] > 0) & (q["ask"] > q["bid"])]
        for leg in ("S", "H"):
            sub = q[q["leg"] == leg]
            if len(sub):
                half = float(((sub["ask"] - sub["bid"]) / 2).median())
                mid = float(((sub["ask"] + sub["bid"]) / 2).median())
                qstats[leg] = {"n": len(sub), "half_spread": half, "mid": mid,
                               "spread_ticks_median": float(((sub["ask"] - sub["bid"]) /
                                                             (tick_s if leg == "S" else tick_h)).median())}
        if "S" in qstats and "H" in qstats:
            c_s, c_h = qstats["S"]["half_spread"] / qstats["S"]["mid"], qstats["H"]["half_spread"] / qstats["H"]["mid"]
            cost_src = f"실측: quotes.csv 호가 차의 절반(표본 S {qstats['S']['n']} · H {qstats['H']['n']})"
    cost_bp = 2 * (c_s + abs(beta) * c_h) * 1e4  # 진입+청산 × 두 다리, 스프레드(bp) 단위

    # ③ 괴리 사건 + 반감기
    ev_rows, all_events = [], {}
    for mode in ("rolling", "fixed"):
        for entry in ("immediate", "reentry"):
            for set_name, mask, dd in (("설계", dmask, d_days), ("검증", vmask, v_days)):
                ev = find_events(f, mode, entry, p, mask)
                all_events[(mode, entry, set_name)] = ev
                s = summarize_events(ev, len(dd), cost_bp)
                ev_rows.append({"variant": f"{mode}·{entry}", "set": set_name, **s})
    hl = {}
    for mode in ("rolling", "fixed"):
        for set_name, mask in (("설계", dmask), ("검증", vmask)):
            h, b, n = half_life(f, mode, mask)
            hl[(mode, set_name)] = (h, b, n)
    ev_all = []
    for (mode, entry, set_name), ev in all_events.items():
        for e in ev:
            ev_all.append({"variant": f"{mode}·{entry}", "set": set_name, **e})
    pd.DataFrame(ev_all).to_csv(out / "events.csv", index=False, encoding="utf-8-sig")

    # ⑤ 수량
    ms = float(args.mult_s or legs_meta.get("S", {}).get("mltplr") or 10)
    mh = float(args.mult_h or legs_meta.get("H", {}).get("mltplr") or 10)
    combos = size_combos(beta, ps, ph, ms, mh)
    qs, qh = (args.qs, args.qh) if args.qs and args.qh else (combos[0]["q_S"], combos[0]["q_H"])

    # ⑥ 백테스트
    bt_rows, daily_rows, trades_all = [], [], []
    for mode in ("rolling", "fixed"):
        for entry in ("immediate", "reentry"):
            for set_name, mask in (("설계", dmask), ("검증", vmask)):
                for slip in (0.0, p.slip_ticks):
                    tr = backtest(f, mode, entry, p, mask, qs, qh, ms, mh, tick_s, tick_h, slip)
                    bt_rows.append({"variant": f"{mode}·{entry}", "set": set_name,
                                    "cost": f"{slip:g}틱", **bt_summary(tr)})
                    if slip == p.slip_ticks:
                        for t in tr:
                            trades_all.append({"variant": f"{mode}·{entry}", "set": set_name, **t})
    tdf = pd.DataFrame(trades_all)
    tdf.to_csv(out / "trades_backtest.csv", index=False, encoding="utf-8-sig")
    if len(tdf):
        daily = tdf.groupby(["variant", "date"])["pnl"].agg(["count", "sum"]).reset_index()
        daily.to_csv(out / "daily_pnl.csv", index=False, encoding="utf-8-sig")

    # ⑦ 선행·후행
    ll_sh = xcorr(f["r_S"].where(f["fresh_ret"]), f["r_H"].where(f["fresh_ret"]), range(-5, 6))
    ll_fx = ll_fx_s = []
    if has_fx and "r_FX" in f:
        common = ((f["r_S"] + f["r_H"]) / 2).where(f["fresh_ret"])
        fxr = f["r_FX"].where(f["traded_FX"])
        ds = f.groupby("date")["s"].diff().where(f["fresh_ret"])
        ll_fx = xcorr(fxr, common, range(-10, 11))
        ll_fx_s = xcorr(fxr, ds, range(-10, 11))

    f.to_csv(out / "aligned_bars.csv", index=False, encoding="utf-8-sig")

    # 그림(선택)
    charts = make_charts(f, all_events, days, ll_sh, ll_fx, out, p)

    # ---------------- 보고서 ----------------
    main_key = ("rolling·reentry", "검증")
    main_ev = next((r for r in ev_rows if (r["variant"], r["set"]) == main_key), {})
    conv_gain = main_ev.get("mean_gain_converge_bp", float("nan"))
    stop_loss = main_ev.get("mean_loss_stop_bp", float("nan"))
    be = float("nan")
    if all(isinstance(x, float) and math.isfinite(x) for x in (conv_gain, stop_loss)) and conv_gain - cost_bp > 0:
        be = 100 * (abs(stop_loss) + cost_bp) / ((conv_gain - cost_bp) + (abs(stop_loss) + cost_bp))
    min_traded = float(liq["traded_pct"].min()) if len(liq) else float("nan")

    L = []
    w = L.append
    w("# 4주차 실행 전 분석 — 삼성전자·SK하이닉스 주식선물 페어\n")
    w(f"만든 시각 {datetime.now():%Y-%m-%d %H:%M} · 데이터 `{data}` · 환경 {meta.get('env', '?')}\n")
    for lab in ("S", "H", "FX"):
        if lab in legs_meta:
            m = legs_meta[lab]
            w(f"- **{lab}** `{m.get('sym')}` {m.get('sym_nm', '')} · 최종거래일 {m.get('lst_trd_dt', '?')} · 승수 {m.get('mltplr', '?')}")
    w(f"\n분석 날짜 {len(days)}일 — 설계용 {', '.join(d_days)} / 검증용 {', '.join(v_days) or '없음'}  ")
    w(f"분석 시간대 봉 시작 {p.session_start}~{p.session_end} · 진입 {p.entry_start}~{p.entry_end} · 정리 {p.force_exit}\n")

    w("## 0. 한눈에 보기 (판정 기준은 참고용)\n")
    summary_rows = [
        {"item": "① 유동성: 체결 있는 봉 비율(가장 낮은 날·다리)", "value": f"{min_traded:.1f}%",
         "verdict": verdict(min_traded, 70, 50)},
        {"item": "① 두 종목이 같은 분에 함께 체결된 비율", "value": f"{fresh_pct:.1f}%",
         "verdict": verdict(fresh_pct, 60, 40)},
        {"item": "② 1분 수익률 상관(설계)", "value": f"{corr:.2f}", "verdict": verdict(corr, 0.5, 0.3)},
        {"item": "② 날마다 β 흔들림(변동계수)", "value": f"{beta_cv:.2f}", "verdict": verdict(beta_cv, 0.3, 0.6, False)},
        {"item": "③ 괴리 사건 수/일 (rolling·reentry, 검증)", "value": f"{main_ev.get('per_day', 0)}",
         "verdict": verdict(float(main_ev.get("per_day", 0)), 3, 1)},
        {"item": "③ 수렴 비율 (rolling·reentry, 검증)", "value": f"{main_ev.get('converge_pct', '—')}%",
         "verdict": "아래 손익분기와 비교"},
        {"item": "④ 수렴 폭 ÷ 왕복 비용", "value": (f"{conv_gain / cost_bp:.2f}배" if math.isfinite(conv_gain) and cost_bp else "—"),
         "verdict": verdict(conv_gain / cost_bp if math.isfinite(conv_gain) and cost_bp else float("nan"), 1.5, 1.0)},
        {"item": "④ 비용 반영 손익분기 수렴 비율", "value": f"{be:.0f}%" if math.isfinite(be) else "—",
         "verdict": ("통과" if math.isfinite(be) and main_ev.get("converge_pct", 0) >= be
                     else "미달" if math.isfinite(be)
                     else "미달 (수렴해도 비용을 못 넘음)" if math.isfinite(conv_gain) else "판단 불가")},
        {"item": "④ 1틱 크기 (삼성 / 하이닉스)", "value": f"{tick_s / ps * 1e4:.1f}bp / {tick_h / ph * 1e4:.1f}bp",
         "verdict": "참고 — 1틱이 크면 비용이 결과를 정함"},
    ]
    w(md_table(summary_rows, [("item", "확인 항목"), ("value", "값"), ("verdict", "판정")]))
    w("\n판정 기준: 유동성 70%↑ 통과·50%↑ 주의 / 동시 체결 60%↑·40%↑ / 상관 0.5↑·0.3↑ / β 변동계수 0.3↓·0.6↓ / "
      "사건 3회↑·1회↑ / 수렴폭÷비용 1.5배↑·1배↑. **임의로 정한 출발점**이며 보고서에는 값 자체를 쓰세요.\n")

    w("## 1. 유동성 — 체결 없는 봉(가짜 괴리 위험)\n")
    w(md_table(liq.to_dict("records"), [("leg", "다리"), ("date", "날짜"), ("grid", "격자 봉"),
                                         ("bars", "받은 봉"), ("traded_pct", "체결 있는 봉 %"),
                                         ("median_vol", "봉 거래량 중앙값"), ("longest_gap_min", "최장 무체결(분)")]))
    w(f"\n두 종목이 **같은 분에 함께 체결**된 봉 {fresh_pct:.1f}%. 한쪽만 체결된 봉에서 계산한 괴리는 "
      "다른 쪽 가격이 멈춰 있어 생긴 가짜일 수 있어, 진입은 함께 체결된 봉에서만 허용했습니다"
      f"(`require_fresh={p.require_fresh}`).\n")

    w("## 2. 관계 — 헤지비율 β (1분 로그수익률 회귀, r_S = a + β·r_H)\n")
    w(f"- 설계 구간 β = **{beta:.3f}** · 상관 {corr:.2f} · 관측 {nobs:,}개 (두 봉 연속 함께 체결된 분만)")
    w(f"- 스프레드 s = ln(삼성) − {beta:.3f} × ln(하이닉스). 고정 방식 μ = {mu_fix:.5f}, σ = {sd_fix * 1e4:.1f}bp\n")
    w(md_table(daily_beta, [("date", "날짜"), ("set", "구간"), ("beta", "β"), ("corr", "상관"), ("n", "관측")]))
    w(f"\n날마다 β 변동계수 {beta_cv:.2f}. 크면 「고정 β」 가정이 실행 주간에 깨질 위험이 큽니다.\n")

    w("## 3. 괴리 사건 — 벌어진 뒤 되돌아왔나\n")
    w(f"진입 |z| ≥ {p.entry_z} · 수렴 |z| ≤ {p.exit_z} · 손절 |z| ≥ {p.stop_z} · 최대 {p.max_hold}봉 · "
      f"rolling 창 {p.window}봉(하루 안에서만). 이득(bp)은 비용 전 스프레드 변화, 손익 방향으로 부호를 맞춤.\n")
    w("- **rolling**: 최근 N봉 평균·표준편차 기준(2주차 볼린저와 같은 방식) · **fixed**: 설계 구간에서 정한 μ·σ 를 고정")
    w("- **immediate**: |z| 가 기준을 넘는 봉에서 진입 · **reentry**: 넘었다가 다시 안으로 들어온 봉에서 진입\n")
    w(md_table(ev_rows, [("variant", "변형"), ("set", "구간"), ("events", "사건"), ("per_day", "일평균"),
                         ("converge_pct", "수렴%"), ("stop_pct", "손절%"), ("time_eod_pct", "시간·마감%"),
                         ("median_bars_to_converge", "수렴까지 봉(중앙)"), ("mean_gain_converge_bp", "수렴 이득bp"),
                         ("mean_loss_stop_bp", "손절 손실bp"), ("mean_gain_bp", "평균bp"),
                         ("mean_net_bp", "비용 뒤 평균bp"), ("stale_entry_pct", "한쪽 무체결 진입%")]))
    w("\n반감기(AR(1), 하루 안에서만 추정):\n")
    w(md_table([{"mode": m, "set": s, "hl": h, "b": b, "n": n} for (m, s), (h, b, n) in hl.items()],
               [("mode", "방식"), ("set", "구간"), ("hl", "반감기(분)"), ("b", "b 계수"), ("n", "관측")]))
    w("\n반감기가 최대 보유(봉)보다 훨씬 길면 시간 청산이 잦아집니다. fixed 의 반감기가 매우 길거나 없으면 "
      "날을 넘는 수준 이동이 커서 고정 μ 가 맞지 않는다는 뜻입니다.\n")

    w("## 4. 비용 — 수렴 폭이 네 번의 비용을 넘나\n")
    w(f"- 비용 근거: {cost_src}")
    w(f"- 틱(추정): 삼성 {tick_s:g} · 하이닉스 {tick_h:g} — 분봉 종가의 최소 가격 간격. 실제 호가단위와 다르면 "
      "`--tick-s` `--tick-h` 로 넣으세요.")
    if qstats:
        for leg, q in qstats.items():
            w(f"- 호가 차 실측 {leg}: 절반 {q['half_spread']:g}원 (중앙값, 틱으로 {q['spread_ticks_median']:.1f}틱 폭)")
    w(f"- 1틱 = 삼성 가격의 {tick_s / ps * 1e4:.1f}bp · 하이닉스 가격의 {tick_h / ph * 1e4:.1f}bp")
    w(f"- 다리마다 한 번 거래 비용: 삼성 {c_s * 1e4:.2f}bp · 하이닉스 {c_h * 1e4:.2f}bp")
    w(f"- **왕복 비용(진입·청산 × 두 다리) = 스프레드 {cost_bp:.2f}bp** = 2 × (c_S + |β|·c_H)")
    if math.isfinite(be):
        w(f"- rolling·reentry 검증: 수렴 이득 {conv_gain:.2f}bp · 손절 손실 {stop_loss:.2f}bp → "
          f"비용 반영 손익분기 수렴 비율 **약 {be:.0f}%** (실제 {main_ev.get('converge_pct')}%)")
    w("")

    w("## 5. 수량 — 정수 계약 조합\n")
    w(f"최근 종가 삼성 {ps:,.0f} · 하이닉스 {ph:,.0f} · 승수 {ms:g} / {mh:g}. 목표: 하이닉스 명목금액 ≈ |β| × 삼성 명목금액\n")
    w(md_table(combos, [("q_S", "삼성 계약"), ("q_H", "하이닉스 계약"), ("notional_S", "삼성 명목(원)"),
                        ("notional_H", "하이닉스 명목(원)"), ("hedge_error_pct", "헤지 오차 %")]))
    w(f"\n백테스트에 쓴 조합: 삼성 {qs} · 하이닉스 {qh}. 증거금은 모의계좌 `orderable-quantity` 로 따로 확인하세요.\n")

    w("## 6. 백테스트 — 정수 계약, 설계/검증 분리\n")
    w(f"봉 마감 종가로 판정·체결, 비용 있는 줄은 다리마다 {p.slip_ticks:g}틱 불리하게 체결. 진입은 두 종목 함께 체결된 봉만, "
      f"손절 뒤 {p.cooldown}봉 대기, 동시 1페어. 수수료 0원.\n")
    w(md_table(bt_rows, [("variant", "변형"), ("set", "구간"), ("cost", "비용"), ("trades", "거래"),
                         ("win_pct", "승률%"), ("net", "순손익(원)"), ("per_trade", "건당(원)"),
                         ("avg_win", "평균 이익"), ("avg_loss", "평균 손실"), ("reasons", "청산 사유")]))
    w("\n**설계 구간에서 가장 좋은 변형을 고르고, 검증 구간에서 그대로 유지되는지**로 판단하세요. "
      "검증에서 뒤집히면 과최적화입니다(2주차 V5 사례).\n")

    w("## 7. 선행·후행 (상관, 관측 n 일 때 대략 ±2/√n 밖이면 우연보다 큼)\n")
    w("**삼성 수익률(t) vs 하이닉스 수익률(t+L)** — L>0 에서 크면 삼성이 먼저, L<0 에서 크면 하이닉스가 먼저\n")
    w(md_table([{"L": L_, "c": c, "n": n, "band": 2 / math.sqrt(n) if n else float("nan")} for L_, c, n in ll_sh],
               [("L", "시차 L(분)"), ("c", "상관"), ("n", "관측"), ("band", "±2/√n")]))
    if ll_fx:
        w("\n**원/달러 선물 수익률(t) vs 반도체 공통 수익률(t+L)** — L>0 에서 크면 환율이 먼저(최종 주제의 질문)\n")
        w(md_table([{"L": L_, "c": c, "n": n, "c2": c2} for (L_, c, n), (_, c2, _) in zip(ll_fx, ll_fx_s)],
                   [("L", "시차 L(분)"), ("c", "환율→공통 상관"), ("c2", "환율→스프레드 변화 상관"), ("n", "관측")]))
        w("\n환율이 오르면(원화 약세) 수출주에 유리하다는 통념이면 L=0 근처 상관이 양수여야 합니다. "
          "스프레드 변화와의 상관이 0 에 가까우면 「환율 영향은 업종 공통 부분에 있고 두 회사 차이와는 무관」을 뜻합니다.\n")
    else:
        w("\n(원/달러 선물 분봉이 없어 환율 시차 분석은 건너뜀 — fetch 에 `--fx-sym` 을 주면 생깁니다.)\n")

    if charts:
        w("## 그림\n")
        for c in charts:
            w(f"![{c}]({c})")
        w("")
    w("## 파일\n")
    w("`liquidity.csv` 유동성 · `events.csv` 괴리 사건 전부 · `trades_backtest.csv` 백테스트 거래(비용 있음) · "
      "`daily_pnl.csv` 날짜별 손익 · `aligned_bars.csv` 시각 맞춘 봉·스프레드·z · `params.json` 설정값\n")

    (out / "analysis.md").write_text("\n".join(L), encoding="utf-8")
    (out / "params.json").write_text(json.dumps({**asdict(p), "beta": beta, "mu_fixed": mu_fix, "sd_fixed": sd_fix,
                                                 "tick_s": tick_s, "tick_h": tick_h, "cost_bp": cost_bp,
                                                 "q_S": qs, "q_H": qh, "mult_s": ms, "mult_h": mh,
                                                 "design_days": d_days, "validation_days": v_days},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\n보고서 → {out / 'analysis.md'}")
    log(md_table(summary_rows, [("item", "확인 항목"), ("value", "값"), ("verdict", "판정")]))


def make_charts(f, all_events, days, ll_sh, ll_fx, out: Path, p: Params) -> list[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    for fam in ("Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR"):
        try:
            from matplotlib import font_manager

            if any(fam in x.name for x in font_manager.fontManager.ttflist):
                plt.rcParams["font.family"] = fam
                break
        except Exception:
            pass
    plt.rcParams["axes.unicode_minus"] = False
    made = []
    show = days[-min(len(days), 6):]
    fig, axes = plt.subplots(len(show), 1, figsize=(11, 2.3 * len(show)), sharex=False, squeeze=False)
    ev = all_events.get(("rolling", "reentry", "설계"), []) + all_events.get(("rolling", "reentry", "검증"), [])
    for ax, d in zip(axes[:, 0], show):
        sub = f[f["date"] == d]
        ax.plot(sub["minute"] / 60, sub["z_rolling"], lw=0.8, color="#3060a0")
        for y in (p.entry_z, -p.entry_z):
            ax.axhline(y, color="#c04040", lw=0.6, ls="--")
        for y in (p.stop_z, -p.stop_z):
            ax.axhline(y, color="#808080", lw=0.5, ls=":")
        ax.axhline(0, color="#999", lw=0.5)
        for e in [e for e in ev if e["date"] == d]:
            h, m = map(int, e["time"].split(":"))
            col = {"converge": "#2a8a3a", "stop": "#c04040"}.get(e["outcome"], "#d08a20")
            ax.scatter([(h * 60 + m) / 60], [e["z_entry"]], color=col, s=18, zorder=3)
        ax.set_ylabel(d, fontsize=8)
        ax.set_ylim(-5, 5)
    axes[0, 0].set_title("rolling z (점: reentry 진입, 초록=수렴 빨강=손절 주황=시간·마감)", fontsize=9)
    axes[-1, 0].set_xlabel("시각(시)")
    fig.tight_layout()
    fig.savefig(out / "z_by_day.png", dpi=120)
    plt.close(fig)
    made.append("z_by_day.png")

    fig, ax = plt.subplots(1, 2 if ll_fx else 1, figsize=(10 if ll_fx else 5.5, 3), squeeze=False)
    Ls, cs = [x[0] for x in ll_sh], [x[1] for x in ll_sh]
    ax[0, 0].bar(Ls, cs, color="#3060a0")
    n = max((x[2] for x in ll_sh), default=0)
    if n:
        for y in (2 / math.sqrt(n), -2 / math.sqrt(n)):
            ax[0, 0].axhline(y, color="#c04040", lw=0.6, ls="--")
    ax[0, 0].set_title("삼성(t) vs 하이닉스(t+L)", fontsize=9)
    if ll_fx:
        ax[0, 1].bar([x[0] for x in ll_fx], [x[1] for x in ll_fx], color="#2a8a3a")
        n2 = max((x[2] for x in ll_fx), default=0)
        if n2:
            for y in (2 / math.sqrt(n2), -2 / math.sqrt(n2)):
                ax[0, 1].axhline(y, color="#c04040", lw=0.6, ls="--")
        ax[0, 1].set_title("원/달러(t) vs 반도체 공통(t+L)", fontsize=9)
    fig.tight_layout()
    fig.savefig(out / "lead_lag.png", dpi=120)
    plt.close(fig)
    made.append("lead_lag.png")
    return made


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="4주차 삼성전자·SK하이닉스 주식선물 페어 실행 전 분석")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("find", help="종목 마스터에서 주식선물 코드 찾기")
    a.add_argument("names", nargs="+")
    a.add_argument("--spread", action="store_true", help="같은 종목 월물 스프레드 종목도 표시")
    a.set_defaults(func=cmd_find)

    for name, fn, hp in (("fetch", cmd_fetch, "1분봉 수집"), ("quotes", cmd_quotes, "호가 차 표본 조사")):
        a = sub.add_parser(name, help=hp)
        a.add_argument("--sym-s", required=True, help="삼성전자 선물 종목코드")
        a.add_argument("--sym-h", required=True, help="SK하이닉스 선물 종목코드")
        a.add_argument("--env", choices=("paper", "real"), default="paper")
        a.add_argument("--base-url", help="시험용 접속 주소(가짜 서버)")
        a.add_argument("--data", default="data")
        if name == "fetch":
            a.add_argument("--days", type=int, default=10, help="최근 몇 영업일(주말만 건너뜀)")
            a.add_argument("--end", help="마지막 날 YYYYMMDD (기본 오늘)")
            a.add_argument("--fx-sym", help="관찰용 미국달러 선물 코드 (예: A756C000)")
        else:
            a.add_argument("--minutes", type=float, default=10)
            a.add_argument("--every", type=float, default=5, help="몇 초마다")
        a.set_defaults(func=fn)

    a = sub.add_parser("analyze", help="분석 보고서 만들기")
    a.add_argument("--data", default="data")
    a.add_argument("--out")
    a.add_argument("--design-days", type=int, dest="design_days")
    a.add_argument("--window", type=int)
    a.add_argument("--entry-z", type=float, dest="entry_z")
    a.add_argument("--exit-z", type=float, dest="exit_z")
    a.add_argument("--stop-z", type=float, dest="stop_z")
    a.add_argument("--max-hold", type=int, dest="max_hold")
    a.add_argument("--slip-ticks", type=float, dest="slip_ticks")
    a.add_argument("--tick-s", type=float, dest="tick_s")
    a.add_argument("--tick-h", type=float, dest="tick_h")
    a.add_argument("--mult-s", type=float, dest="mult_s")
    a.add_argument("--mult-h", type=float, dest="mult_h")
    a.add_argument("--qs", type=int)
    a.add_argument("--qh", type=int)
    a.set_defaults(func=cmd_analyze)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
