"""NH선물 Open API — 해외선물 호출부.

2주차 nh_client.py 와 같은 원칙(본문으로 성공 판정, 401·토큰 코드만 1회 재발급,
주문 재전송 금지, 호출마다 api_calls.csv 기록)을 해외 API 에 맞춰 옮긴 것.

해외가 국내와 다른 점 (참고 저장소 nh-futures-open-api 의 명세 정리에서 확인):
  - 경로가 /trade/v1/overseas/... , 키도 해외용 APP Key 를 따로 씀
  - 주문 거부 사유가 msg_cd 가 아니라 err_cd·err_msg 로 옴
  - 신규·정정·취소가 order 하나(qt_div_cd 1·2·3)
  - 현재가 요청은 배열이지만 한 번에 한 종목만 받음
  - 주문내역 조회구분 inq_div: 0 전체·1 주문·2 체결·3 미체결
"""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

BASE_URL = {"paper": "https://apidemo.futures.co.kr", "real": "https://api.futures.co.kr"}
TOKEN_PATH = "/auth-service/v1/token"

# api_id → (경로, 호출 등급)
SPECS = {
    "price": ("/trade/v1/overseas/price", "quote"),
    "bid-price": ("/trade/v1/overseas/bid-price", "quote"),
    "chart-minute": ("/trade/v1/overseas/chart-minute", "quote"),
    "chart-daily": ("/trade/v1/overseas/chart-daily", "quote"),
    "product-detail": ("/trade/v1/overseas/product-detail", "account"),
    "market-operations": ("/trade/v1/overseas/market-operations", "account"),
    "product-expiry": ("/trade/v1/overseas/product-expiry", "account"),
    "order": ("/trade/v1/overseas/order", "order"),
    "order-history": ("/trade/v1/overseas/order-history", "account"),
    "open-interest": ("/trade/v1/overseas/open-interest", "account"),
    "deposit": ("/trade/v1/overseas/deposit", "account"),
}
# 등급별 최소 간격(초): 시세·계좌 초당 10건, 주문 초당 5건보다 여유 있게
MIN_GAP = {"quote": 0.12, "account": 0.12, "order": 0.25}

OK_CODES = {"", "0", "EGW002", "EGW013"}


class ApiFailure(Exception):
    def __init__(self, api_id: str, code: str, message: str, body: Any = None):
        super().__init__(f"{api_id} 실패 ({code}) {message}")
        self.api_id, self.code, self.message, self.body = api_id, code, message, body


class NoResponse(Exception):
    """응답을 못 받음(시간 초과·연결 실패). 주문이면 처리 여부를 모름 → 재전송 금지."""


def num(v: Any) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _ok_code(code: str) -> bool:
    # 주문 성공 응답이 err_cd "00000"(「주문이 정상처리 되었습니다」)로 옴 → 0 만으로 된 코드는 정상
    return code in OK_CODES or set(code) == {"0"}


def failure_of(api_id: str, body: Any) -> tuple[str, str] | None:
    """HTTP 200 이어도 본문으로 실패를 판정."""
    if not isinstance(body, dict):
        return None
    code = str(body.get("errorCode") or "").strip()
    if code and not _ok_code(code):
        return code, str(body.get("errorMessage") or body.get("message") or "").strip()
    for c_field, m_field in (("err_cd", "err_msg"), ("msg_cd", "msg_cntn")):
        code = str(body.get(c_field) or "").strip()
        if code and not _ok_code(code):
            return code, str(body.get(m_field) or "").strip()
    return None


class OverseasClient:
    def __init__(self, env: str, app_key: str, app_secret: str, log_dir: Path,
                 timeout: float = 10.0, base_url: str | None = None,
                 token_cache: Path | None = None):
        if env not in BASE_URL:
            raise ValueError("env 는 paper 또는 real")
        self.env, self.app_key, self.app_secret = env, app_key, app_secret
        self.base = base_url or BASE_URL[env]
        self.timeout = timeout
        self.s = requests.Session()
        self.token: str = ""
        self.expires_at = datetime.min
        self.last_issue = datetime.min
        self.token_cache = token_cache or Path(f".token_cache_overseas_{env}.json")
        self.last_call: dict[str, float] = {}
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / "api_calls.csv"
        new = not self.log_path.exists()
        self._log_f = open(self.log_path, "a", newline="", encoding="utf-8")
        self._log = csv.writer(self._log_f)
        if new:
            self._log.writerow(["ts", "api", "http", "latency_ms", "ok", "code", "message"])
        self._load_cache()

    # ── 토큰 ──────────────────────────────────────────────
    def _load_cache(self) -> None:
        try:
            d = json.loads(self.token_cache.read_text(encoding="utf-8"))
            if d.get("key_tail") == self.app_key[-4:]:
                self.token = d["token"]
                self.expires_at = datetime.fromisoformat(d["expires_at"])
        except (OSError, ValueError, KeyError):
            pass

    def _issue(self) -> None:
        # 같은 키로 1분에 한 번만 (2주차 관찰 1: 만료 직전 반복 재발급 방지)
        if datetime.now() - self.last_issue < timedelta(minutes=1) and self.token:
            return
        t0 = time.time()
        try:
            r = self.s.post(self.base + TOKEN_PATH, timeout=self.timeout, data={
                "grant_type": "client_credentials", "appkey": self.app_key,
                "appsecret": self.app_secret})
        except requests.RequestException as e:
            self._write("token", "", t0, False, "NO_RESPONSE", str(e)[:120])
            raise NoResponse(f"토큰 발급 응답 없음: {e}") from e
        body = _json(r)
        ok = r.status_code < 400 and bool(body.get("access_token"))
        self._write("token", r.status_code, t0, ok, str(body.get("errorCode") or ""),
                    "" if ok else str(body)[:200])
        if not ok:
            raise ApiFailure("token", str(r.status_code), str(body)[:300], body)
        self.token = body["access_token"]
        self.last_issue = datetime.now()
        exp = str(body.get("expires_at") or "").strip()
        try:
            self.expires_at = datetime.fromisoformat(exp.replace("T", " ")) if exp else None
        except ValueError:
            self.expires_at = None
        if not self.expires_at:
            self.expires_at = datetime.now() + timedelta(seconds=int(num(body.get("expires_in")) or 3600))
        try:
            self.token_cache.write_text(json.dumps({
                "key_tail": self.app_key[-4:], "token": self.token,
                "expires_at": self.expires_at.isoformat()}), encoding="utf-8")
        except OSError:
            pass

    def _ensure_token(self) -> None:
        if not self.token or datetime.now() >= self.expires_at - timedelta(minutes=1):
            self._issue()

    # ── 호출 ──────────────────────────────────────────────
    def _write(self, api, http, t0, ok, code, msg) -> None:
        self._log.writerow([datetime.now().isoformat(timespec="milliseconds"), api, http,
                            int((time.time() - t0) * 1000), int(ok), code, msg])
        self._log_f.flush()

    def call(self, api_id: str, payload: dict[str, Any], retries: int = 0) -> dict[str, Any]:
        """retries: 조회 API 만 시간 초과 때 재시도. 주문은 반드시 0."""
        path, grade = SPECS[api_id]
        if grade == "order":
            retries = 0
        for attempt in range(retries + 1):
            try:
                return self._call_once(api_id, path, grade, payload)
            except NoResponse:
                if attempt >= retries:
                    raise
                time.sleep(1.0 + attempt)
        raise AssertionError("unreachable")

    def _call_once(self, api_id, path, grade, payload, refreshed=False):
        self._ensure_token()
        gap = MIN_GAP[grade] - (time.time() - self.last_call.get(grade, 0))
        if gap > 0:
            time.sleep(gap)
        self.last_call[grade] = time.time()
        t0 = time.time()
        try:
            r = self.s.post(self.base + path, json=payload, timeout=self.timeout,
                            headers={"Authorization": f"Bearer {self.token}"})
        except requests.RequestException as e:
            self._write(api_id, "", t0, False, "NO_RESPONSE", type(e).__name__)
            raise NoResponse(f"{api_id}: {e}") from e
        body = _json(r)
        fail = failure_of(api_id, body)
        token_problem = r.status_code == 401 or (fail and fail[0].startswith("EGW05"))
        if token_problem and not refreshed:
            # 401·토큰 코드는 처리 전 거절 → 주문도 중복 없이 한 번만 다시
            self._write(api_id, r.status_code, t0, False, fail[0] if fail else "401", "token refresh")
            self.token, self.last_issue = "", datetime.min
            return self._call_once(api_id, path, grade, payload, refreshed=True)
        if r.status_code >= 400 and not fail:
            fail = (str(r.status_code), str(body)[:200])
        self._write(api_id, r.status_code, t0, fail is None, fail[0] if fail else "",
                    fail[1][:200] if fail else "")
        if fail:
            raise ApiFailure(api_id, fail[0], fail[1], body)
        return body

    # ── 명세 함수 (요청 칸 이름은 명세 그대로) ─────────────
    def price(self, exch_cd: str, sym: str) -> dict[str, Any]:
        body = self.call("price", {"Iccurs1": [{"iccurs1_exch_cd": exch_cd, "iccurs1_sym": sym}]}, retries=2)
        rows = body.get("Occurs1") or []
        return rows[0] if rows else {}

    def chart_minute(self, exch_cd: str, sym: str, start_dt: str, end_dt: str,
                     next_key: str = "99999999", dcnt: str = "1") -> dict[str, Any]:
        return self.call("chart-minute", {
            "exch_cd": exch_cd, "sym": sym, "inq_strt_dt": start_dt, "strt_tm": "000000",
            "inq_ed_dt": end_dt, "ed_tm": "235959", "req_qty": "300", "dcnt": dcnt,
            "cidx_yn": "0", "nxt_key": next_key}, retries=2)

    def product_detail(self, prd_cd: str = "", exch_cd: str = "") -> list[dict[str, Any]]:
        body = self.call("product-detail", {"cur_cd": "", "exch_cd": exch_cd, "fut_opt_div_cd": "",
                                            "nxt_key": "", "prd_cd": prd_cd, "prd_grp_cd": "",
                                            "prd_nm": "", "sett_cd": ""}, retries=2)
        return body.get("Occurs1") or []

    def market_operations(self, prd_cd: str = "", exch_cd: str = "") -> list[dict[str, Any]]:
        body = self.call("market-operations", {"fut_opt_div_cd": "", "prd_grp_cd": "",
                                               "exch_cd": exch_cd, "cur_cd": "", "prd_cd": prd_cd,
                                               "prd_nm": "", "nxt_key": ""}, retries=2)
        return body.get("Occurs1") or []

    def order_market(self, sym: str, side: str, qty: int = 1) -> dict[str, Any]:
        """시장가 신규주문. side: 'buy'|'sell'. 응답 없으면 NoResponse — 재전송 금지."""
        return self.call("order", {
            "qt_div_cd": "1", "sym": sym, "trd_div_cd": "1" if side == "buy" else "2",
            "glb_ord_tp_cd": "1", "ord_qty": str(qty), "ord_pric": "0", "stop_pric": "",
            "org_ord_no": "0"})

    def order_limit(self, sym: str, side: str, price: str, qty: int = 1) -> dict[str, Any]:
        """지정가 신규주문. HKEX 는 시장가를 받지 않음(오류 7032) → 체결될 만한 가격의 지정가로 대신."""
        return self.call("order", {
            "qt_div_cd": "1", "sym": sym, "trd_div_cd": "1" if side == "buy" else "2",
            "glb_ord_tp_cd": "2", "ord_qty": str(qty), "ord_pric": price, "stop_pric": "",
            "org_ord_no": "0"})

    def cancel(self, sym: str, org_ord_no: str, qty: int, side: str) -> dict[str, Any]:
        return self.call("order", {
            "qt_div_cd": "3", "sym": sym, "trd_div_cd": "1" if side == "buy" else "2",
            "glb_ord_tp_cd": "2", "ord_qty": str(qty), "ord_pric": "0", "stop_pric": "",
            "org_ord_no": org_ord_no})

    def order_history(self, inq_div: str = "0") -> list[dict[str, Any]]:
        rows, key = [], ""
        for _ in range(10):
            body = self.call("order-history", {"inq_div": inq_div, "trd_div_cd": "0", "nxt_key": key}, retries=1)
            rows += body.get("Occurs1") or []
            key = str(body.get("nxt_key") or "").strip()
            if not key:
                break
        return rows

    def open_interest(self) -> list[dict[str, Any]]:
        rows, key = [], ""
        for _ in range(10):
            body = self.call("open-interest", {"nxt_key": key}, retries=1)
            rows += body.get("Occurs1") or []
            key = str(body.get("nxt_key") or "").strip()
            if not key:
                break
        return rows

    def net_position(self, sym: str) -> int:
        """잔고의 순포지션(매수 +, 매도 −)."""
        net = 0
        for r in self.open_interest():
            if str(r.get("occurs1_sym") or "").strip() != sym:
                continue
            q = int(num(r.get("occurs1_opint_qty")))
            net += q if str(r.get("occurs1_trd_tp")).strip() == "1" else -q
        return net


def _json(r: requests.Response) -> dict[str, Any]:
    try:
        b = r.json()
        return b if isinstance(b, dict) else {"_raw": b}
    except ValueError:
        return {"_text": r.text[:300]}


def keys_from_env() -> tuple[str, str]:
    k = os.environ.get("NH_OVS_APP_KEY", "").strip()
    s = os.environ.get("NH_OVS_APP_SECRET", "").strip()
    if not k or not s:
        raise SystemExit("환경변수 NH_OVS_APP_KEY · NH_OVS_APP_SECRET 에 해외 모의 APP Key 를 넣어 주세요.")
    return k, s
