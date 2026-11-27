"""NH 실시간 체결·호가·주문통보 수신. 주문은 REST, 확인은 O1 우선입니다.

KA/KB: 국내 주간. FA/FB: 해외 관측. O1: 국내 주문/체결 통보.
같은 APP Key이면 mixed feed 한 세션에서 함께 구독할 수 있습니다.
외부 websocket-client를 쓰지 않고 websockets를 사용해 동명 파일 충돌을 피합니다.
"""
from __future__ import annotations
import asyncio
import inspect
import json
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo
from nh_client import NHClient, KST, num
from error import TradingError, redact

STREAM_PATH='/trade/ws-stream'
MAX_SUBSCRIPTIONS=20
RECONNECT_BACKOFF=(1.,2.,5.,10.,30.)
TRADE_CHANNELS={'domestic':'KA','overseas':'FA'}
QUOTE_CHANNELS={'domestic':'KB','overseas':'FB'}
ORDER_CHANNEL='O1'


def _message(action:str,body:dict)->str:
    return json.dumps({'header':{'api_ver':'1','action':action},'body':body},ensure_ascii=False)


class RealtimeFeed:
    def __init__(self,client:NHClient,symbols:dict[str,Any],market:str='domestic',
                 on_trade:Callable|None=None,on_order:Callable|None=None,on_status:Callable|None=None,
                 on_quote:Callable|None=None,settings:dict|None=None,journal=None):
        self.client,self.market,self.settings=client,market,settings or {}
        self.on_trade,self.on_order,self.on_status,self.on_quote=on_trade,on_order,on_status,on_quote
        self.journal=journal or client.journal
        self.symbols={}
        for name,value in symbols.items():
            spec=value if isinstance(value,dict) else {'symbol':value,'market':self.settings.get('symbol_markets',{}).get(name,market)}
            if spec.get('market') not in TRADE_CHANNELS:raise TradingError('CONFIG',f'{name}의 market은 domestic 또는 overseas여야 합니다.')
            self.symbols[name]={'symbol':str(spec.get('symbol') or spec.get('sym') or ''),'market':spec['market']}
        self.name_of={v['symbol']:k for k,v in self.symbols.items()}
        self.want_quotes=bool(self.settings.get('subscribe_quotes',True))
        self.want_orders=bool(self.settings.get('subscribe_orders',any(v['market']=='domestic' for v in self.symbols.values())))
        self.subscriptions=[]
        for spec in self.symbols.values():
            self.subscriptions.append((TRADE_CHANNELS[spec['market']],spec['symbol']))
            if self.want_quotes:self.subscriptions.append((QUOTE_CHANNELS[spec['market']],spec['symbol']))
        if self.want_orders:self.subscriptions.append((ORDER_CHANNEL,''))
        if len(self.subscriptions)>MAX_SUBSCRIPTIONS:raise TradingError('CONFIG','계정 실시간 구독 상한 20건 초과')
        self._stop_event=asyncio.Event();self._ws=None
        self.live=False;self.connected=False;self.last_message=None;self.live_since=None
        self.quotes:dict[str,dict]={};self.orders:dict[str,dict]={};self._acks=set();self._execs={}
        self.connection_count=0;self.gaps=[dict(start=datetime.now(KST).isoformat(),end=None)]

    async def _emit(self,callback,value:dict):
        if callback:
            result=callback(value)
            if inspect.isawaitable(result):await result

    async def _status(self,event:str,**kw):
        item=dict(event=event,state=event,status=event,market=self.market,connected=self.connected,live=self.live,
                  timestamp=datetime.now(KST),**kw)
        if self.journal:self.journal.event('websocket',**redact({k:v.isoformat() if isinstance(v,datetime) else v for k,v in item.items()}))
        await self._emit(self.on_status,item)

    @property
    def healthy(self)->bool:
        return bool(self.live and self.last_message and (datetime.now(KST)-self.last_message).total_seconds()<float(self.settings.get('idle_timeout_seconds',90)))

    async def stop(self):
        self._stop_event.set()
        if self._ws is not None:
            await self._ws.close()

    async def run(self):
        try:import websockets
        except ImportError:raise TradingError('CONFIG','실시간 실행에는 websockets 패키지가 필요합니다.') from None
        attempt=0
        while not self._stop_event.is_set():
            began=datetime.now(KST)
            try:
                key=await asyncio.to_thread(self.client.ws_access_key)
                url=self.client.base_url.replace('https://','wss://').replace('http://','ws://')+STREAM_PATH+'?access_key='+quote(key,safe='')
                # 서버 JSON pingpong과 라이브러리 제어프레임 ping은 별개입니다.
                async with websockets.connect(url,open_timeout=12,close_timeout=3,ping_interval=None,ping_timeout=None) as ws:
                    self._ws=ws;self.connected=True;self._acks.clear()
                    self.connection_count+=1
                    token=await asyncio.to_thread(self.client.token)
                    await ws.send(_message('0',dict(action_code='session_init',session_token=token)))
                    while True:
                        raw=await asyncio.wait_for(ws.recv(),timeout=12)
                        m=json.loads(raw)
                        code=str(m.get('code') or m.get('rt_cd') or '')
                        if code.lower()=='pingpong':await ws.send(raw);continue
                        if code=='INIT' and str(m.get('msg') or m.get('message'))=='INIT_SUCCESS':break
                        raise TradingError('AUTH','WebSocket 세션 초기화 거절',context=redact(m))
                    for ac,sym in self.subscriptions:
                        body={'action_code':ac}
                        if sym:body['action_sym']=sym
                        await ws.send(_message('A',body))
                    await self._status('connected',subscriptions=len(self.subscriptions),connection=self.connection_count)
                    while not self._stop_event.is_set():
                        raw=await asyncio.wait_for(ws.recv(),timeout=float(self.settings.get('idle_timeout_seconds',90)))
                        self.last_message=datetime.now(KST)
                        await self._handle(raw,ws)
                        if not self.live and (self.last_message-began).total_seconds()>float(self.settings.get('subscription_timeout_seconds',30)):
                            raise TradingError('WS_DISCONNECTED','구독 확인을 모두 받지 못했습니다.',context={'pending':[list(x) for x in set(self.subscriptions)-self._acks]})
            except asyncio.CancelledError:raise
            except Exception as exc:
                if not self._stop_event.is_set():
                    self.live=False;self.connected=False
                    await self._status('disconnected',error=str(redact(str(exc)))[:300])
            finally:
                self.connected=False;self.live=False;self._ws=None
                if not self._stop_event.is_set() and (not self.gaps or self.gaps[-1]['end'] is not None):
                    self.gaps.append(dict(start=datetime.now(KST).isoformat(),end=None))
            if self._stop_event.is_set():break
            attempt=0 if (datetime.now(KST)-began).total_seconds()>60 else min(attempt+1,len(RECONNECT_BACKOFF)-1)
            delay=RECONNECT_BACKOFF[attempt]
            await self._status('reconnecting',delay_seconds=delay)
            try:await asyncio.wait_for(self._stop_event.wait(),timeout=delay)
            except asyncio.TimeoutError:pass
        await self._status('stopped')

    async def _handle(self,raw:str,ws):
        try:m=json.loads(raw)
        except (ValueError,TypeError):
            await self._status('invalid_json');return
        if not isinstance(m,dict):return
        code=str(m.get('code') or m.get('rt_cd') or '')
        if code.lower()=='pingpong':await ws.send(raw);return
        if code:
            data=m.get('data') or m.get('body') or m
            if not isinstance(data,dict):data={}
            if code=='RT_INIT':
                ac=str(data.get('action_code') or '');sym=str(data.get('action_sym') or '')
                if ac==ORDER_CHANNEL:sym=''
                if ac:self._acks.add((ac,sym))
                await self._check_ready()
                await self._status('subscription_ack',action_code=ac,symbol=sym)
            elif code in ('RT_FAIL','INIT_FAIL') or 'FAIL' in code:
                raise TradingError('WS_DISCONNECTED','실시간 구독 거절',context=redact(m))
            else:await self._status('server_message',server_code=code,message=str(redact(m.get('msg') or m.get('message') or ''))[:200])
            return
        h,b=m.get('header') or {},m.get('body') or {}
        if not isinstance(h,dict) or not isinstance(b,dict):return
        ac=str(h.get('action_code') or '')
        sym=str(h.get('action_sym') or b.get('sym') or '')
        if ac in ('KA','KC','FA'):
            self._acks.add(('KA' if ac=='KC' else ac,sym));await self._check_ready()
            await self._trade(sym,b,'overseas' if ac=='FA' else 'domestic')
        elif ac in ('KB','KD','FB'):
            self._acks.add(('KB' if ac=='KD' else ac,sym));await self._check_ready()
            await self._quote(sym,b,'overseas' if ac=='FB' else 'domestic')
        elif ac==ORDER_CHANNEL:
            self._acks.add((ORDER_CHANNEL,''));await self._check_ready();await self._order(b)

    async def _check_ready(self):
        if not self.live and set(self.subscriptions).issubset(self._acks):
            self.live=True;self.live_since=datetime.now(KST)
            if self.gaps and self.gaps[-1]['end'] is None:self.gaps[-1]['end']=self.live_since.isoformat()
            await self._status('ready',connection=self.connection_count)

    def _timestamp(self,b:dict,market:str)->tuple[datetime,bool]:
        now=datetime.now(KST)
        raw_day=str(b.get('ko_trd_dt') or '').strip() if market=='overseas' else ''
        raw_time=str(b.get('ko_exec_tm') or '').strip() if market=='overseas' else str(b.get('exch_tm') or '').strip()
        digits=''.join(c for c in raw_time if c.isdigit())
        if len(digits)>=14:raw_day,digits=digits[:8],digits[8:14]
        if len(digits)>=6:
            try:
                dt=datetime.strptime((raw_day or now.strftime('%Y%m%d'))+digits[:6],'%Y%m%d%H%M%S').replace(tzinfo=KST)
                if not raw_day:
                    if dt-now>timedelta(hours=12):dt-=timedelta(days=1)
                    if now-dt>timedelta(hours=12):dt+=timedelta(days=1)
                return dt,True
            except ValueError:pass
        if market=='overseas':
            # 한국시각이 없으면 exchange zone을 사용하되 가정임을 표시합니다.
            day=str(b.get('ref_dt') or '').strip();tm=str(b.get('exch_tm') or '').strip()
            try:
                zone=ZoneInfo(str(self.settings.get('exchange_timezone','America/Chicago')))
                return datetime.strptime(day+tm[:6],'%Y%m%d%H%M%S').replace(tzinfo=zone).astimezone(KST),False
            except (ValueError,KeyError):pass
        return now,False

    async def _trade(self,sym:str,b:dict,market:str):
        name=self.name_of.get(sym)
        if name is None:return
        received=datetime.now(KST);ts,verified=self._timestamp(b,market)
        tick_div=str(b.get('tick_div') or '')
        scale=self.client.price_scale(sym) if market=='overseas' else 1.0
        px,qty=num(b.get('last_pric'))*scale,num(b.get('exec_qty'))
        valid=px>0 and qty>0 and (market!='domestic' or tick_div=='1')
        item=dict(name=name,symbol=sym,market=market,timestamp=ts,received_at=received,price=px,volume=qty,
                  bid=num(b.get('bid_best_pric'))*scale,ask=num(b.get('ask_best_pric'))*scale,timestamp_verified=verified,
                  tick_div=tick_div,valid_trade=valid,sequence=str(b.get('seq_no') or ''))
        previous=self.quotes.get(sym,{})
        self.quotes[sym]={**previous,**item}
        if self.journal and self.on_trade is None:self.journal.row('ticks',**{k:v.isoformat() if isinstance(v,datetime) else v for k,v in item.items()})
        # 유효 거래만 봉과 손절 판단으로 보냅니다. 수신시각 대체는 미확인으로 표시합니다.
        if valid:await self._emit(self.on_trade,item)

    async def _quote(self,sym:str,b:dict,market:str):
        name=self.name_of.get(sym)
        if name is None:return
        ts,verified=self._timestamp(b,market);received=datetime.now(KST)
        first=(b.get('Occurs1') or [{}])
        if isinstance(first,list):first=first[0] if first else {}
        if not isinstance(first,dict):first={}
        def v(*keys):
            for key in keys:
                for row in (b,first):
                    if row.get(key) not in (None,''):return num(row[key])
            return 0.
        scale=self.client.price_scale(sym) if market=='overseas' else 1.0
        item=dict(name=name,symbol=sym,market=market,timestamp=ts,received_at=received,
            bid=v('bid_pric_1','occurs1_bid_best_pric','bid_best_pric')*scale,ask=v('ask_pric_1','occurs1_ask_best_pric','ask_best_pric')*scale,
            bid_qty=v('bid_qty_1','occurs1_bid_qty'),ask_qty=v('ask_qty_1','occurs1_ask_qty'),timestamp_verified=verified)
        self.quotes[sym]={**self.quotes.get(sym,{}),**item}
        if self.journal and self.on_quote is None:self.journal.row('quotes',**{k:v.isoformat() if isinstance(v,datetime) else v for k,v in item.items()})
        await self._emit(self.on_quote,item)

    async def _order(self,b:dict):
        no=str(b.get('ord_no') or '').strip().lstrip('0')
        if not no:return
        received=datetime.now(KST)
        old=self.orders.get(no,{})
        eno=str(b.get('exec_no') or '').strip();per=num(b.get('per_exec_qty'));px=num(b.get('exec_pric'))
        execs=self._execs.setdefault(no,{})
        if eno and per>0 and px>0:execs[eno]=(per,px)
        total=sum(q for q,p in execs.values());filled=int(max(num(b.get('exec_qty')),old.get('filled_qty',0),total))
        avg=num(b.get('exec_avg_pric')) or (sum(q*p for q,p in execs.values())/total if total>=filled and total>0 else old.get('avg_price'))
        if not avg and filled==1 and px>0:avg=px
        raw_remaining=b.get('rem_ord_qty')
        remaining=int(num(raw_remaining)) if raw_remaining not in (None,'') else old.get('remaining_qty')
        item=dict(order_no=no,symbol=str(b.get('sym') or ''),filled_qty=filled,remaining_qty=remaining,
            avg_price=avg,status=str(b.get('ord_sts') or old.get('status') or ''),received_at=received,
            side='BUY' if str(b.get('trd_div'))=='1' else 'SELL',cancelled_qty=int(num(b.get('cxl_qty'))),
            per_exec_qty=per,exec_no=eno,error_code=str(b.get('err_cd') or '').strip())
        self.orders[no]=item
        if self.journal and self.on_order is None:self.journal.row('ws_orders',**{k:v.isoformat() if isinstance(v,datetime) else v for k,v in item.items()})
        await self._emit(self.on_order,item)
