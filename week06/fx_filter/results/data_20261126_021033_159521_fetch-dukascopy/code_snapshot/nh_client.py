"""NH선물 REST. 상수는 API개발/전체_API_문서.xlsx 기준입니다.

동일 서버·APP Key 토큰은 프로세스 내 공유합니다. 해외 클라이언트는 관측용,
국내 주문은 모의계좌만 허용합니다. 응답 불명 주문은 절대 재전송하지 않습니다.
"""
from __future__ import annotations
import hashlib
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo
import requests
from error import TradingError, redact

KST = timezone(timedelta(hours=9))
BASE_URLS = {'demo':'https://apidemo.futures.co.kr','paper':'https://apidemo.futures.co.kr','real':'https://api.futures.co.kr'}
TOKEN_PATH = '/auth-service/v1/token'
WS_KEY_PATH = '/permission/v1/web-socket-key'
FIRST_KEY = '99999999'
OK_CODES = {'','0','EGW002','EGW013'}
MIN_INTERVAL = {'quote':.12,'account':.12,'order':.25}
API_PATHS = {
 'domestic_price':('/trade/v1/domestic/price','quote'),
 'domestic_quote':('/trade/v1/domestic/price-bid','quote'),
 'domestic_minute':('/trade/v1/domestic/chart-minute','quote'),
 'domestic_daily':('/trade/v1/domestic/chart-daily','quote'),
 'domestic_order':('/trade/v1/domestic/order','order'),
 'domestic_cancel':('/trade/v1/domestic/cancel','order'),
 'domestic_history':('/trade/v1/domestic/order-history','account'),
 'domestic_positions':('/trade/v1/domestic/open-interest','account'),
 'domestic_orderable':('/trade/v1/domestic/orderable-quantity','account'),
 'overseas_price':('/trade/v1/overseas/price','quote'),
 'overseas_quote':('/trade/v1/overseas/bid-price','quote'),
 'overseas_minute':('/trade/v1/overseas/chart-minute','quote'),
 'overseas_daily':('/trade/v1/overseas/chart-daily','quote'),
}
_TOKEN_CACHE:dict[str,dict] = {}
_LAST_CALL:dict[tuple,float] = {}
_AUTH_LOCK = threading.RLock()
_RATE_LOCK = threading.Lock()

def user_environment(name:str,default:str='')->str:
    """Windows 사용자 변수를 우선 읽어 새로 수정한 값을 반영합니다."""
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
                value,_ = winreg.QueryValueEx(key,name)
                if str(value).strip(): return os.path.expandvars(str(value)).strip()
        except (OSError,ImportError): pass
    return os.environ.get(name,default).strip()

def num(value:Any,default:float=0.)->float:
    try: return float(str(value).strip().replace(',',''))
    except (ValueError,TypeError): return default

def rows_of(body:dict,key:str='Occurs1')->list[dict]:
    rows=body.get(key,[])
    if isinstance(rows,dict): return [rows]
    return [r for r in rows if isinstance(r,dict)] if isinstance(rows,list) else []

def as_datetime(value:Any)->datetime:
    if isinstance(value,datetime): dt=value
    elif isinstance(value,date): dt=datetime.combine(value,datetime.min.time())
    else:
        raw=str(value)
        dt=datetime.strptime(raw,'%Y%m%d') if len(raw)==8 and raw.isdigit() else datetime.fromisoformat(raw.replace('Z','+00:00'))
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)

def _failure(body:dict)->tuple[str,str]|None:
    for ck,mk in (('errorCode','errorMessage'),('msg_cd','msg_cntn'),('rsp_cd','rsp_msg')):
        code=str(body.get(ck) or '').strip()
        if code not in OK_CODES:
            return code,str(body.get(mk) or body.get('errorMsg') or body.get('message') or body.get('msg') or 'API 오류 응답')
    return None

def _auth_rejection(code:str,message:str,status:int)->bool:
    text=message.lower()
    # EGW023은 문서상 Authorization 누락. 코드만 보고 재발급하지 않습니다.
    if any(s in text for s in ('헤더 누락','header missing','authorization header','app key','appkey','appsecret')): return False
    return (status==401 or '토큰' in text or 'token' in text) and any(s in text for s in ('만료','유효하지','invalid','expired','폐기'))

@dataclass(repr=False)
class Credentials:
    environment:str
    app_key:str
    app_secret:str
    base_url:str
    @property
    def url(self)->str: return self.base_url

class NHClient:
    def __init__(self,environment:str='demo',market:str='domestic',journal=None,settings:dict|None=None,**kwargs):
        settings=settings or {}
        if environment not in BASE_URLS or market not in ('domestic','overseas'):
            raise TradingError('CONFIG','environment 또는 market 설정 오류')
        prefix='NH_OVS_' if market=='overseas' else 'NH_'
        key_name=settings.get('app_key_env',prefix+'APP_KEY')
        secret_name=settings.get('app_secret_env',prefix+'APP_SECRET')
        key,secret=user_environment(key_name),user_environment(secret_name)
        if not key or not secret: raise TradingError('CONFIG',f'사용자 환경변수 {key_name}, {secret_name}을 확인하세요.')
        self.base_url=BASE_URLS[environment]
        self.creds=Credentials(environment,key,secret,self.base_url)
        self.environment,self.market,self.settings,self.journal=environment,market,settings,journal
        self.http=requests.Session()
        self.timeout=float(settings.get('timeout_seconds',12))
        self.read_retries=int(settings.get('read_retries',2))
        self._fingerprint=hashlib.sha256((self.base_url+':'+key).encode()).hexdigest()

    @property
    def credential_id(self)->str: return self._fingerprint[:16]

    def _log(self,api:str,started:float,status:int|None,code:str='',message:str=''):
        if self.journal:
            self.journal.row('api_calls',ts=datetime.now(KST).isoformat(),market=self.market,api=api,
                http_status=status or '',latency_ms=round((time.perf_counter()-started)*1000,2),
                error_code=code,message=str(redact(message))[:250])

    def _throttle(self,grade:str):
        with _RATE_LOCK:
            key=(self._fingerprint,grade)
            wait=MIN_INTERVAL[grade]-(time.monotonic()-_LAST_CALL.get(key,0))
            if wait>0: time.sleep(wait)
            _LAST_CALL[key]=time.monotonic()

    def token(self,force:bool=False)->str:
        with _AUTH_LOCK:
            now=datetime.now(KST); cached=_TOKEN_CACHE.get(self._fingerprint)
            if cached and now<cached['expires'] and not force: return cached['token']
            if force and cached and (now-cached['issued']).total_seconds()<10:
                raise TradingError('AUTH','인증 거절 반복: 10초 이내 재발급 중단')
            started=time.perf_counter()
            try:
                response=self.http.post(self.base_url+TOKEN_PATH,
                    headers={'Content-Type':'application/x-www-form-urlencoded'},
                    data={'grant_type':'client_credentials','appkey':self.creds.app_key,'appsecret':self.creds.app_secret},timeout=self.timeout)
                body=response.json()
            except requests.RequestException as exc:
                self._log('token',started,None,'TRANSPORT',type(exc).__name__)
                raise TradingError('TRANSPORT','접근토큰 응답을 받지 못했습니다.') from None
            except ValueError: raise TradingError('AUTH','접근토큰 응답이 JSON이 아닙니다.') from None
            if response.status_code>=400 or not isinstance(body,dict) or not body.get('access_token'):
                trouble=_failure(body) if isinstance(body,dict) else None
                code,message=trouble or (str(response.status_code),'토큰 발급 거부')
                self._log('token',started,response.status_code,code,message)
                raise TradingError('AUTH',message,context={'api_code':code})
            try: expires=as_datetime(str(body.get('expires_at') or ''))
            except (ValueError,TypeError): expires=now+timedelta(seconds=num(body.get('expires_in'),86400))
            _TOKEN_CACHE[self._fingerprint]={'token':str(body['access_token']),'expires':expires,'issued':now}
            self._log('token',started,response.status_code)
            return str(body['access_token'])

    @property
    def session_token(self)->str: return self.token()

    def call(self,api:str,payload:dict|None=None)->dict:
        if api not in API_PATHS: raise TradingError('CONFIG',f'등록되지 않은 API: {api}')
        path,grade=API_PATHS[api]; order=grade=='order'; refreshed=False
        for attempt in range(1 if order else self.read_retries+1):
            self._throttle(grade); token=self.token(); started=time.perf_counter()
            try:
                response=self.http.post(self.base_url+path,
                    headers={'Content-Type':'application/json','Authorization':'Bearer '+token},json=payload or {},timeout=self.timeout)
            except requests.RequestException as exc:
                self._log(api,started,None,'TRANSPORT',type(exc).__name__)
                if order: raise TradingError('UNKNOWN_ORDER','주문 응답 불명: 주문내역 확인 전 재전송 금지',context={'api':api}) from None
                if attempt<self.read_retries:
                    time.sleep(min(2**attempt,4)); continue
                raise TradingError('TRANSPORT',f'{api} 조회 응답을 받지 못했습니다.') from None
            try: body=response.json()
            except ValueError: body={}
            if not isinstance(body,dict): body={}
            code,message=_failure(body) or ('','')
            self._log(api,started,response.status_code,code,message)
            if order and (response.status_code>=500 or not body):
                raise TradingError('UNKNOWN_ORDER','주문 접수 여부를 판단할 수 없는 응답',context={'api':api,'http_status':response.status_code})
            if not order and not refreshed and _auth_rejection(code,message,response.status_code) and attempt<self.read_retries:
                self.token(force=True); refreshed=True; continue
            if code=='EGW036' or response.status_code==429:
                if not order and attempt<self.read_retries:
                    time.sleep(min(2**attempt,4)); continue
                raise TradingError('RATE_LIMIT',message or '호출 제한 도달',retryable=not order)
            if code or response.status_code>=400:
                cat='ORDER_REJECTED' if order else 'AUTH' if response.status_code in (401,403) else 'DATA'
                raise TradingError(cat,message or f'HTTP {response.status_code}',context={'api':api,'api_code':code})
            if not body: raise TradingError('DATA',f'{api} 응답이 비었거나 JSON 형식이 아닙니다.')
            return body
        raise TradingError('TRANSPORT',f'{api} 조회 상한 초과')

    def price(self,sym:str,exchange:str|None=None)->dict:
        payload={'sym':sym} if self.market=='domestic' else {'Iccurs1':[{'iccurs1_exch_cd':exchange or 'FCME','iccurs1_sym':sym}]}
        return self.call(self.market+'_price',payload)

    def quote(self,sym:str,exchange:str|None=None)->dict:
        payload={'sym':sym,'exch_cd':'KSP'} if self.market=='domestic' else {'sym':sym,'exch_cd':exchange or 'FCME'}
        body=self.call(self.market+'_quote',payload); records=rows_of(body); first=records[0] if records else {}
        def val(*keys):
            for key in keys:
                for row in (body,first):
                    if row.get(key) not in (None,''): return num(row[key])
            return 0.
        return dict(symbol=sym,bid=val('bid_pric_1','occurs1_bid_best_pric','bid_best_pric'),
            ask=val('ask_pric_1','occurs1_ask_best_pric','ask_best_pric'),
            bid_qty=val('bid_qty_1','occurs1_bid_qty'),ask_qty=val('ask_qty_1','occurs1_ask_qty'),
            last=val('last_pric'),received_at=datetime.now(KST).isoformat(),raw=redact(body))

    def chart_minute(self,sym:str,start:datetime,end:datetime,exchange:str|None=None,interval:int=1,
                     next_key:str=FIRST_KEY,cidx_yn:str='0',fut_cidx_exp_mod:str='0')->dict:
        p=dict(sym=sym,inq_strt_dt=start.strftime('%Y%m%d'),inq_ed_dt=end.strftime('%Y%m%d'),
            strt_tm=start.strftime('%H%M%S'),ed_tm=end.strftime('%H%M%S'),dcnt=str(interval),
            req_qty='495' if self.market=='overseas' else '2000',nxt_key=next_key,cidx_yn=str(cidx_yn))
        if self.market=='domestic': p.update(nght_tp='1',fut_cidx_exp_mod=str(fut_cidx_exp_mod))
        else: p['exch_cd']=exchange or 'FCME'
        return self.call(self.market+'_minute',p)

    def minute_bars(self,sym:str,start:Any,end:Any=None,exchange:str|None=None,interval:int=1,max_pages:int=500,
                    cidx_yn:str|None=None,fut_cidx_exp_mod:str|None=None)->list[dict]:
        """최대240분 창으로 나눠 조회. ISO8601 KST start/OHLCV 반환, 중복 제거.
        chart_timezone은 과거차트 ref_dt+exch_tm의 시간대(기본 Asia/Seoul).
        """
        lo=as_datetime(start); hi=as_datetime(end) if end is not None else lo.replace(hour=23,minute=59,second=59)
        if end is not None and ((isinstance(end,date) and not isinstance(end,datetime)) or len(str(end)) in (8,10)):
            hi=hi.replace(hour=23,minute=59,second=59)
        if hi<lo: raise TradingError('CONFIG','분봉 종료시각이 시작보다 빠릅니다.')
        result={}; current=lo; pages=0
        while current<=hi:
            pages+=1
            if pages>max_pages: raise TradingError('DATA',f'분봉 시간창 상한 {max_pages}회 도달')
            stop=min(hi,current+timedelta(minutes=240)-timedelta(seconds=1))
            body=self.chart_minute(sym,current,stop,exchange,interval,
                cidx_yn=str(cidx_yn if cidx_yn is not None else self.settings.get('cidx_yn','0')),
                fut_cidx_exp_mod=str(fut_cidx_exp_mod if fut_cidx_exp_mod is not None else self.settings.get('fut_cidx_exp_mod','0')))
            for row in rows_of(body):
                b=self._parse_bar(row)
                if b and current<=as_datetime(b['start'])<=stop: result[b['start']]=b
            current=stop+timedelta(seconds=1)
        return [result[k] for k in sorted(result)]

    def _parse_bar(self,row:dict,daily:bool=False)->dict|None:
        day=str(row.get('occurs1_ref_dt') or row.get('ref_dt') or '').strip()
        tm='000000' if daily else str(row.get('occurs1_exch_tm') or row.get('exch_tm') or '').zfill(6)
        try: naive=datetime.strptime(day+tm,'%Y%m%d%H%M%S')
        except ValueError: return None
        zone=str(self.settings.get('chart_timezone','Asia/Seoul'))
        tz=KST if zone in ('Asia/Seoul','KST','+09:00') else ZoneInfo(zone)
        b={'start':naive.replace(tzinfo=tz).astimezone(KST).isoformat(),'volume':num(row.get('occurs1_cum_trd_qty'))}
        for field in ('open','high','low','close'):
            px=num(row.get('occurs1_'+field+'_pric'))
            if px<=0: return None
            b[field]=px
        b['timestamp_basis']='ref_dt+exch_tm:'+zone
        b['foreign_business_date']=str(row.get('occurs1_glb_biz_dt') or '')
        return b

    def daily_bars(self,sym:str,start:Any,end:Any,exchange:str|None=None,max_pages:int=20)->list[dict]:
        lo,hi=as_datetime(start),as_datetime(end); key=FIRST_KEY; seen=set(); result={}
        for _ in range(max_pages):
            p=dict(sym=sym,inq_strt_dt=lo.strftime('%Y%m%d'),inq_ed_dt=hi.strftime('%Y%m%d'),
                req_qty='554' if self.market=='overseas' else '999',cidx_yn='0',nxt_key=key)
            if self.market=='domestic': p.update(nght_tp='1',fut_cidx_exp_mod='0')
            else: p['exch_cd']=exchange or 'FCME'
            body=self.call(self.market+'_daily',p)
            for row in rows_of(body):
                b=self._parse_bar(row,True)
                if b and lo.date()<=as_datetime(b['start']).date()<=hi.date():result[b['start']]=b
            nxt=str(body.get('nxt_key') or '').strip()
            if not nxt or set(nxt)=={'9'} or nxt in seen:break
            seen.add(nxt);key=nxt
        return [result[t] for t in sorted(result)]

    def _domestic_only(self):
        if self.market!='domestic':raise TradingError('CONFIG','해외 클라이언트는 관측 전용입니다.')

    def place_order(self,symbol:str,side:str,qty:int,order_type:str='market',price:float|None=None,client_tag:str|None=None)->dict:
        self._domestic_only();side=side.upper()
        if self.environment not in ('demo','paper'):raise TradingError('CONFIG','모의계좌 주문만 허용합니다.')
        if side not in ('BUY','SELL') or int(qty)!=qty or qty<1:raise TradingError('CONFIG','주문 방향·수량 오류')
        if order_type not in ('market','limit') or (order_type=='limit' and (price is None or price<=0)):raise TradingError('CONFIG','주문 유형·지정가격 오류')
        body=self.call('domestic_order',dict(sym=symbol,trd_div='1' if side=='BUY' else '2',
            ord_tp_cd='2' if order_type=='market' else '1',ord_qty=str(qty),ord_pric='0' if order_type=='market' else str(price),exec_cnd_cd='1'))
        no=str(body.get('ord_no') or '').strip()
        if not no:raise TradingError('UNKNOWN_ORDER','주문번호 없는 응답: 주문내역 대조 필요')
        return dict(order_no=no,ord_no=no,symbol=symbol,side=side,qty=qty,client_tag=client_tag,raw=redact(body))

    def cancel_order(self,order_no:str,symbol:str,side:str,qty:int)->dict:
        self._domestic_only()
        if self.environment not in ('demo','paper'):raise TradingError('CONFIG','모의계좌 취소만 허용합니다.')
        if qty<=0:raise TradingError('CONFIG','최종 잔량 확인 후 양수 수량만 취소하세요.')
        return self.call('domestic_cancel',dict(sym=symbol,trd_div='1' if side.upper()=='BUY' else '2',ord_qty=str(int(qty)),org_ord_no=str(order_no).zfill(10)))

    def _account_rows(self,api:str,payload:dict|None=None,max_pages:int=20)->list[dict]:
        self._domestic_only();result=[];key='';seen=set()
        for _ in range(max_pages):
            body=self.call(api,{**(payload or {}),'nxt_key':key});result.extend(rows_of(body))
            nxt=str(body.get('nxt_key') or '').strip()
            if not nxt or set(nxt)=={'9'} or nxt in seen:break
            seen.add(nxt);key=nxt
        else:raise TradingError('RECONCILE','계좌 연속조회 상한 도달: 완전한 상태 확인 실패')
        return result

    def order_rows(self)->list[dict]:
        return self._account_rows('domestic_history',dict(exec_div='0',trd_div='0',trd_tm_div_cd='1',ord_div_cd='0'))

    def order_status(self,order_no:str,symbol:str|None=None)->dict:
        no=str(order_no).lstrip('0')
        mine=[r for r in self.order_rows() if str(r.get('occurs1_ord_no') or '').lstrip('0')==no and (not symbol or str(r.get('occurs1_sym') or '').strip()==symbol)]
        if not mine:return dict(order_no=str(order_no),found=False,filled=0,remaining=None,avg_price=None)
        execs={}
        for r in mine:
            eno=str(r.get('occurs1_exch_exec_no') or '').strip();per=num(r.get('occurs1_per_exec_qty'));px=num(r.get('occurs1_exec_pric'))
            if eno and per>0 and px>0:execs[eno]=(per,px)
        total=sum(q for q,p in execs.values());filled=int(max(max(num(r.get('occurs1_exec_qty')) for r in mine),total))
        best=max(mine,key=lambda r:num(r.get('occurs1_exec_qty')))
        avg=sum(q*p for q,p in execs.values())/total if total and total>=filled else None
        if avg is None and filled==1:avg=num(best.get('occurs1_exec_pric')) or None
        cancelled=int(max(num(r.get('occurs1_cxl_qty')) for r in mine));ordered=int(max(num(r.get('occurs1_ord_qty')) for r in mine))
        rem=max(0,ordered-filled-cancelled) if ordered else None
        status=str(best.get('occurs1_ord_sts') or '');reject=next((str(r.get('occurs1_ord_rejt_rsn')) for r in mine if r.get('occurs1_ord_rejt_rsn')),'')
        return dict(order_no=str(order_no),found=True,symbol=str(best.get('occurs1_sym') or ''),filled=filled,remaining=rem,
            cancelled=cancelled,qty=ordered,avg_price=avg,status=status,reject=reject,terminal=rem==0 or status in ('6','8','9'),raw=redact(mine))

    def positions(self)->list[dict]:
        result=[]
        for r in self._account_rows('domestic_positions'):
            q=int(num(r.get('occurs1_opint_qty')))
            if q:result.append(dict(symbol=str(r.get('occurs1_sym') or '').strip(),qty=q if str(r.get('occurs1_trd_div'))=='1' else -q,
                avg_price=num(r.get('occurs1_avg_pric')),raw=redact(r)))
        return result

    def position_of(self,symbol:str)->int:return sum(p['qty'] for p in self.positions() if p['symbol']==symbol)

    def open_orders(self,symbol:str|None=None)->list[dict]:
        rows=self.order_rows();ids={str(r.get('occurs1_ord_no') or '') for r in rows if not symbol or str(r.get('occurs1_sym') or '').strip()==symbol};result=[]
        for no in ids:
            mine=[r for r in rows if str(r.get('occurs1_ord_no') or '')==no]
            ordered=max(int(num(r.get('occurs1_ord_qty'))) for r in mine);filled=max(int(num(r.get('occurs1_exec_qty'))) for r in mine);cancelled=max(int(num(r.get('occurs1_cxl_qty'))) for r in mine)
            rem=max(0,ordered-filled-cancelled)
            if no and rem>0 and not any(str(r.get('occurs1_ord_sts'))=='9' for r in mine):result.append(dict(order_no=no,symbol=str(mine[0].get('occurs1_sym') or ''),
                side='BUY' if str(mine[0].get('occurs1_trd_div'))=='1' else 'SELL',remaining=rem,filled=filled,remaining_qty=rem,filled_qty=filled))
        return result

    def orderable_quantity(self,symbol:str,side:str,price:float=0)->dict:
        return self.call('domestic_orderable',dict(sym=symbol,trd_div='1' if side.upper()=='BUY' else '2',ord_tp_cd='2' if not price else '1',ord_pric=str(price)))

    def reconcile_order(self,order_no:str,symbol:str)->dict:
        order=self.order_status(order_no,symbol)
        return dict(order=order,open_orders=self.open_orders(symbol),positions=[p for p in self.positions() if p['symbol']==symbol],
                    filled_qty=order.get('filled',0),remaining_qty=order.get('remaining'),avg_price=order.get('avg_price'),
                    status=order.get('status',''),found=order.get('found',False),terminal=order.get('terminal',False))

    def lookup_uncertain_order(self,symbol:str,side:str,qty:int,since:datetime)->dict:
        candidates={}
        for r in self.order_rows():
            if str(r.get('occurs1_sym') or '').strip()!=symbol or str(r.get('occurs1_trd_div'))!=('1' if side.upper()=='BUY' else '2') or int(num(r.get('occurs1_ord_qty')))!=qty:continue
            raw=str(r.get('occurs1_exch_rcv_tm') or '').strip();day=str(r.get('occurs1_trd_dt') or since.strftime('%Y%m%d'))
            try:dt=datetime.strptime(day+raw[:6],'%Y%m%d%H%M%S').replace(tzinfo=KST)
            except ValueError:continue
            if dt>=as_datetime(since)-timedelta(seconds=2):
                no=str(r.get('occurs1_ord_no') or '');candidates[no]=dict(order_no=no,received_at=dt.isoformat())
        return dict(candidates=list(candidates.values()),unique=len(candidates)==1,guidance='후보가 유일해도 잔고 대조 후 확정; 신규 주문 재전송 금지')

    def ws_access_key(self)->str:
        self._throttle('account');started=time.perf_counter()
        try:
            response=self.http.get(self.base_url+WS_KEY_PATH,headers={'Authorization':'Bearer '+self.token()},timeout=self.timeout);body=response.json()
        except (requests.RequestException,ValueError):raise TradingError('TRANSPORT','WebSocket 접속키 응답 불명') from None
        self._log('web-socket-key',started,response.status_code)
        if response.status_code>=400 or not isinstance(body,dict) or not body.get('access_key'):raise TradingError('AUTH','WebSocket 접속키 발급 거부')
        return str(body['access_key'])
    websocket_key=ws_access_key
    def close(self):self.http.close()
