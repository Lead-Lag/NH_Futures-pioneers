"""계좌 응답 손상을 빈 잔고로 오인하지 않는지 검증합니다.

모든 조회는 메모리 내 가짜 응답만 사용합니다. 환경변수, HTTP 세션,
실제 계좌, 주문 API에는 접근하지 않습니다.
"""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

PROJECT=Path(os.environ.get('HEDGE02_SOURCE',str(Path(__file__).resolve().parents[1])))
sys.path.insert(0,str(PROJECT))
candidate=os.environ.get('HEDGE02_CLIENT_SOURCE')
if candidate:
    spec=importlib.util.spec_from_file_location('_hedge02_account_candidate',candidate)
    nh_client=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=nh_client
    spec.loader.exec_module(nh_client)
else:
    import nh_client
from error import TradingError


def position(symbol='A756C000',qty='1',side='1'):
    return dict(occurs1_sym=symbol,occurs1_opint_qty=qty,occurs1_trd_div=side,occurs1_avg_pric='1,400.5')


def order(**changes):
    row=dict(occurs1_ord_no='0000000042',occurs1_sym='A756C000',occurs1_trd_div='1',
             occurs1_ord_qty='2',occurs1_exec_qty='0',occurs1_cxl_qty='0',occurs1_ord_sts='1')
    row.update(changes)
    return row


class AccountShapeSafetyTests(unittest.TestCase):
    def client(self,*responses):
        client=nh_client.NHClient.__new__(nh_client.NHClient)
        client.market='domestic'
        client.call=Mock(side_effect=list(responses))
        return client

    def assert_reconcile(self,fn):
        with self.assertRaises(TradingError) as raised:fn()
        self.assertEqual(raised.exception.code,'RECONCILE')
        self.assertFalse(raised.exception.retryable)

    def test_explicit_empty_account_is_flat(self):
        client=self.client(dict(nrec='0',Occurs1=[],nxt_key='99999999'))
        self.assertEqual(client.positions(),[])
        client.call.assert_called_once()

    def test_count_zero_supports_documented_empty_object_or_missing_rows(self):
        for body in (dict(nrec='0'),dict(nrec='0',Occurs1={}),dict(nrec='0',Occurs1=None),dict(nrec='0',Occurs1='')):
            with self.subTest(body=body):
                self.assertEqual(self.client(body).positions(),[])

    def test_explicit_typed_empty_list_without_count_is_flat(self):
        self.assertEqual(self.client(dict(Occurs1=[])).positions(),[])

    def test_missing_rows_without_zero_count_cannot_imply_flat(self):
        for body in ({},dict(nxt_key='99999999'),dict(Occurs1=None),dict(Occurs1={})):
            with self.subTest(body=body):self.assert_reconcile(lambda:self.client(body).positions())

    def test_malformed_record_is_not_silently_dropped(self):
        for rows in ([position(),'bad'],[position(),None],[{}],'not-an-array',3):
            with self.subTest(rows=rows):
                self.assert_reconcile(lambda:self.client(dict(Occurs1=rows)).positions())

    def test_non_object_account_body_is_rejected(self):
        for body in ([],None,'bad'):
            with self.subTest(body=body):self.assert_reconcile(lambda:self.client(body).positions())

    def test_invalid_nrec_cannot_be_coerced_to_zero(self):
        for count in ('','bad','NaN','Infinity','-1','0.5',None,True):
            with self.subTest(count=count):
                self.assert_reconcile(lambda:self.client(dict(nrec=count,Occurs1=[])).positions())

    def test_nrec_row_presence_contradiction_is_rejected(self):
        for body in (dict(nrec='0',Occurs1=[position()]),dict(nrec='1',Occurs1=[])):
            with self.subTest(body=body):self.assert_reconcile(lambda:self.client(body).positions())

    def test_single_record_object_is_supported(self):
        result=self.client(dict(nrec='1',Occurs1=position(qty='2'))).positions()
        self.assertEqual([(p['symbol'],p['qty']) for p in result],[('A756C000',2)])

    def test_complete_pages_preserve_both_positions(self):
        client=self.client(dict(nrec='1',Occurs1=[position('AAA')],nxt_key='page2'),
                           dict(nrec='1',Occurs1=[position('BBB',side='2')],nxt_key='99999999'))
        result=client.positions()
        self.assertEqual([(p['symbol'],p['qty']) for p in result],[('AAA',1),('BBB',-1)])
        self.assertEqual([c.args[1]['nxt_key'] for c in client.call.call_args_list],['','page2'])

    def test_repeated_cursor_never_returns_partial_account(self):
        client=self.client(dict(Occurs1=[position('AAA')],nxt_key='same'),
                           dict(Occurs1=[position('BBB')],nxt_key='same'))
        self.assert_reconcile(client.positions)
        self.assertEqual(client.call.call_count,2)

    def test_repeated_page_with_new_cursor_cannot_double_position(self):
        client=self.client(dict(Occurs1=[position()],nxt_key='page2'),
                           dict(Occurs1=[position()],nxt_key='page3'))
        self.assert_reconcile(client.positions)

    def test_empty_intermediate_page_does_not_prove_flat(self):
        client=self.client(dict(nrec='0',Occurs1=[],nxt_key='page2'))
        self.assert_reconcile(client.positions)

    def test_page_limit_never_returns_partial_rows(self):
        client=self.client(dict(Occurs1=[position()],nxt_key='page2'))
        self.assert_reconcile(lambda:client._account_rows('domestic_positions',max_pages=1))

    def test_invalid_position_quantity_never_silently_becomes_flat(self):
        for qty in (None,'','bad','NaN','Infinity','-1','1.25',True):
            with self.subTest(qty=qty):
                client=self.client()
                with patch.object(client,'_account_rows',return_value=[position(qty=qty)]):
                    self.assert_reconcile(client.positions)

    def test_missing_quantity_is_rejected(self):
        row=position();row.pop('occurs1_opint_qty')
        client=self.client()
        with patch.object(client,'_account_rows',return_value=[row]):self.assert_reconcile(client.positions)

    def test_unknown_direction_never_defaults_to_sell(self):
        for side in ('','0','3','SELL',None):
            with self.subTest(side=side):
                client=self.client()
                with patch.object(client,'_account_rows',return_value=[position(side=side)]):self.assert_reconcile(client.positions)

    def test_nonzero_position_requires_symbol(self):
        for symbol in ('',' ',None):
            with self.subTest(symbol=symbol):
                client=self.client()
                with patch.object(client,'_account_rows',return_value=[position(symbol=symbol)]):self.assert_reconcile(client.positions)

    def test_trimmed_sides_keep_signed_exposure(self):
        client=self.client()
        with patch.object(client,'_account_rows',return_value=[position('A',qty='2',side=' 1 '),position('B',qty='3',side=' 2 ')]):
            result=client.positions()
        self.assertEqual([(p['symbol'],p['qty']) for p in result],[('A',2),('B',-3)])

    def test_explicit_zero_position_is_not_traded(self):
        client=self.client()
        with patch.object(client,'_account_rows',return_value=[position(symbol='',qty='0',side='')]):
            self.assertEqual(client.positions(),[])

    def test_open_order_cannot_disappear_when_quantity_is_missing(self):
        for field in ('occurs1_ord_qty','occurs1_exec_qty','occurs1_cxl_qty'):
            row=order();row.pop(field)
            with self.subTest(field=field):
                self.assert_reconcile(self.client(dict(Occurs1=[row])).open_orders)

    def test_order_metadata_cannot_default_unknown_side_to_sell(self):
        for field,value in (('occurs1_ord_no',''),('occurs1_sym',''),('occurs1_trd_div','3'),('occurs1_trd_div',None)):
            with self.subTest(field=field,value=value):
                self.assert_reconcile(self.client(dict(Occurs1=[order(**{field:value})])).open_orders)

    def test_malformed_order_quantities_are_rejected(self):
        for field in ('occurs1_ord_qty','occurs1_exec_qty','occurs1_cxl_qty'):
            for value in ('bad','-1','NaN','0.25',None,True):
                with self.subTest(field=field,value=value):
                    self.assert_reconcile(self.client(dict(Occurs1=[order(**{field:value})])).open_orders)

    def test_zero_order_quantity_does_not_imply_closed(self):
        self.assert_reconcile(self.client(dict(Occurs1=[order(occurs1_ord_qty='0')])).open_orders)

    def test_explicit_empty_exec_and_cancel_are_known_zero(self):
        result=self.client(dict(Occurs1=[order(occurs1_exec_qty='',occurs1_cxl_qty=' ')])).open_orders()
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['remaining_qty'],2)
        self.assertEqual(result[0]['filled_qty'],0)

    def test_contradictory_final_quantities_do_not_become_zero_remaining(self):
        client=self.client(dict(Occurs1=[order(occurs1_exec_qty='2',occurs1_cxl_qty='1')]))
        self.assert_reconcile(client.open_orders)

    def test_order_direction_whitespace_is_normalized(self):
        result=self.client(dict(Occurs1=[order(occurs1_trd_div=' 1 ')])).open_orders()
        self.assertEqual(result[0]['side'],'BUY')

    def test_valid_partial_fill_keeps_positive_remaining(self):
        result=self.client(dict(Occurs1=[order(occurs1_exec_qty='1')])).open_orders()
        self.assertEqual((result[0]['remaining_qty'],result[0]['filled_qty']),(1,1))


if __name__=='__main__':unittest.main()
