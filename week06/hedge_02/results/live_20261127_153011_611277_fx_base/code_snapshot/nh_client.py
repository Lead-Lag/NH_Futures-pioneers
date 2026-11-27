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
from decimal import Decimal, InvalidOperation
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

def _account_integer(value:Any,field:str)->int:
    """계좌 수량·건수에는 숫자 변환 실패를 0으로 대체하지 않습니다."""
    if value is None or isinstance(value,bool):
        raise TradingError('RECONCILE',f'계좌 응답 {field} 누락 또는 숫자 형식 오류')
    try:
        amount=Decimal(str(value).strip().replace(',',''))
    except (InvalidOperation,ValueError,TypeError):
        raise TradingError('RECONCILE',f'계좌 응답 {field} 숫자 형식 오류') from None
    if not amount.is_finite() or amount<0 or amount!=amount.to_integral_value():
        raise TradingError('RECONCILE',f'계좌 응답 {field}는 음수가 아닌 정수여야 합니다.')
    return int(amount)

def _account_result_rows(body:Any)->list[dict]:
    """빈 계좌와 응답 스키마 손상을 구분합니다. 부분 행은 버리지 않습니다."""
    if not isinstance(body,dict):
        raise TradingError('RECONCILE','계좌 응답 본문이 객체가 아닙니다.')
    count=_account_integer(body['nrec'],'nrec') if 'nrec' in body else None
    raw=body.get('Occurs1')
    if raw is None or raw=='' or raw=={}:
        if count==0:return []
        raise TradingError('RECONCILE','계좌 응답 Occurs1 누락: 빈 계좌로 확정할 수 없습니다.')
    if isinstance(raw,dict):rows=[raw]
    elif isinstance(raw,list):rows=raw
    else:raise TradingError('RECONCILE','계좌 응답 Occurs1 형식 오류')
    if any(not isinstance(row,dict) or not row for row in rows):
        raise TradingError('RECONCILE','계좌 응답에 잘못된 행이 있어 전체 상태를 확정할 수 없습니다.')
    # nrec가 페이지 건수인지 전체 건수인지는 강제하지 않습니다.
    if (count==0 and rows) or (count is not None and count>0 and not rows):
        raise TradingError('RECONCILE','계좌 응답 nrec와 행 유무가 일치하지 않습니다.')
    return rows


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
        self.rate_intervals={
            'quote':max(MIN_INTERVAL['quote'],1/max(0.1,float(settings.get('quote_requests_per_second',5)))),
            'account':max(MIN_INTERVAL['account'],1/max(0.1,float(settings.get('account_requests_per_second',5)))),
            'order':max(MIN_INTERVAL['order'],1/max(0.1,float(settings.get('order_requests_per_second',4)))),
        }
        self._fingerprint=hashlib.sha256((self.base_url+':'+key).encode()).hexdigest()

    @property
    def credential_id(self)->str: return self._fingerprint[:16]

    def price_scale(self,symbol:str)->float:
        # NH's observed 6J quote is cents per JPY (0.63060); normalize to
        # USD per JPY (0.0063060), consistent with the configured CME tick.
        scales=self.settings.get('price_scale_by_prefix',{'6J':0.01})
        return next((float(scale) for prefix,scale in scales.items() if symbol.startswith(prefix)),1.0)

    def normalize_bar(self,symbol:str,bar:dict)->dict:
        result=dict(bar);scale=self.price_scale(symbol) if self.market=='overseas' else 1.0
        for field in ('open','high','low','close'):result[field]=float(result[field])*scale
        result.update(symbol=symbol,price_scale=scale)
        return result

    def _log(self,api:str,started:float,status:int|None,code:str='',message:str=''):
        if self.journal:
            self.journal.row('api_calls',ts=datetime.now(KST).isoformat(),market=self.market,api=api,
                http_status=status or '',latency_ms=round((time.perf_counter()-started)*1000,2),
                error_code=code,message=str(redact(message))[:250])

    def _throttle(self,grade:str):
        with _RATE_LOCK:
            key=(self._fingerprint,grade)
            wait=self.rate_intervals[grade]-(time.monotonic()-_LAST_CALL.get(key,0))
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
        """국내는 시간창 조회, 해외는 서버 nxt_key를 그대로 쓰는 역방향 연속조회.

        해외 서버가 최초 요청 날짜를 무시하고 최신 봉부터 반환하는 실제 관찰을
        반영합니다. 상한 밖 최신 봉은 저장하지 않아도 과거 조회는 계속합니다.
        구간 도달/키 종료/빈 페이지에서 중단하고 반복·무진행은 오류로 기록합니다.
        """
        lo=as_datetime(start)
        hi=as_datetime(end) if end is not None else lo.replace(hour=23,minute=59,second=59)
        if end is not None and ((isinstance(end,date) and not isinstance(end,datetime)) or len(str(end)) in (8,10)):
            hi=hi.replace(hour=23,minute=59,second=59)
        if hi<lo:
            raise TradingError('CONFIG','분봉 종료시각이 시작보다 빠릅니다.')
        if max_pages<1:
            raise TradingError('CONFIG','분봉 최대 조회 횟수는 1 이상이어야 합니다.')
        cidx=str(cidx_yn if cidx_yn is not None else self.settings.get('cidx_yn','0'))
        expiry_mode=str(fut_cidx_exp_mod if fut_cidx_exp_mod is not None else self.settings.get('fut_cidx_exp_mod','0'))
        result={}
        if self.market=='overseas':
            key=FIRST_KEY
            seen_keys=set()
            seen_pages=set()
            previous_oldest=None
            reason='max_pages'
            reached_start=False
            pages=0
            self.last_history_status={}
            for page in range(max_pages):
                pages=page+1
                if key in seen_keys:
                    raise TradingError('DATA','해외 분봉 연속조회 키가 반복됐습니다.',context={'symbol':sym,'pages':pages,'collected':len(result)})
                seen_keys.add(key)
                body=self.chart_minute(sym,lo,hi,exchange,interval,next_key=key,cidx_yn=cidx,fut_cidx_exp_mod=expiry_mode)
                raw_rows=rows_of(body)
                if not raw_rows:
                    reason='empty_page'
                    break
                bars=[b for b in (self._parse_bar(r) for r in raw_rows) if b is not None]
                if not bars:
                    raise TradingError('DATA','해외 분봉 페이지의 일시·가격을 해석할 수 없습니다.',context={'symbol':sym,'pages':pages,'raw_rows':len(raw_rows)})
                times=tuple(sorted(b['start'] for b in bars))
                signature=(times[0],times[-1],len(times))
                oldest,newest=as_datetime(times[0]),as_datetime(times[-1])
                if signature in seen_pages or (previous_oldest is not None and oldest>=previous_oldest):
                    raise TradingError('DATA','해외 분봉 연속조회가 더 과거로 진행되지 않습니다.',context={'symbol':sym,'pages':pages,'oldest':oldest.isoformat(),'collected':len(result)})
                seen_pages.add(signature)
                previous_oldest=oldest
                if page==0 and (newest>hi or oldest<lo):
                    if self.journal:
                        self.journal.event('history_requested_range_ignored',symbol=sym,market=self.market,
                            requested_start=lo.isoformat(),requested_end=hi.isoformat(),
                            returned_start=oldest.isoformat(),returned_end=newest.isoformat(),
                            action='follow_server_nxt_key_and_filter_locally')
                for b in bars:
                    if lo<=as_datetime(b['start'])<=hi:
                        result[b['start']]=b
                if oldest<=lo:
                    reason='requested_start_reached'
                    reached_start=True
                    break
                next_key=str(body.get('nxt_key') or '').strip()
                if not next_key or set(next_key)=={'9'}:
                    reason='server_end_key'
                    break
                if next_key in seen_keys:
                    raise TradingError('DATA','해외 분봉 응답의 다음 키가 이미 조회한 키입니다.',context={'symbol':sym,'pages':pages,'collected':len(result)})
                key=next_key
            self.last_history_status=dict(symbol=sym,pages=pages,stop_reason=reason,
                reached_requested_start=reached_start,collected=len(result),
                requested_start=lo.isoformat(),requested_end=hi.isoformat(),
                oldest_received=previous_oldest.isoformat() if previous_oldest else None)
            if self.journal:
                self.journal.event('history_paging_finished',**self.last_history_status)
            if reason=='max_pages':
                raise TradingError('DATA',f'해외 분봉 연속조회 상한 {max_pages}회에 도달했습니다.',context=self.last_history_status)
            return [self.normalize_bar(sym,result[k]) for k in sorted(result)]
        current=lo
        pages=0
        while current<=hi:
            pages+=1
            if pages>max_pages:
                raise TradingError('DATA',f'국내 분봉 시간창 상한 {max_pages}회 도달')
            stop=min(hi,current+timedelta(minutes=240)-timedelta(seconds=1))
            body=self.chart_minute(sym,current,stop,exchange,interval,cidx_yn=cidx,fut_cidx_exp_mod=expiry_mode)
            for row in rows_of(body):
                b=self._parse_bar(row)
                if b and current<=as_datetime(b['start'])<=stop:
                    result[b['start']]=b
            current=stop+timedelta(seconds=1)
        self.last_history_status=dict(symbol=sym,pages=pages,stop_reason='time_windows_finished',collected=len(result),
            requested_start=lo.isoformat(),requested_end=hi.isoformat())
        return [self.normalize_bar(sym,result[k]) for k in sorted(result)]

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
        self._domestic_only();result=[];key='';requested=set();pages=[]
        for _ in range(max_pages):
            if key in requested:
                raise TradingError('RECONCILE','계좌 연속조회 커서 반복: 부분 상태로 계좌를 확정하지 않습니다.')
            requested.add(key)
            body=self.call(api,{**(payload or {}),'nxt_key':key})
            page=_account_result_rows(body)
            nxt=str(body.get('nxt_key') or '').strip()
            terminal=not nxt or set(nxt)=={'9'}
            if page and any(page==prior for prior in pages):
                raise TradingError('RECONCILE','계좌 연속조회 페이지 반복: 잔고 중복 합산 방지')
            if not terminal and (nxt in requested or not page):
                raise TradingError('RECONCILE','계좌 연속조회가 진행되지 않아 전체 상태를 확정할 수 없습니다.')
            result.extend(page);pages.append(page)
            if terminal:return result
            key=nxt
        raise TradingError('RECONCILE','계좌 연속조회 상한 도달: 완전한 상태 확인 실패')

    def order_rows(self)->list[dict]:
        rows=self._account_rows('domestic_history',dict(exec_div='0',trd_div='0',trd_tm_div_cd='1',ord_div_cd='0'))
        result=[]
        for raw in rows:
            row=dict(raw)
            for field in ('occurs1_ord_no','occurs1_sym','occurs1_trd_div'):
                row[field]=str(raw.get(field) or '').strip()
            if not row['occurs1_ord_no'] or not row['occurs1_sym'] or row['occurs1_trd_div'] not in ('1','2'):
                raise TradingError('RECONCILE','주문내역의 주문번호·종목·매수매도 구분을 확인할 수 없습니다.')
            ordered=_account_integer(raw.get('occurs1_ord_qty'),'occurs1_ord_qty')
            if ordered==0:raise TradingError('RECONCILE','주문내역의 주문수량은 양수여야 합니다.')
            quantities={}
            for field in ('occurs1_exec_qty','occurs1_cxl_qty'):
                if field not in raw:
                    raise TradingError('RECONCILE',f'주문내역 {field} 누락: 최종수량을 확인할 수 없습니다.')
                value=raw[field]
                # 명시적 빈 문자열은 미체결/취소 없음으로 허용하되 누락·None은 거부합니다.
                quantities[field]=_account_integer('0' if isinstance(value,str) and not value.strip() else value,field)
            if quantities['occurs1_exec_qty']+quantities['occurs1_cxl_qty']>ordered:
                raise TradingError('RECONCILE','주문·체결·취소수량이 모순되어 잔량을 확정할 수 없습니다.')
            row['occurs1_ord_qty']=ordered;row.update(quantities);result.append(row)
        return result

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
            q=_account_integer(r.get('occurs1_opint_qty'),'occurs1_opint_qty')
            if not q:continue
            symbol=str(r.get('occurs1_sym') or '').strip()
            side=str(r.get('occurs1_trd_div') or '').strip()
            if not symbol or side not in ('1','2'):
                raise TradingError('RECONCILE','보유 포지션의 종목 또는 매수·매도 구분을 확인할 수 없습니다.')
            result.append(dict(symbol=symbol,qty=q if side=='1' else -q,
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
