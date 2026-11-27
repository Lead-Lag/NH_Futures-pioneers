"""두 다리 주문 처리 시험 — 가짜 NH 서버로 「한쪽만 체결」 같은 상황을 일부러 만들어 확인합니다.
키·인터넷 불필요.  python -m unittest discover tests
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ["NO_PROXY"] += ",127.0.0.1,localhost"

import requests  # noqa: E402

import mock_server  # noqa: E402
from journal import RunJournal  # noqa: E402
from nh_client import Credentials, NHClient  # noqa: E402
from pair_broker import PaperPairExecutor  # noqa: E402
from pair_engine import (ABORTED, BROKEN, BUY, COMPLETE, SELL, UNWOUND, Leg, PairIntent)  # noqa: E402

KST = timezone(timedelta(hours=9))
PORT = 8799
URL = f"http://127.0.0.1:{PORT}"
SYMS = {"S": mock_server.SAM, "H": mock_server.HYN}


def setUpModule():
    global SRV
    SRV = mock_server.serve(PORT)


def tearDownModule():
    SRV.shutdown()


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = RunJournal(Path(self.tmp.name), echo=False)
        self.c = NHClient(Credentials("paper", "k", "s", URL), timeout=2.0)
        self.ex = PaperPairExecutor(self.c, SYMS, self.j, {"S": 100, "H": 1000}, {"S": 10, "H": 10}, leg_timeout=1.5)
        self.ex.POLL = 0.2

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def scenario(self, name):
        requests.post(URL + "/mock/scenario", json={"name": name}, timeout=2)

    def pos(self):
        return {k: self.c.position_of(s) for k, s in SYMS.items()}

    def entry(self, qs=10, qh=1):
        return PairIntent(1, datetime.now(KST), "ENTRY_SHORT_SPREAD", -1, "시험",
                          [Leg("H", BUY, qh), Leg("S", SELL, qs)], {"S": 188500.0, "H": 1116000.0})

    def test_normal_complete(self):
        self.scenario("normal")
        out = self.ex.execute(self.entry())
        self.assertEqual(out.status, COMPLETE)
        self.assertEqual(out.qty, {"H": 1, "S": 10})
        self.assertEqual(self.pos(), {"S": -10, "H": 1})

    def test_second_leg_never_fills_is_unwound(self):
        self.scenario("leg2_never")
        out = self.ex.execute(self.entry())
        self.assertEqual(out.status, UNWOUND)
        self.assertEqual(self.pos(), {"S": 0, "H": 0})      # 하이닉스를 되팔아 노출 0
        roles = [x.role for x in out.legs]
        self.assertIn("unwind", roles)
        self.assertNotEqual(out.exposure_ms, "")

    def test_partial_second_leg_retries_then_unwinds(self):
        self.scenario("leg2_partial")
        out = self.ex.execute(self.entry())
        # 10 → 5 체결, 남은 5 다시 → 2 체결 … 재전송 1번 뒤에도 모자라 되돌림
        self.assertEqual(out.status, UNWOUND)
        self.assertEqual(self.pos(), {"S": 0, "H": 0})

    def test_first_leg_rejected_aborts_without_exposure(self):
        self.scenario("leg1_reject")
        out = self.ex.execute(self.entry())
        self.assertEqual(out.status, ABORTED)
        self.assertEqual(len(out.legs), 1)
        self.assertEqual(self.pos(), {"S": 0, "H": 0})

    def test_cancel_race_uses_final_fill(self):
        self.scenario("cancel_race")
        out = self.ex.execute(self.entry())
        # 취소 요청 순간 전량 체결 → 취소 뒤 다시 읽어 체결로 인정, 페어 완성
        self.assertEqual(out.status, COMPLETE)
        self.assertEqual(self.pos(), {"S": -10, "H": 1})

    def test_split_fills_are_summed(self):
        self.scenario("split_fills")
        out = self.ex.execute(self.entry())
        self.assertEqual(out.status, COMPLETE)
        self.assertEqual(out.qty["S"], 10)
        self.assertAlmostEqual(out.avg["S"], 188550.0)     # 5@188500 + 5@188600

    def test_timeout_not_resent(self):
        self.scenario("timeout_once")
        out = self.ex.execute(self.entry())
        # 응답 시간 초과 → 다시 보내지 않고 주문내역에서 찾아 체결 확인
        self.assertEqual(out.status, COMPLETE)
        state = requests.post(URL + "/mock/state", timeout=2).json()
        self.assertEqual(state["orders"], 2)               # 하이닉스 1 + 삼성 1 (중복 주문 없음)
        self.assertEqual(self.pos(), {"S": -10, "H": 1})

    def test_flaky_token_still_completes(self):
        self.scenario("token_flaky")
        out = self.ex.execute(self.entry())
        self.assertEqual(out.status, COMPLETE)
        state = requests.post(URL + "/mock/state", timeout=2).json()
        self.assertEqual(state["orders"], 2)               # 거절된 주문은 처리 전 거절 → 중복 주문 없음
        self.assertEqual(self.pos(), {"S": -10, "H": 1})

    def test_flatten(self):
        self.scenario("normal")
        self.ex.execute(self.entry())
        self.ex.flatten(self.pos(), "시험")
        self.assertEqual(self.pos(), {"S": 0, "H": 0})


if __name__ == "__main__":
    unittest.main()
