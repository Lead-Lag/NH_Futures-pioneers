#!/usr/bin/env python3
"""OpenDART 수집 — 「진짜 2차」 연결용 (10절 다음 단계).

경로 (B) 공급계약: 「단일판매ㆍ공급계약체결」 공시에서 '누가(공시 회사) 누구에게(계약상대) 무엇을' 공급하는지 모은다.
  → 병목 단어의 1차 회사(주요제품 일치)를 계약상대로 둔 공시 회사 = 2차 후보.
  → 공시마다 접수일(rcept_dt)이 있어 「진입일 전에 공개된 공시만」 쓰면 미래 편향이 없다.

requests 만 사용(노트북에서 실행 가능). 인증키는 환경변수 DART_API_KEY 또는 config.local.json 의 "DART_API_KEY" 칸.
키는 코드·결과 파일에 남기지 않는다.

사용:
  python dart_collect.py test                                   # 키 확인 + 지난달 공급계약 공시 몇 건인지 + 3건 파싱해 보기
  python dart_collect.py contracts --start 2023-04 --end 2024-11 # 기간 전체 수집(원본은 results/dart/raw/ 에 저장, 다시 돌리면 이어서)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

BASE = "https://opendart.fss.or.kr/api"
OUT = Path("results/dart")
STATUS = {"000": "정상", "010": "등록되지 않은 키", "011": "사용할 수 없는 키", "012": "접근할 수 없는 IP",
          "013": "조회된 데이터 없음", "020": "요청 제한 초과(하루 2만 건)", "100": "필드 오류", "800": "시스템 점검", "900": "정의되지 않은 오류"}


# ───────────────────────── 공통 ─────────────────────────
def api_key() -> str:
    k = os.environ.get("DART_API_KEY", "").strip()
    if not k and Path("config.local.json").exists():
        k = str(json.loads(Path("config.local.json").read_text(encoding="utf-8")).get("DART_API_KEY", "")).strip()
    if not k:
        sys.exit("[오류] DART 인증키가 없습니다. config.local.json 에 \"DART_API_KEY\": \"발급받은 키\" 를 넣거나 "
                 "환경변수 DART_API_KEY 를 설정하세요(이 파일은 커밋하지 않음).")
    return k


class Dart:
    def __init__(self, key: str, delay: float):
        self.key, self.delay, self.s = key, delay, requests.Session()
        self.calls = 0

    def get(self, path: str, **params) -> requests.Response:
        time.sleep(self.delay)
        self.calls += 1
        r = self.s.get(f"{BASE}/{path}", params={"crtfc_key": self.key, **params}, timeout=30)
        r.raise_for_status()
        return r

    def json(self, path: str, **params) -> dict:
        d = self.get(path, **params).json()
        st = d.get("status")
        if st not in ("000", "013"):
            sys.exit(f"[오류] {path}: status {st} {STATUS.get(st, '')} — {d.get('message', '')}")
        return d


def decode(b: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


# ───────────────────────── 회사 코드 ─────────────────────────
def corp_codes(d: Dart) -> list[dict]:
    """DART 고유번호 ↔ 종목코드(상장사만)."""
    f = OUT / "corp_codes.csv"
    if f.exists():
        return list(csv.DictReader(f.open(encoding="utf-8-sig")))
    r = d.get("corpCode.xml")
    if r.content[:2] != b"PK":                       # 오류면 zip 대신 JSON/XML 메시지가 옴
        sys.exit(f"[오류] corpCode.xml: {decode(r.content)[:300]}")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    rows = [{"corp_code": e.findtext("corp_code"), "corp_name": e.findtext("corp_name"),
             "corp_eng_name": (e.findtext("corp_eng_name") or "").strip(),      # 영문 계약상대(「SK hynix」 등) 연결용
             "stock_code": (e.findtext("stock_code") or "").strip()} for e in root.iter("list")]
    rows = [x for x in rows if x["stock_code"]]
    OUT.mkdir(parents=True, exist_ok=True)
    with f.open("w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["corp_code", "corp_name", "corp_eng_name", "stock_code"])
        w.writeheader()
        w.writerows(rows)
    return rows


# ───────────────────────── 공시 목록 ─────────────────────────
def month_windows(start: str, end: str):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        first = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        yield first, nxt - timedelta(days=1)
        y, m = nxt.year, nxt.month


def list_contracts(d: Dart, first: date, last: date) -> list[dict]:
    """거래소 공시(pblntf_ty=I) 중 보고서명에 「공급계약」 이 있는 것. 회사를 지정하지 않으면 한 번에 3개월까지라 달마다 조회."""
    out, page = [], 1
    while True:
        js = d.json("list.json", bgn_de=first.strftime("%Y%m%d"), end_de=last.strftime("%Y%m%d"),
                    pblntf_ty="I", page_no=page, page_count=100)
        for x in js.get("list", []):
            if "공급계약" in x.get("report_nm", ""):
                out.append({k: x.get(k, "") for k in ("rcept_no", "rcept_dt", "corp_code", "corp_name",
                                                       "stock_code", "corp_cls", "report_nm")})
        if page >= int(js.get("total_page", 1) or 1):
            return out
        page += 1


# ───────────────────────── 공시 원문 파싱 ─────────────────────────
CELL_RE = re.compile(r"<(TD|TE|TH|TU)\b[^>]*>(.*?)</\1>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
FIELDS = {   # 원문 표의 왼쪽 칸 이름(일부만 맞아도 됨) → 저장할 열
    "contract_name": ["체결계약명", "계약명", "판매ㆍ공급계약 내용", "판매·공급계약 내용", "계약내용", "세부내용"],
    "counterparty": ["계약상대방", "계약상대"],
    "amount": ["계약금액", "확정 계약금액", "계약금액 총액"],
    "sales_ratio": ["매출액대비", "매출액 대비"],
    "period_start": ["시작일"],
    "period_end": ["종료일"],
    "contract_date": ["계약(수주)일자", "계약일자", "수주일자"],
}


LABELS = [n.replace(" ", "") for ns in FIELDS.values() for n in ns] + ["최근매출액", "판매ㆍ공급지역", "계약기간", "회사와의관계"]


def is_label(v: str) -> bool:
    """값이 아니라 다음 칸의 이름(「최근 매출액(원)」 등)인지."""
    k = v.replace(" ", "")
    return any(n in k for n in LABELS) or bool(re.fullmatch(r".*\((원|%|USD|백만원)\)", k))


def cells(xml: str) -> list[str]:
    return [re.sub(r"\s+", " ", TAG_RE.sub(" ", c[1])).replace("&amp;", "&").strip() for c in CELL_RE.findall(xml)]


def parse_contract(xml: str) -> dict:
    """「왼쪽 칸 이름 → 오른쪽 칸 값」. 형식이 회사마다 조금씩 달라 못 찾으면 빈칸(원본은 raw/ 에 있음)."""
    cs = cells(xml)
    # 정정 공시는 앞에 「정정전 · 정정후」 비교표가 있어 정정전 값을 먼저 읽게 됨 → 비교표 뒤 본문(1. 판매ㆍ공급계약 내용 …)부터 읽는다
    if any("정정사항" in c for c in cs[:20]):
        body = [i for i, c in enumerate(cs) if c.replace(" ", "").startswith("1.판매")]
        if body:
            cs = cs[body[-1]:]
    got = {}
    for i, c in enumerate(cs[:-1]):
        key = c.replace(" ", "")
        for field, names in FIELDS.items():
            if field in got:
                continue
            if any(n.replace(" ", "") in key for n in names) and "관계" not in key and "회사와의" not in key:
                v = next((x for x in cs[i + 1:i + 4] if x), "")
                if v and not is_label(v):          # 「-」 은 비공개·해당 없음이므로 그대로 둔다(다음 칸 이름을 값으로 읽지 않게)
                    got[field] = v
    return got


def fetch_doc(d: Dart, rcept_no: str) -> str:
    raw = OUT / "raw" / f"{rcept_no}.zip"
    if not raw.exists():
        r = d.get("document.xml", rcept_no=rcept_no)
        if r.content[:2] != b"PK":
            return ""
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(r.content)
    z = zipfile.ZipFile(raw)
    return "\n".join(decode(z.read(n)) for n in z.namelist())


# ───────────────────────── 실행 ─────────────────────────
COLS = ["rcept_no", "rcept_dt", "corp_code", "corp_name", "stock_code", "corp_cls", "report_nm", "is_correction",
        "counterparty", "contract_name", "amount", "sales_ratio", "contract_date", "period_start", "period_end"]


def cmd_test(d: Dart, a) -> None:
    cc = corp_codes(d)
    print(f"[1] 키 정상 · 상장사 고유번호 {len(cc):,}곳 → {OUT / 'corp_codes.csv'}")
    today = date.today()
    last = today.replace(day=1) - timedelta(days=1)
    rows = list_contracts(d, last.replace(day=1), last)
    print(f"[2] {last:%Y-%m} 공급계약 공시 {len(rows)}건 (정정 공시 포함)")
    for x in rows[:a.n]:
        p = parse_contract(fetch_doc(d, x["rcept_no"]))
        print(f"    {x['rcept_no']} {x['corp_name']:<12} → 상대: {p.get('counterparty', '?'):<20} "
              f"| {p.get('contract_name', '?')[:30]} | 매출액 대비 {p.get('sales_ratio', '?')}")
    print(f"API 호출 {d.calls}회 (하루 한도 2만 회)")


def cmd_contracts(d: Dart, a) -> None:
    corp_codes(d)
    OUT.mkdir(parents=True, exist_ok=True)
    lst = []
    for first, last in month_windows(a.start, a.end):
        rows = list_contracts(d, first, last)
        print(f"{first:%Y-%m}: 공급계약 공시 {len(rows)}건")
        lst += rows
    out = OUT / f"contracts_{a.start}_{a.end}.csv"
    n_ok, n_fix, t0 = 0, 0, time.time()
    todo = sum(1 for x in lst if "정정" not in x["report_nm"] or a.with_corrections)
    print(f"원문을 열 공시 {todo}건(정정 공시 {len(lst) - todo}건은 목록만 저장) · 이미 받은 원문은 다시 받지 않음")
    with out.open("w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=COLS)
        w.writeheader()
        for i, x in enumerate(lst, 1):
            fix = "정정" in x["report_nm"]
            # 2차 연결에는 원래 공시(그 접수일에 알 수 있던 정보)만 쓰므로 정정 공시 원문은 기본적으로 열지 않는다
            p = {} if (fix and not a.with_corrections) else parse_contract(fetch_doc(d, x["rcept_no"]))
            n_ok += bool(p.get("counterparty"))
            n_fix += fix
            w.writerow({**x, **p, "is_correction": fix})
            if i % 200 == 0:
                el = time.time() - t0
                print(f"  {i}/{len(lst)} · 계약상대를 읽은 공시 {n_ok}건 · {el / 60:.0f}분 경과, 남은 시간 약 {el / i * (len(lst) - i) / 60:.0f}분")
    base = max(len(lst) - n_fix, 1)
    print(f"저장: {out} — {len(lst)}건(정정 {n_fix}건 제외 {base}건) 중 계약상대를 읽은 공시 {n_ok}건 ({n_ok / base:.0%}) · API 호출 {d.calls}회")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["test", "contracts"])
    ap.add_argument("--start", default="2023-04", help="contracts 시작 월(2차 연결은 진입 전 12개월 공시를 쓰므로 표본보다 1년 앞부터)")
    ap.add_argument("--end", default="2024-11")
    ap.add_argument("--n", type=int, default=3, help="test 에서 원문을 열어 볼 공시 수")
    ap.add_argument("--delay", type=float, default=0.3, help="요청 간격(초)")
    ap.add_argument("--with-corrections", action="store_true", help="정정 공시 원문도 열기(기본: 목록만)")
    a = ap.parse_args()
    d = Dart(api_key(), a.delay)
    {"test": cmd_test, "contracts": cmd_contracts}[a.cmd](d, a)


if __name__ == "__main__":
    main()
