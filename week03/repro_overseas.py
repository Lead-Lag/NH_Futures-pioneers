"""오류 제보용 재현 — 요청과 응답 원본을 그대로 JSON 파일로 저장 (주문 없음).

    python repro_overseas.py

① 해외 현재가: 같은 종목을 거래소 코드 FHKEX / FHKE 로, 없는 종목(MHIF27)을 FHKE 로 조회
② 해외 주문내역: 응답 원본 (오늘 주문이 있어야 함)
계좌번호로 보이는 칸(acnt·account)은 저장 전에 가립니다. 토큰·키는 저장하지 않습니다.
결과: results/repro_<시각>/  (case_*.json · api_calls.csv)
"""
import json
from datetime import datetime
from pathlib import Path

from nh_overseas import BASE_URL, SPECS, OverseasClient, keys_from_env

out = Path("results") / f"repro_{datetime.now():%Y%m%d_%H%M%S}"
api = OverseasClient("paper", *keys_from_env(), out)
api._ensure_token()


def mask(o):
    if isinstance(o, dict):
        return {k: ("****" if any(w in k.lower() for w in ("acnt", "account", "ac_no", "acno")) else mask(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [mask(x) for x in o]
    return o


def raw_call(name: str, api_id: str, payload: dict) -> None:
    path = SPECS[api_id][0]
    sent = datetime.now().isoformat(timespec="seconds")
    r = api.s.post(api.base + path, json=payload, timeout=10,
                   headers={"Authorization": f"Bearer {api.token}"})
    try:
        body = r.json()
    except ValueError:
        body = {"_text": r.text[:2000]}
    rec = {"case": name, "sent_at": sent, "env": "paper", "url": BASE_URL["paper"] + path,
           "request": payload, "http_status": r.status_code, "response": mask(body)}
    (out / f"case_{name}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    occ = body.get("Occurs1") if isinstance(body, dict) else None
    keys = {k: body.get(k) for k in ("errorCode", "errorMessage", "err_cd", "err_msg", "msg_cd", "msg_cntn")
            if isinstance(body, dict) and k in body}
    first = occ[0] if occ else None
    brief = {k: first.get(k) for k in ("occurs1_sym", "occurs1_last_pric", "occurs1_cum_trd_qty")} if first else None
    print(f"[{name}] HTTP {r.status_code} | 오류칸 {keys or '없음'} | Occurs1 {len(occ) if occ is not None else '없음'}줄 | 첫 줄 {brief}")


def price(exch, sym):
    return {"Iccurs1": [{"iccurs1_exch_cd": exch, "iccurs1_sym": sym}]}


raw_call("price_FHKEX_MHIX26", "price", price("FHKEX", "MHIX26"))
raw_call("price_FHKE_MHIX26", "price", price("FHKE", "MHIX26"))
raw_call("price_FHKE_MHIF27", "price", price("FHKE", "MHIF27"))
raw_call("price_FSGX_SFCX26", "price", price("FSGX", "SFCX26"))
raw_call("order_history", "order-history", {"inq_div": "0", "trd_div_cd": "0", "nxt_key": ""})
print(f"저장: {out}")

