#!/usr/bin/env python3
"""KDI 경제정보센터 「월간 경제 이슈」 → 상위 N위 이슈의 관련 기사 제목 수집 → kdi_bottleneck.py 입력 CSV.

수집 허용 확인(2026-11-18): robots.txt = "User-agent: * / Allow: /", 이용약관에 자동 수집 금지 조항 없음.
수집 범위: 기사 목록의 제목·날짜·언론사만. 기사 본문(네이버 뉴스)에는 접속하지 않는다.
요청 간격: --delay 초(기본 1.5초). 받은 원본은 results/news/raw/ 에 저장하고, 다시 돌리면 저장본을 쓴다(재요청 없음).

확인한 요청(브라우저 개발자도구, 2026-11-18):
  월 페이지   GET https://eiec.kdi.re.kr/bigdata/monthlyEconomicIssue.do?year=2026&month=09   (HTML, 이슈 순위 목록)
  이슈 기사   GET https://eiec.kdi.re.kr/bigdata/monthlyEconomicIssue/getIssueDetailJson.do?cluster_id=30&pub_date=202610&issue_name=...
              → JSON {keyword, news[], newsMinMaxDate, sentiment, trend}

필요한 것: Python 3 + requests 만 (pandas 불필요 — 이 노트북의 보안 정책에서도 실행 가능)

사용:
  python kdi_crawl.py --start 2026-10 --end 2026-10 --max-rank 3          # ① 먼저 한 달·3개 이슈로 시험
  python kdi_crawl.py --start 2026-01 --end 2026-10                       # ② 전체
"""
from __future__ import annotations

import argparse
import csv
import html as htmllib
import json
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://eiec.kdi.re.kr"
PAGE = BASE + "/bigdata/monthlyEconomicIssue.do"
DETAIL = BASE + "/bigdata/monthlyEconomicIssue/getIssueDetailJson.do"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) university-assignment-crawler (low rate)",
           "Referer": PAGE, "Accept-Language": "ko-KR,ko;q=0.9"}


def months(start: str, end: str) -> list[tuple[int, int]]:
    y, m = map(int, start.split("-"))
    y2, m2 = map(int, end.split("-"))
    out = []
    while (y, m) <= (y2, m2):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class Fetcher:
    def __init__(self, delay: float, raw: Path):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay, self.raw = delay, raw
        self.calls = 0
        raw.mkdir(parents=True, exist_ok=True)

    def get(self, url: str, params: dict, cache_name: str) -> str:
        f = self.raw / cache_name
        if f.exists() and f.stat().st_size > 0:
            return f.read_text(encoding="utf-8")
        for attempt in range(3):
            try:
                time.sleep(self.delay)
                r = self.s.get(url, params=params, timeout=30)
                self.calls += 1
                r.raise_for_status()
                r.encoding = r.encoding or "utf-8"
                f.write_text(r.text, encoding="utf-8")
                return r.text
            except requests.RequestException as e:
                wait = 5 * (attempt + 1)
                print(f"  [재시도 {attempt + 1}/3] {e} — {wait}초 뒤", file=sys.stderr)
                time.sleep(wait)
        raise SystemExit(f"[오류] 받지 못했습니다: {url} {params}")


# ───────────── 월 페이지 HTML 에서 (순위, cluster_id, 이슈명) 찾기 ─────────────
PATTERNS = [
    # JS 객체/JSON: cluster_id: 30, ... issue_name: "..."
    re.compile(r"cluster_id[\"']?\s*[:=]\s*[\"']?(\d+)[\"']?[^{}]{0,400}?issue_name[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", re.S),
    re.compile(r"issue_name[\"']?\s*[:=]\s*[\"']([^\"']+)[\"'][^{}]{0,400}?cluster_id[\"']?\s*[:=]\s*[\"']?(\d+)", re.S),
    # 함수 호출: fn(30, '202610', '이슈명') / fn('30','202610','이슈명')
    re.compile(r"\(\s*[\"']?(\d{1,4})[\"']?\s*,\s*[\"'](\d{6})[\"']\s*,\s*[\"']([^\"']{2,80})[\"']"),
    # data 속성
    re.compile(r"data-cluster(?:[-_]id)?\s*=\s*[\"'](\d+)[\"'][^>]*?data-(?:issue[-_])?name\s*=\s*[\"']([^\"']+)[\"']", re.S),
    re.compile(r"data-(?:issue[-_])?name\s*=\s*[\"']([^\"']+)[\"'][^>]*?data-cluster(?:[-_]id)?\s*=\s*[\"'](\d+)[\"']", re.S),
]


def parse_issues(page: str) -> list[dict]:
    """1순위: 이슈맵 차트의 data-chart-json(공식 rank·cluster_id·name·기사 수 cnt). 없으면 아래 추정 패턴."""
    mt = re.search(r"data-chart-json='(\[.*?\])'", page, re.S)
    if mt:
        try:
            arr = json.loads(htmllib.unescape(mt.group(1)))
            if arr and "cluster_id" in arr[0]:
                return [{"order": i + 1, "rank": int(x["rank"]), "cluster_id": str(x["cluster_id"]),
                         "issue_name": str(x["name"]).strip(), "cnt": x.get("cnt")} for i, x in enumerate(arr)]
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    # 2순위: 순위 목록 버튼 <button data-cluster-id=.. data-issue-name=..><b>순위</b>
    btn = re.findall(r'data-cluster-id="(\d+)"\s+data-pub-date="\d{6}"\s+data-issue-name="([^"]+)"[^>]*>\s*<b>(\d+)</b>', page)
    if btn:
        seen, out = set(), []
        for cid, name, rk in btn:
            if cid not in seen:
                seen.add(cid)
                out.append({"order": len(out) + 1, "rank": int(rk), "cluster_id": cid,
                            "issue_name": htmllib.unescape(name).strip(), "cnt": None})
        return out
    found: list[tuple[int, str, str]] = []          # (위치, cluster_id, 이름)
    for i, pat in enumerate(PATTERNS):
        for mt in pat.finditer(page):
            g = mt.groups()
            if i == 0:
                cid, name = g
            elif i == 1:
                name, cid = g
            elif i == 2:
                cid, _pub, name = g
            elif i == 3:
                cid, name = g
            else:
                name, cid = g
            name = htmllib.unescape(name).strip()
            if re.search(r"[가-힣]", name):
                found.append((mt.start(), cid, name))
        if found:
            break
    # 처음 나온 순서 = 화면 순위 순서로 가정(나중에 화면과 대조)
    seen, issues = set(), []
    for _pos, cid, name in sorted(found):
        if cid in seen:
            continue
        seen.add(cid)
        issues.append({"order": len(issues) + 1, "cluster_id": cid, "issue_name": name})
    # 순위 번호(동순위 포함)를 이슈명 바로 앞의 숫자에서 찾아본다
    for it in issues:
        alts = "|".join(re.escape(x) for x in {it["issue_name"], htmllib.escape(it["issue_name"], quote=False)})
        mt = re.search(r">\s*(\d{1,2})\s*<[^가-힣]{0,300}?(?:" + alts + ")", page, re.S)
        it["rank"] = int(mt.group(1)) if mt else it["order"]
    return issues


def diagnose(page: str, out: Path) -> None:
    out.write_text(page, encoding="utf-8")
    print(f"[진단] 월 페이지에서 이슈 목록을 찾지 못했습니다. 원본: {out}", file=sys.stderr)
    for kw in ("cluster", "getIssueDetail", "issue_name", "onclick"):
        hits = [mt.start() for mt in re.finditer(kw, page)][:3]
        for h in hits:
            snippet = page[max(0, h - 150): h + 250].replace("\n", " ")
            print(f"--- '{kw}' 주변 ---\n{snippet}\n", file=sys.stderr)
    print("위 출력을 그대로 복사해 보내 주세요.", file=sys.stderr)


# ───────────── 기사 JSON 에서 필드 찾기 ─────────────
def pick_key(item: dict, prefer: list[str], contains: str, avoid: tuple[str, ...] = ()) -> str | None:
    for k in prefer:
        if k in item:
            return k
    for k in item:
        if contains in k.lower() and not any(a in k.lower() for a in avoid):
            return k
    return None


def norm_date(v: str) -> str:
    d = re.sub(r"\D", "", str(v))[:8]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else str(v)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2026-01")
    ap.add_argument("--end", default="2026-10")
    ap.add_argument("--max-rank", type=int, default=10, help="순위 번호가 이 값 이하인 이슈 전부(동순위 포함)")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--out", type=Path, default=Path("results/news/kdi_2026.csv"))
    ap.add_argument("--raw", type=Path, default=Path("results/news/raw"))
    a = ap.parse_args()

    fx = Fetcher(a.delay, a.raw)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    log_rows, n_total, keys_shown = [], 0, False

    with a.out.open("w", newline="", encoding="utf-8-sig") as fo:
        w = csv.writer(fo)
        w.writerow(["date", "issue_rank", "issue_title", "news_title", "press", "cluster_id", "pub_date", "url"])
        for (y, m) in months(a.start, a.end):
            pub = f"{y}{m:02d}"
            page = fx.get(PAGE, {"year": y, "month": f"{m:02d}"}, f"page_{pub}.html")
            issues = parse_issues(page)
            if not issues:
                diagnose(page, a.raw / f"page_{pub}_diagnose.html")
                sys.exit(1)
            sel = [it for it in issues if it["rank"] <= a.max_rank]
            print(f"\n[{pub}] 이슈 {len(issues)}개 중 {a.max_rank}위 이내 {len(sel)}개 — 화면 순위와 같은지 확인하세요")
            for it in sel:
                print(f"  {it['rank']:>2}위  cluster_id={it['cluster_id']:<4} {it['issue_name']}")
            for it in sel:
                txt = fx.get(DETAIL, {"cluster_id": it["cluster_id"], "pub_date": pub, "issue_name": it["issue_name"]},
                             f"issue_{pub}_{it['cluster_id']}.json")
                try:
                    news = json.loads(txt).get("news", [])
                except json.JSONDecodeError:
                    print(f"  [경고] {pub} cluster {it['cluster_id']}: JSON 이 아님 — 건너뜀 (원본 raw 폴더)", file=sys.stderr)
                    continue
                if news and not keys_shown:
                    print(f"  [기사 필드] {list(news[0].keys())}")
                    keys_shown = True
                if not news:
                    log_rows.append([pub, it["rank"], it["cluster_id"], it["issue_name"], 0, it.get("cnt")])
                    continue
                tk = pick_key(news[0], ["title", "news_title", "TITLE"], "title")
                dk = pick_key(news[0], ["news_date", "date", "pub_dt", "reg_date"], "date", avoid=("pub_date",))
                jk = pick_key(news[0], ["journal", "press"], "journal")
                uk = pick_key(news[0], ["url", "link", "news_url"], "url")
                if tk is None:
                    sys.exit(f"[오류] 기사 제목 필드를 찾지 못했습니다. 필드 목록: {list(news[0].keys())} — 이 줄을 보내 주세요.")
                for nw in news:
                    w.writerow([norm_date(nw.get(dk, "")) if dk else "", it["rank"], it["issue_name"],
                                str(nw.get(tk, "")).strip(), nw.get(jk, "") if jk else "", it["cluster_id"], pub,
                                nw.get(uk, "") if uk else ""])
                n_total += len(news)
                log_rows.append([pub, it["rank"], it["cluster_id"], it["issue_name"], len(news), it.get("cnt")])
                exp = it.get("cnt")
                flag = "" if exp in (None, len(news)) else f"  ← 화면 기사 수 {exp}건과 다름"
                print(f"  {it['rank']:>2}위 기사 {len(news)}건{flag}")

    log = a.out.with_name(a.out.stem + "_crawl_log.csv")
    with log.open("w", newline="", encoding="utf-8-sig") as fl:
        csv.writer(fl).writerows([["pub_date", "issue_rank", "cluster_id", "issue_name", "n_news", "kdi_cnt"]] + log_rows)
    print(f"\n완료: 기사 {n_total}건 → {a.out}\n이슈별 건수 기록 → {log}\n이번 실행의 실제 요청 {fx.calls}회 (나머지는 저장본 사용)")
    print("※ date 열이 비어 있으면 기사 날짜 필드를 못 찾은 것 — 위 [기사 필드] 줄을 보내 주세요.")


if __name__ == "__main__":
    main()
