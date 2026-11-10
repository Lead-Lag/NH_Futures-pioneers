"""실시간(WebSocket) 수신 시험 — 가짜 NH 실시간 서버로 접속·구독·pingpong·시세체결·주문/체결 통보·재연결을 확인.
키·인터넷 불필요(websockets 패키지 필요).  python -m unittest discover tests
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "") + ",127.0.0.1,localhost"

try:
    import websockets  # noqa: F401
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

import requests  # noqa: E402

import mock_server  # noqa: E402
from journal import RunJournal  # noqa: E402
from nh_client import Credentials, NHClient  # noqa: E402
from pair_broker import PaperPairExecutor  # noqa: E402
from pair_engine import BUY, COMPLETE, SELL, Leg, PairIntent  # noqa: E402

KST = timezone(timedelta(hours=9))
PORT, WS_PORT = 8795, 8796
URL = f"http://127.0.0.1:{PORT}"
SYMS = {"S": mock_server.SAM, "H": mock_server.HYN}


@unittest.skipUnless(HAVE_WS, "websockets 패키지 없음")
class WsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = mock_server.serve(PORT)
        cls.ws = mock_server.serve_ws(WS_PORT)
        os.environ["NH_WS_BASE"] = f"http://127.0.0.1:{WS_PORT}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.ws.shutdown()
        os.environ.pop("NH_WS_BASE", None)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = RunJournal(Path(self.tmp.name), echo=False)
        self.c = NHClient(Credentials("paper", "k", "s", URL), timeout=2.0)
        self.feed = None

    def tearDown(self):
        if self.feed:
            self.feed.stop()
        self.j.close()
        self.tmp.cleanup()

    def scenario(self, name):
        requests.post(URL + "/mock/scenario", json={"name": name}, timeout=2)

    def start(self):
        from ws_feed import RealtimeFeed
        self.feed = RealtimeFeed(self.c, SYMS, self.j)
        self.assertTrue(self.feed.start(wait=5), "실시간 연결 실패")
        return self.feed

    def test_quotes_and_bars(self):
        self.scenario("normal")
        f = self.start()
        time.sleep(1.5)
        q = f.quote("S")
        self.assertIsNotNone(q)
        self.assertGreater(q["last"], 0)
        self.assertGreater(f.ticks, 4)
        self.assertTrue(f.bars_of("S") and f.bars_of("H"))

    def test_fill_confirmed_by_ws(self):
        self.scenario("normal")
        f = self.start()
        ex = PaperPairExecutor(self.c, SYMS, self.j, {"S": 100, "H": 1000}, {"S": 10, "H": 10}, leg_timeout=3, feed=f)
        it = PairIntent(1, datetime.now(KST), "ENTRY_SHORT_SPREAD", -1, "시험",
                        [Leg("H", BUY, 1), Leg("S", SELL, 10)], {"S": 188500.0, "H": 1116000.0})
        out = ex.execute(it)
        self.assertEqual(out.status, COMPLETE)
        self.assertEqual([x.confirm for x in out.legs], ["ws", "ws"])

    def test_falls_back_to_rest_when_no_o1(self):
        self.scenario("ws_silent_o1")
        f = self.start()
        ex = PaperPairExecutor(self.c, SYMS, self.j, {"S": 100, "H": 1000}, {"S": 10, "H": 10}, leg_timeout=4, feed=f)
        it = PairIntent(2, datetime.now(KST), "ENTRY_SHORT_SPREAD", -1, "시험",
                        [Leg("H", BUY, 1), Leg("S", SELL, 10)], {"S": 188500.0, "H": 1116000.0})
        out = ex.execute(it)
        self.assertEqual(out.status, COMPLETE)
        self.assertEqual([x.confirm for x in out.legs], ["rest", "rest"])

    def test_reconnect_marks_gap(self):
        self.scenario("ws_drop")
        f = self.start()
        time.sleep(12)          # 8초 뒤 서버가 끊고, 1초 뒤 다시 붙음
        self.assertGreaterEqual(f.connects, 2)
        self.assertTrue(f.gaps and f.gaps[0][1] is not None)
        down = f.gaps[0][0].replace(second=0, microsecond=0)
        self.assertFalse(f.minute_complete(down))    # 끊긴 분은 불완전 → REST 로 보충 대상
        self.assertTrue(f.healthy())


if __name__ == "__main__":
    unittest.main()
