"""오늘 해외 모의계좌 주문·체결 내역을 서버에서 조회해 표로 보여 주고 CSV 로 저장.
앱 「주문체결내역」 화면과 같은 API(order-history)를 읽음 — 프로그램 기록(orders.csv)과 대조용.

    python order_history.py
"""
import csv
from datetime import datetime
from pathlib import Path

from nh_overseas import OverseasClient, keys_from_env


def n(v):
    try:
        x = float(str(v).strip())
        return f"{x:,.2f}".rstrip("0").rstrip(".") if x else "0"
    except ValueError:
        return str(v).strip()


def t(v):
    s = str(v).strip()
    return f"{s[8:10]}:{s[10:12]}:{s[12:14]}" if len(s) >= 14 else s


out = Path("results") / f"order_history_{datetime.now():%Y%m%d_%H%M%S}"
api = OverseasClient("paper", *keys_from_env(), out)
rows = []
for r in api.order_history("0"):
    g = lambda k: r.get("occurs1_" + k, "")
    side = str(g("trd_div_nm")).strip() or {"1": "매수", "2": "매도"}.get(str(g("trd_tp")).strip(), "?")
    kind = str(g("glb_ord_tp_cd_nm")).strip() or ("시장가" if n(g("ord_pric")) == "0" else "지정가")
    rows.append([str(g("ord_dt")).strip(), t(g("ord_rcv_tm")), t(g("exec_tm")), str(g("sym")).strip(), side, kind,
                 n(g("ord_qty")), n(g("ord_pric")), n(g("exec_qty")), n(g("exec_pric")),
                 str(g("ord_sts")).strip(), str(g("ord_no")).strip()])
rows.sort(key=lambda x: (x[1], x[-1]))
head = ["주문일", "접수", "체결", "종목", "매매", "유형", "주문수량", "주문가", "체결수량", "체결가", "상태", "주문번호"]
with open(out / "order_history.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(head)
    w.writerows(rows)
print(f"서버 주문내역 {len(rows)}건  (조회 {datetime.now():%Y-%m-%d %H:%M:%S}, 해외 모의계좌)")
print(" | ".join(head))
for x in rows:
    print(" | ".join(x))
print(f"저장: {out / 'order_history.csv'}")
