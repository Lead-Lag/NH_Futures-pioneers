"""NH선물 Open API (국내 선물) REST 호출부.

- 접근토큰 발급(form) 과 캐시(.token_cache.json)
- 호출 제한 간격(시세·계좌 초당 10건, 주문 초당 5건) 유지
- 성공 판정: HTTP 200 이어도 본문의 errorCode / msg_cd 가 있으면 실패로 봅니다.
- 주문은 **절대 자동 재전송하지 않습니다.** 응답을 못 받으면 주문내역으로 확인합니다.
- 모든 호출을 api_calls.csv 로 남깁니다(이름, 상태, 지연, 오류코드).

경로와 필드명은 NH선물 공개 저장소(PetterChanHyuk/nh-futures-open-api)의
catalog.py · domestic_futureoption_functions.py 와 실제 응답 기록(tests/fixtures/nh)에서 옮겼습니다.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

KST = timezone(timedelta(hours=9))  # Windows 에서 tzdata 없이도 동작하도록 고정 오프셋

BASE_URLS = {
    "paper": "https://apidemo.futures.co.kr",  # 모의
    "real": "https://api.futures.co.kr",  # 실전
}

TOKEN_PATH = "/auth-service/v1/token"

# api 이름 -> (경로, 제한 등급)
APIS: dict[str, tuple[str, str]] = {
    "domestic_price": ("/trade/v1/domestic/price", "quote"),
    "domestic_price-bid": ("/trade/v1/domestic/price-bid", "quote"),
    "domestic_chart-minute": ("/trade/v1/domestic/chart-minute", "quote"),
    "domestic_chart-daily": ("/trade/v1/domestic/chart-daily", "quote"),
    "domestic_product-detail": ("/trade/v1/domestic/product-detail", "account"),
    "domestic_order": ("/trade/v1/domestic/order", "order"),
    "domestic_modify": ("/trade/v1/domestic/modify", "order"),
    "domestic_cancel": ("/trade/v1/domestic/cancel", "order"),
    "domestic_order-history": ("/trade/v1/domestic/order-history", "account"),
    "domestic_open-interest": ("/trade/v1/domestic/open-interest", "account"),
}

MIN_INTERVAL = {"quote": 0.12, "account": 0.12, "order": 0.25}

# 조회 API 는 응답을 못 받으면 몇 번 더 물어봅니다(같은 값을 다시 묻는 것이라 안전).
# 주문 계열은 **절대** 다시 보내지 않습니다 — 접수됐는지 모르는 주문이 두 번 나갈 수 있습니다.
ORDER_APIS = {"domestic_order", "domestic_modify", "domestic_cancel"}
READ_RETRIES = 3  # 첫 시도 뒤 추가로 몇 번
READ_BACKOFF = (1.0, 2.0, 4.0)  # 초

# 정상인데도 errorCode 가 실려 오는 값 (공개 저장소 error_codes.OK_CODES)
OK_CODES = {"EGW002", "EGW013", "0"}

FIRST_PAGE = "99999999"  # 차트 연속조회 첫 장


class NHApiError(Exception):
    """API 호출 실패의 공통 부모."""

    def __init__(self, api: str, message: str, code: str = "", body: Any = None):
        super().__init__(f"[{api}] {code} {message}".strip())
        self.api = api
        self.code = code
        self.body = body


class TransportError(NHApiError):
    """응답을 받지 못함(시간 초과·연결 실패). 주문이면 접수 여부를 알 수 없음."""


class BusinessError(NHApiError):
    """서버가 받아서 거부함(msg_cd / errorCode)."""


@dataclass
class Credentials:
    environment: str  # paper | real
    app_key: str
    app_secret: str
    base_url: str = ""

    @property
    def url(self) -> str:
        return (self.base_url or BASE_URLS[self.environment]).rstrip("/")


def load_credentials(path: str | None, environment: str | None = None) -> Credentials:
    """config.local.json → 환경변수 순서로 읽습니다. 키는 코드·결과에 남기지 않습니다."""
    data: dict[str, Any] = {}
    if path and Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    env = environment or data.get("environment") or os.getenv("NH_ENV") or "paper"
    section = data.get(env, {}) if isinstance(data.get(env), dict) else {}
    key = section.get("app_key") or os.getenv("NH_APP_KEY", "")
    secret = section.get("app_secret") or os.getenv("NH_APP_SECRET", "")
    base = section.get("base_url") or os.getenv("NH_BASE_URL", "")
    if not key or not secret:
        raise SystemExit(
            f"[{env}] APP Key / Secret 이 없습니다. config.local.json 의 {env} 칸이나 "
            "환경변수 NH_APP_KEY / NH_APP_SECRET 을 채워 주세요."
        )
    return Credentials(env, key, secret, base)


class NHClient:
    def __init__(
        self,
        creds: Credentials,
        *,
        token_cache: Path | None = None,
        call_logger: Callable[[dict[str, Any]], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.creds = creds
        self.http = requests.Session()
        self.timeout = timeout
        self.token_cache = token_cache
        self.call_logger = call_logger
        self._token: str | None = None
        self._token_type = "Bearer"
        self._expires: datetime | None = None
        self._last_call: dict[str, float] = {}
        self._issued_at = datetime.min.replace(tzinfo=KST)

    # ------------------------------------------------------------ 인증
    def _fingerprint(self) -> str:
        raw = f"{self.creds.environment}:{self.creds.app_key}:{self.creds.url}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _load_cached_token(self) -> bool:
        if not self.token_cache or not self.token_cache.exists():
            return False
        try:
            saved = json.loads(self.token_cache.read_text(encoding="utf-8"))
            item = saved.get(self._fingerprint())
            if not item:
                return False
            expires = datetime.fromisoformat(item["expires_at"])
            if datetime.now(KST) + timedelta(minutes=2) >= expires:
                return False
            self._token, self._token_type, self._expires = item["token"], item["type"], expires
            return True
        except (ValueError, KeyError, OSError):
            return False

    def _save_token(self) -> None:
        if not self.token_cache:
            return
        try:
            saved = {}
            if self.token_cache.exists():
                saved = json.loads(self.token_cache.read_text(encoding="utf-8"))
            saved[self._fingerprint()] = {
                "token": self._token,
                "type": self._token_type,
                "expires_at": self._expires.isoformat() if self._expires else "",
            }
            self.token_cache.write_text(json.dumps(saved), encoding="utf-8")
        except OSError:
            pass

    def _issue_token(self) -> None:
        url = self.creds.url + TOKEN_PATH
        started = time.perf_counter()
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.creds.app_key,
            "appsecret": self.creds.app_secret,
        }
        try:
            # 토큰 발급만 form 형식(JSON 으로 보내면 「필수 파라미터 누락」)
            r = self.http.post(url, data=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            self._log("token", None, started, "TRANSPORT", str(exc))
            raise TransportError("token", f"토큰 발급 요청 실패: {exc}") from exc
        body = _json_or_text(r)
        if r.status_code >= 400 or not isinstance(body, dict) or not body.get("access_token"):
            code = str(body.get("errorCode", r.status_code)) if isinstance(body, dict) else str(r.status_code)
            self._log("token", r.status_code, started, code, str(body)[:200])
            raise BusinessError("token", f"토큰 발급 거부: {body}", code, body)
        self._log("token", r.status_code, started, "", "")
        self._token = body["access_token"]
        self._token_type = str(body.get("token_type") or "Bearer")
        self._expires = _parse_expiry(body)
        self._issued_at = datetime.now(KST)
        self._save_token()

    def ensure_token(self) -> None:
        now = datetime.now(KST)
        if self._token and self._expires and now + timedelta(minutes=1) < self._expires:
            return
        # 만료 1분 전부터 새로 받는데, 서버가 만료 전에는 **같은 토큰(같은 만료 시각)**을 돌려줄 수
        # 있습니다(10/28 실행에서 14:02~14:03 사이 5초마다 14번 발급 확인). 방금 받았는데 아직
        # 만료 전이면 그대로 쓰고, 다시 받는 것은 1분에 한 번까지만 합니다.
        if (self._token and self._expires and now < self._expires
                and now - self._issued_at < timedelta(minutes=1)):
            return
        if not self._load_cached_token():
            self._issue_token()

    # ------------------------------------------------------------ 호출
    def call(self, api: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if api in ORDER_APIS:
            return self._call_once(api, payload)
        for attempt in range(READ_RETRIES + 1):
            try:
                return self._call_once(api, payload)
            except TransportError:
                if attempt >= READ_RETRIES:
                    raise
                time.sleep(READ_BACKOFF[min(attempt, len(READ_BACKOFF) - 1)])
        raise AssertionError("unreachable")

    def _call_once(self, api: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        path, grade = APIS[api]
        self._throttle(grade)
        self.ensure_token()
        r, body, started = self._send(api, path, payload or {})
        trouble = _failure(body)
        if r is not None and (r.status_code == 401 or (trouble and trouble[0].startswith("EGW05"))):
            # 토큰 거절: 한 번만 새로 받아 재시도. 401 은 처리 전 거절이라 주문도 중복되지 않음.
            self._log(api, r.status_code, started, trouble[0] if trouble else "401", "token rejected → reissue")
            self._token = None
            if self.token_cache and self.token_cache.exists():
                self.token_cache.unlink(missing_ok=True)
            self._issue_token()
            r, body, started = self._send(api, path, payload or {})
            trouble = _failure(body)
        if trouble:
            self._log(api, r.status_code, started, trouble[0], trouble[1])
            raise BusinessError(api, trouble[1], trouble[0], body)
        if r.status_code >= 400:
            self._log(api, r.status_code, started, f"HTTP{r.status_code}", str(body)[:200])
            raise NHApiError(api, f"HTTP {r.status_code}: {str(body)[:200]}", f"HTTP{r.status_code}", body)
        self._log(api, r.status_code, started, "", "")
        return body

    def _send(self, api: str, path: str, payload: dict[str, Any]):
        started = time.perf_counter()
        headers = {"Authorization": f"{self._token_type} {self._token}"}
        try:
            r = self.http.post(self.creds.url + path, headers=headers, json=payload, timeout=self.timeout)
        except requests.Timeout as exc:
            self._log(api, None, started, "TIMEOUT", str(exc))
            if api in ORDER_APIS:
                raise TransportError(api, "응답 시간 초과 — 주문 접수 여부 불명, 주문내역으로 확인 필요") from exc
            raise TransportError(api, f"응답 시간 초과({self.timeout:.0f}초) — 조회라서 다시 시도") from exc
        except requests.RequestException as exc:
            self._log(api, None, started, "TRANSPORT", str(exc))
            raise TransportError(api, f"연결 실패(인터넷·DNS 확인): {type(exc).__name__}") from exc
        return r, _json_or_text(r), started

    def _throttle(self, grade: str) -> None:
        gap = MIN_INTERVAL[grade] - (time.monotonic() - self._last_call.get(grade, 0.0))
        if gap > 0:
            time.sleep(gap)
        self._last_call[grade] = time.monotonic()

    def _log(self, api: str, status: int | None, started: float, code: str, message: str) -> None:
        if self.call_logger:
            self.call_logger(
                {
                    "ts": datetime.now(KST).isoformat(timespec="milliseconds"),
                    "api": api,
                    "http_status": status if status is not None else "",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "error_code": code,
                    "message": message[:300],
                }
            )

    # ------------------------------------------------------------ 국내 API (명세 이름 그대로)
    def price(self, sym: str) -> dict[str, Any]:
        return self.call("domestic_price", {"sym": sym})

    def price_bid(self, sym: str) -> dict[str, Any]:
        return self.call("domestic_price-bid", {"sym": sym, "exch_cd": "KSP"})

    def product_detail(self) -> dict[str, Any]:
        return self.call("domestic_product-detail", {})

    def chart_minute(
        self,
        sym: str,
        day: str,
        *,
        minutes: int = 1,
        next_key: str = FIRST_PAGE,
        rows: int = 300,
        night: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "domestic_chart-minute",
            {
                "sym": sym,
                "inq_strt_dt": day,
                "inq_ed_dt": day,
                "strt_tm": "000000",
                "ed_tm": "235959",
                "dcnt": str(minutes),
                "nght_tp": "2" if night else "1",
                "cidx_yn": "0",
                "fut_cidx_exp_mod": "0",
                "nxt_key": next_key,
                "req_qty": str(rows),
            },
        )

    def order(self, sym: str, *, side: str, qty: int, order_type: str = "2", price: str = "0") -> dict[str, Any]:
        """신규주문(주간). side: BUY/SELL, order_type: 1 지정가·2 시장가. 시장가는 가격 0."""
        return self.call(
            "domestic_order",
            {
                "sym": sym,
                "trd_div": "1" if side == "BUY" else "2",
                "ord_tp_cd": order_type,
                "ord_qty": str(qty),
                "ord_pric": price,
                "exec_cnd_cd": "1",  # FAS
            },
        )

    def cancel(self, sym: str, *, org_ord_no: str, side: str, qty: int) -> dict[str, Any]:
        return self.call(
            "domestic_cancel",
            {
                "sym": sym,
                "trd_div": "1" if side == "BUY" else "2",
                "ord_qty": str(qty),
                "org_ord_no": org_ord_no.zfill(10),
            },
        )

    def order_history(self, *, next_key: str = "") -> dict[str, Any]:
        return self.call(
            "domestic_order-history",
            {"exec_div": "0", "trd_div": "0", "trd_tm_div_cd": "1", "ord_div_cd": "0", "nxt_key": next_key},
        )

    def open_interest(self, *, next_key: str = "") -> dict[str, Any]:
        return self.call("domestic_open-interest", {"nxt_key": next_key})

    # ------------------------------------------------------------ 편의 함수
    def minute_bars(self, sym: str, day: str, *, minutes: int = 1, max_pages: int = 10) -> list[dict[str, Any]]:
        """그날 주간 분봉 전부를 오래된 것부터. 응답은 최신 봉이 먼저 옵니다."""
        rows: list[dict[str, Any]] = []
        key = FIRST_PAGE
        for _ in range(max_pages):
            body = self.chart_minute(sym, day, minutes=minutes, next_key=key)
            page = body.get("Occurs1") or []
            rows.extend(page)
            key = str(body.get("nxt_key") or "").strip()
            if len(page) < 300 or not key:
                break
        bars = [parse_bar(r, day) for r in rows]
        uniq = {b["start"]: b for b in bars if b}
        return [uniq[k] for k in sorted(uniq)]

    def order_rows(self, max_pages: int = 5) -> list[dict[str, Any]]:
        rows, key = [], ""
        for _ in range(max_pages):
            body = self.order_history(next_key=key)
            rows.extend(body.get("Occurs1") or [])
            key = str(body.get("nxt_key") or "").strip()
            if not key:
                break
        return rows

    def position_of(self, sym: str) -> int:
        """잔고에서 그 종목의 순포지션(매수 +, 매도 −)."""
        net, key = 0, ""
        for _ in range(5):
            body = self.open_interest(next_key=key)
            for r in body.get("Occurs1") or []:
                if str(r.get("occurs1_sym", "")).strip() != sym:
                    continue
                qty = int(num(r.get("occurs1_opint_qty")))
                net += qty if str(r.get("occurs1_trd_div")) == "1" else -qty
            key = str(body.get("nxt_key") or "").strip()
            if not key:
                break
        return net


# ---------------------------------------------------------------- 도우미
def num(value: Any) -> float:
    try:
        return float(str(value).strip() or 0)
    except ValueError:
        return 0.0


def parse_bar(row: dict[str, Any], day: str) -> dict[str, Any] | None:
    """분봉 한 줄. occurs1_exch_tm 은 봉 **시작** 시각(HHMMSS)입니다."""
    try:
        ref = str(row.get("occurs1_ref_dt") or day)
        tm = str(row["occurs1_exch_tm"]).zfill(6)
        start = datetime.strptime(ref + tm, "%Y%m%d%H%M%S").replace(tzinfo=KST)
        return {
            "start": start,
            "open": num(row["occurs1_open_pric"]),
            "high": num(row["occurs1_high_pric"]),
            "low": num(row["occurs1_low_pric"]),
            "close": num(row["occurs1_close_pric"]),
            "volume": num(row.get("occurs1_cum_trd_qty")),
        }
    except (KeyError, ValueError):
        return None


def _json_or_text(r: requests.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text


def _failure(body: Any) -> tuple[str, str] | None:
    if not isinstance(body, dict):
        return None
    code = str(body.get("errorCode") or "").strip()
    if code and code not in OK_CODES:
        msg = next((str(body.get(k)) for k in ("errorMessage", "errorMsg", "message", "msg") if body.get(k)), "")
        return code, msg
    code = str(body.get("msg_cd") or "").strip()
    if code:
        return code, str(body.get("msg_cntn") or "").strip()
    return None


def _parse_expiry(body: dict[str, Any]) -> datetime:
    raw = str(body.get("expires_at") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    try:
        return datetime.now(KST) + timedelta(seconds=int(float(body.get("expires_in"))))
    except (TypeError, ValueError):
        return datetime.now(KST) + timedelta(minutes=10)
