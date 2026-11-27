"""계좌·네트워크를 사용하지 않는 실행 안전성 검증.

HTTP 세션과 WebSocket 연결은 모두 가짜 객체로 대체합니다.
환경변수·실제 키·토큰·계좌·시세에 접근하지 않습니다.
"""
from __future__ import annotations
import asyncio
import importlib.util
import json
import inspect
import functools
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime, timedelta

PROJECT = Path(os.environ.get('HEDGE02_SOURCE', r'C:\Users\vlfph\Documents\GitHub\NH_Futures-pioneers\week06\hedge_02'))
sys.path.insert(0, str(PROJECT))
import nh_client
from error import TradingError, redact
from run_live import LiveSession

# 다른 패키지의 websocket 모듈 캐시와 이름 충돌하지 않도록 명시적으로 로드합니다.
_spec = importlib.util.spec_from_file_location('_hedge02_ws_under_review', PROJECT / 'websocket.py')
ws_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_module)
RealtimeFeed = ws_module.RealtimeFeed
KST = nh_client.KST


class MemoryJournal:
    def __init__(self):
        self.records = []
    def row(self, name, row=None, **kwargs):
        self.records.append((name, {**(row or {}), **kwargs}))
    def event(self, kind, message='', **kwargs):
        self.records.append(('event', {'kind': kind, 'message': message, **kwargs}))
    def order(self, row): self.row('orders', row)
    def fill(self, row): self.row('fills', row)
    def trade(self, row): self.row('trades', row)
    def signal(self, row): self.row('signals', row)
    def bar(self, row): self.row('bars', row)


def response(body, status=200):
    return SimpleNamespace(status_code=status, json=Mock(return_value=body))


def fake_live_session(*, trade=True):
    """인증·시세 준비를 생략하고 주문 상태 전이만 검증합니다."""
    session = LiveSession.__new__(LiveSession)
    session.trade_enabled = trade
    session.j = MemoryJournal()
    session.config = {'execution': {'fill_timeout_seconds': 0, 'flatten_on_exit': True}}
    session.symbols = {'usdkrw': 'TEST_SYMBOL'}
    session.order_lock = asyncio.Lock()
    session.stop_entries = False
    session.repair_needed = False
    session.shutdown = asyncio.Event()
    session.owned_orders = set()
    session.feeds = []
    session.order_task = None
    session.context = SimpleNamespace(metadata={})
    session.eng = SimpleNamespace(position=0, pending=None, fail_count=0, halted='',
        on_order_failed=Mock(), on_fill=Mock(), force_exit=Mock(return_value=None))
    def record_fill(intent, price, qty, ts):
        session.eng.position = qty if intent.side == 'BUY' else -qty
    session.eng.on_fill.side_effect = record_fill
    session.domestic = SimpleNamespace(place_order=Mock(return_value={'order_no': '0000000042'}),
        cancel_order=Mock(return_value={'ord_no': '0000000043'}), reconcile_order=Mock(),
        positions=Mock(return_value=[]), open_orders=Mock(return_value=[]))
    return session


def entry_intent():
    return SimpleNamespace(id=7, is_entry=True, side='BUY', qty=1,
                           purpose='ENTRY_LONG', ref_price=1330.0)


class NoSocketAsyncCase(unittest.TestCase):
    """실제 I/O 없는 코루틴의 상태전이 검증. OS 루프백 소켓도 만들지 않습니다.

    실행 환경이 asyncio의 self-pipe용 socketpair를 허용하지 않아, 즉시 완료되는
    가짜 I/O만 인라인 구동합니다. 실제 네트워크/타이머/OS 이벤트 루프 통합은
    이 단위검증의 범위가 아닙니다.
    """
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name, function in list(cls.__dict__.items()):
            if name.startswith('test_') and inspect.iscoroutinefunction(function):
                @functools.wraps(function)
                def wrapped(self, _function=function):
                    coroutine = _function(self)
                    try:
                        pending = coroutine.send(None)
                    except StopIteration as finished:
                        return finished.value
                    else:
                        coroutine.close()
                        raise AssertionError(f'Unmocked asynchronous I/O in test: {pending!r}')
                setattr(cls, name, wrapped)

    def setUp(self):
        async def immediate(function, *args, **kwargs):
            return function(*args, **kwargs)
        async def immediate_wait(awaitable, timeout=None):
            if timeout is not None and timeout <= 0:
                if inspect.iscoroutine(awaitable):
                    awaitable.close()
                raise asyncio.TimeoutError()
            return await awaitable
        async def immediate_sleep(delay, result=None):
            return result
        for target, replacement in (('asyncio.to_thread', immediate), ('asyncio.wait_for', immediate_wait), ('asyncio.sleep', immediate_sleep)):
            monkey = patch(target, side_effect=replacement)
            monkey.start()
            self.addCleanup(monkey.stop)


class ClientSafetyTests(unittest.TestCase):
    def setUp(self):
        nh_client._TOKEN_CACHE.clear()
        nh_client._LAST_CALL.clear()
        self.http = SimpleNamespace(post=Mock(), get=Mock(), close=Mock())
        self.session_patch = patch.object(nh_client.requests, 'Session', return_value=self.http)
        self.env_patch = patch.object(nh_client, 'user_environment', side_effect=lambda name, default='': 'fake_shared_app' if name.endswith('KEY') else 'fake_secret')
        self.no_network = patch('requests.sessions.Session.request', side_effect=AssertionError('REAL_HTTP_FORBIDDEN'))
        self.session_patch.start(); self.env_patch.start(); self.no_network.start()
        self.addCleanup(self.session_patch.stop); self.addCleanup(self.env_patch.stop); self.addCleanup(self.no_network.stop)
        self.token_body = {'access_token': 'FAKE_TOKEN_ONLY', 'expires_in': '86400'}

    def client(self, market='domestic'):
        c = nh_client.NHClient(environment='demo', market=market, settings={'read_retries': 2})
        c._throttle = Mock()
        return c

    def test_same_app_key_two_markets_issue_token_once(self):
        self.http.post.return_value = response(self.token_body)
        domestic, overseas = self.client(), self.client('overseas')
        self.assertEqual(domestic.credential_id, overseas.credential_id)
        self.assertEqual(domestic.token(), overseas.token())
        self.assertEqual(self.http.post.call_count, 1)
        sent = self.http.post.call_args.kwargs
        self.assertIn('data', sent)
        self.assertNotIn('json', sent)

    def test_egw023_missing_header_does_not_reissue_token(self):
        self.http.post.side_effect = [response(self.token_body), response({'errorCode': 'EGW023', 'errorMessage': 'Authorization 헤더 누락'})]
        client = self.client()
        with self.assertRaises(TradingError):
            client.price('TEST_SYMBOL')
        self.assertEqual(self.http.post.call_count, 2, 'token 1 + failed quote 1, no token churn')

    def test_order_timeout_is_unknown_and_never_resent(self):
        self.http.post.return_value = response(self.token_body)
        client = self.client(); client.token()
        self.http.post.reset_mock()
        self.http.post.side_effect = nh_client.requests.Timeout('fake timeout')
        with self.assertRaises(TradingError) as caught:
            client.place_order('TEST_SYMBOL', 'BUY', 1)
        self.assertEqual(caught.exception.code, 'UNKNOWN_ORDER')
        self.assertEqual(self.http.post.call_count, 1)

    def test_order_http503_is_unknown_and_never_resent(self):
        self.http.post.side_effect = [response(self.token_body), response({'message': 'fake unavailable'}, 503)]
        client = self.client()
        with self.assertRaises(TradingError) as caught:
            client.place_order('TEST_SYMBOL', 'BUY', 1)
        self.assertEqual(caught.exception.code, 'UNKNOWN_ORDER')
        self.assertEqual(self.http.post.call_count, 2, 'one token and one order only')

    def test_real_account_and_overseas_orders_are_rejected_locally(self):
        real = nh_client.NHClient(environment='real', market='domestic')
        foreign = self.client('overseas')
        for client in (real, foreign):
            with self.assertRaises(TradingError) as caught:
                client.place_order('TEST_SYMBOL', 'BUY', 1)
            self.assertEqual(caught.exception.code, 'CONFIG')
        self.http.post.assert_not_called()

    def test_credentials_are_redacted_from_log_structure_and_urls(self):
        item = redact({'Authorization': 'Bearer NEVER_LOG_THIS', 'occurs1_ac_no': 'ACCOUNT_FAKE',
                       'url': 'wss://example.invalid/ws?access_key=KEY_FAKE', 'qty': 1})
        encoded = json.dumps(item)
        self.assertNotIn('NEVER_LOG_THIS', encoded)
        self.assertNotIn('ACCOUNT_FAKE', encoded)
        self.assertNotIn('KEY_FAKE', encoded)
        self.assertEqual(item['qty'], 1)


class LiveExecutionSafetyTests(NoSocketAsyncCase):
    async def test_observe_mode_never_calls_order_api(self):
        session = fake_live_session(trade=False)
        await session.execute(entry_intent())
        session.domestic.place_order.assert_not_called()
        session.domestic.cancel_order.assert_not_called()
        session.eng.on_fill.assert_not_called()

    async def test_cancel_race_fill_is_recorded_once_from_final_state(self):
        session = fake_live_session()
        session.domestic.reconcile_order.side_effect = [
            {'found': True, 'filled_qty': 0, 'remaining_qty': 1, 'avg_price': None},
            {'found': True, 'filled_qty': 1, 'remaining_qty': 0, 'avg_price': 1330.1},
        ]
        session.domestic.positions.return_value = [{'symbol': 'TEST_SYMBOL', 'qty': 1}]
        await session.execute(entry_intent())
        session.domestic.place_order.assert_called_once()
        session.domestic.cancel_order.assert_called_once()
        session.eng.on_fill.assert_called_once()
        self.assertEqual(session.eng.on_fill.call_args.args[1:3], (1330.1, 1))
        self.assertFalse(session.shutdown.is_set())
        self.assertEqual(session.domestic.reconcile_order.call_count, 2)

    async def test_cancel_ack_with_remaining_quantity_stops_new_entries(self):
        session = fake_live_session()
        session.domestic.reconcile_order.return_value = {'found': True, 'filled_qty': 0, 'remaining_qty': 1, 'avg_price': None}
        await session.execute(entry_intent())
        session.eng.on_fill.assert_not_called()
        self.assertTrue(session.stop_entries)
        self.assertTrue(session.shutdown.is_set())
        self.assertTrue(session.context.metadata.get('unknown_order_requires_review'))
        session.domestic.place_order.assert_called_once()
        self.assertEqual(session.domestic.reconcile_order.call_count, 2)

    async def test_missing_final_state_is_not_assumed_cancelled(self):
        session = fake_live_session()
        session.domestic.reconcile_order.return_value = {'found': False, 'filled_qty': 0, 'remaining_qty': None, 'avg_price': None}
        await session.execute(entry_intent())
        session.eng.on_fill.assert_not_called()
        session.domestic.cancel_order.assert_not_called()
        self.assertTrue(session.shutdown.is_set())

    async def test_unknown_order_response_does_not_create_duplicate(self):
        session = fake_live_session()
        session.domestic.place_order.side_effect = TradingError('UNKNOWN_ORDER', 'fake response lost')
        await session.execute(entry_intent())
        session.domestic.place_order.assert_called_once()
        session.eng.on_fill.assert_not_called()
        self.assertTrue(session.stop_entries)
        self.assertTrue(session.shutdown.is_set())

    async def test_padded_o1_order_number_is_used_before_timeout(self):
        session = fake_live_session()
        session.config['execution']['fill_timeout_seconds'] = .1
        session.feeds = [SimpleNamespace(orders={'42': {'filled_qty': 1, 'remaining_qty': 0, 'avg_price': 1330.1, 'status': '6'}})]
        session.domestic.reconcile_order.return_value = {'found': True, 'filled_qty': 1, 'remaining_qty': 0, 'avg_price': 1330.1}
        session.domestic.positions.return_value = [{'symbol': 'TEST_SYMBOL', 'qty': 1}]
        await session.execute(entry_intent())
        fills = [r for name, r in session.j.records if name == 'fills']
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]['source'], 'O1_plus_REST_final_reconciliation')

    async def test_cleanup_cancel_race_closes_actual_position_not_original_order_size(self):
        session = fake_live_session()
        session.domestic.open_orders.return_value = [{'order_no': '41', 'side': 'BUY', 'remaining_qty': 1}]
        session.domestic.reconcile_order.side_effect = [
            {'found': True, 'filled_qty': 1, 'remaining_qty': 0, 'avg_price': 1330.1},
            {'found': True, 'filled_qty': 1, 'remaining_qty': 0, 'avg_price': 1330.0},
        ]
        session.domestic.positions.side_effect = [[{'symbol': 'TEST_SYMBOL', 'qty': 1}], [], []]
        await session.cleanup()
        session.domestic.cancel_order.assert_called_once()
        session.domestic.place_order.assert_called_once()
        self.assertEqual(session.domestic.place_order.call_args.args[1:3], ('SELL', 1))
        self.assertNotIn('cleanup_error', session.context.metadata)
        self.assertEqual(session.context.metadata['final_broker_position'], 0)

    async def test_cleanup_unknown_post_cancel_state_never_sends_close_order(self):
        session = fake_live_session()
        session.domestic.open_orders.return_value = [{'order_no': '41', 'side': 'BUY', 'remaining_qty': 1}]
        session.domestic.reconcile_order.return_value = {'found': False, 'filled_qty': 0, 'remaining_qty': 0, 'avg_price': None}
        session.domestic.positions.side_effect = [[{'symbol': 'TEST_SYMBOL', 'qty': 1}], [], []]
        await session.cleanup()
        session.domestic.place_order.assert_not_called()
        self.assertIn('cleanup_error', session.context.metadata)


class WebSocketSafetyTests(NoSocketAsyncCase):
    def make_feed(self, **kwargs):
        client = SimpleNamespace(journal=None, base_url='https://example.invalid',
                                 ws_access_key=Mock(return_value='FAKE_SINGLE_USE_KEY'), token=Mock(return_value='FAKE_TOKEN'),
                                 price_scale=lambda symbol: .01 if symbol.startswith('6J') else 1.)
        return RealtimeFeed(client, {'usdkrw': 'TEST_SYMBOL'}, market='domestic', **kwargs)

    async def test_duplicate_o1_fill_is_not_counted_twice(self):
        seen = []
        feed = self.make_feed(on_order=seen.append)
        body = {'ord_no': '0000000042', 'sym': 'TEST_SYMBOL', 'ord_sts': '6', 'trd_div': '1',
                'exec_qty': '1', 'per_exec_qty': '1', 'exec_no': 'E1', 'exec_pric': '1330.1', 'rem_ord_qty': '0'}
        await feed._order(body)
        await feed._order(body)
        self.assertEqual(feed.orders['42']['filled_qty'], 1)
        self.assertAlmostEqual(feed.orders['42']['avg_price'], 1330.1)
        self.assertEqual(len(feed._execs['42']), 1)

    async def test_ping_is_echoed_and_all_subscription_acks_required(self):
        feed = self.make_feed()
        socket = SimpleNamespace(send=AsyncMock())
        ping = json.dumps({'code': 'pingpong', 'nonce': 'fake'})
        await feed._handle(ping, socket)
        socket.send.assert_awaited_once_with(ping)
        for action in ('KA', 'KB'):
            await feed._handle(json.dumps({'code': 'RT_INIT', 'data': {'action_code': action, 'action_sym': 'TEST_SYMBOL'}}), socket)
        self.assertFalse(feed.live, 'O1 confirmation is still missing')
        await feed._handle(json.dumps({'code': 'RT_INIT', 'data': {'action_code': 'O1'}}), socket)
        self.assertTrue(feed.live)

    async def test_nontrade_domestic_message_does_not_trigger_price_decision(self):
        seen = []
        feed = self.make_feed(on_trade=seen.append)
        await feed._trade('TEST_SYMBOL', {'tick_div': '0', 'last_pric': '1330', 'exec_qty': '0', 'exch_tm': '090000'}, 'domestic')
        self.assertEqual(seen, [])

    async def test_foreign_korean_clock_and_price_scale_are_preserved(self):
        seen = []
        client = SimpleNamespace(journal=None, price_scale=lambda symbol: .01)
        feed = RealtimeFeed(client, {'jpy': '6J_FAKE'}, market='overseas', on_trade=seen.append)
        await feed._trade('6J_FAKE', {'ko_trd_dt': '20261126', 'ko_exec_tm': '093001',
                                    'last_pric': '.6306', 'exec_qty': '1'}, 'overseas')
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0]['price'], .006306)
        self.assertEqual(seen[0]['timestamp'].isoformat(), '2026-11-26T09:30:01+09:00')
        self.assertTrue(seen[0]['timestamp_verified'])

    async def test_subscription_failure_reconnects_with_new_key_and_stops_entries(self):
        states = []
        feed = self.make_feed()
        class FakeSocket:
            def __init__(self, messages):
                self.messages = list(messages)
                self.send = AsyncMock()
                self.close = AsyncMock()
            async def recv(self):
                if not self.messages:
                    raise AssertionError('Unexpected extra recv')
                return json.dumps(self.messages.pop(0))
        class FakeContext:
            def __init__(self, socket): self.socket = socket
            async def __aenter__(self): return self.socket
            async def __aexit__(self, *args): return False
        init = {'code': 'INIT', 'msg': 'INIT_SUCCESS'}
        first = FakeSocket([init, {'code': 'RT_FAIL', 'msg': 'fake subscription denial'}])
        second = FakeSocket([init, *[{'code': 'RT_INIT', 'data': {'action_code': ac, 'action_sym': sym}}
                                   for ac, sym in feed.subscriptions]])
        async def status(item):
            states.append(item)
            if item['event'] == 'ready':
                await feed.stop()
        feed.on_status = status
        connect = Mock(side_effect=[FakeContext(first), FakeContext(second)])
        with patch.dict(sys.modules, {'websockets': SimpleNamespace(connect=connect)}), patch.object(ws_module, 'RECONNECT_BACKOFF', (0.,0.,0.,0.,0.)):
            await asyncio.wait_for(feed.run(), timeout=3)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(feed.client.ws_access_key.call_count, 2)
        self.assertTrue(any(s['event'] == 'disconnected' and not s['live'] for s in states))
        self.assertTrue(any(s['event'] == 'reconnecting' for s in states))
        self.assertTrue(any(s['event'] == 'ready' for s in states))
        self.assertFalse(feed.live)


if __name__ == '__main__':
    unittest.main(verbosity=2)
