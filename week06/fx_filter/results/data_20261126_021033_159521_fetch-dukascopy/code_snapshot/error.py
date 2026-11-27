"""공통 오류 분류·행동지침과 비밀값 가림."""
from __future__ import annotations
import re
from typing import Any
ERROR_POLICIES = {
 'CONFIG': ('STOP',False,'설정·사용자 환경변수 확인 후 재시작'),
 'DATA': ('SKIP_ENTRY',False,'자료 복구 전 신규 진입 보류'),
 'AUTH': ('STOP_ENTRY',False,'서버·헤더·실제 인증 메시지 확인; 무조건 재발급 금지'),
 'RATE_LIMIT': ('BACKOFF',True,'조회 간격 증가; 주문 재전송 금지'),
 'TRANSPORT': ('BACKOFF',True,'조회만 제한적으로 재시도; 신규 진입 보류'),
 'UNKNOWN_ORDER': ('RECONCILE',False,'주문 재전송 금지; 주문내역·미체결·계좌 포지션 대조'),
 'ORDER_REJECTED': ('STOP_ENTRY',False,'거부 사유와 잔고 확인'),
 'RECONCILE': ('STOP_ENTRY',False,'최종 체결·잔량·잔고 확인 전 진입 중지'),
 'WS_DISCONNECTED': ('STOP_ENTRY',True,'재접속·누락봉 복구; 오래된 신호 주문 금지'),
 'UNEXPECTED': ('STOP',False,'원인 기록 후 운용 중단'),
}
_SECRET_KEY=re.compile(r'app.?key|app.?secret|access.?token|session.?token|authorization|access.?key|secret|password|ac_no|account|user_ip',re.I)
_SECRET_TEXT=re.compile(r'(Bearer\s+)[^\s\"\']+|((?:access_key|access_token|appsecret|appkey)=)[^&\s]+',re.I)
def redact(value:Any)->Any:
 if isinstance(value,dict):return {str(k):'[REDACTED]' if _SECRET_KEY.search(str(k)) else redact(v) for k,v in value.items()}
 if isinstance(value,(list,tuple)):return [redact(v) for v in value]
 if isinstance(value,str):return _SECRET_TEXT.sub(lambda m:(m.group(1) or m.group(2) or '')+'[REDACTED]',value)
 return value
class TradingError(Exception):
 def __init__(self,code:str,message:str,action:str|None=None,retryable:bool|None=None,context:dict|None=None):
  p=ERROR_POLICIES.get(code,ERROR_POLICIES['UNEXPECTED'])
  self.code,self.message=code,str(redact(message));self.action=action or p[0]
  self.retryable=p[1] if retryable is None else retryable;self.context=redact(context or {});self.guidance=p[2]
  super().__init__(f'[{self.code}] {self.message} — {self.guidance}')
 def to_dict(self)->dict:return dict(code=self.code,message=self.message,action=self.action,retryable=self.retryable,guidance=self.guidance,context=self.context)
def classify_error(exc:Exception,operation:str='',order:bool=False)->TradingError:
 if isinstance(exc,TradingError):return exc
 code='UNKNOWN_ORDER' if order else 'DATA' if isinstance(exc,(ValueError,KeyError,FileNotFoundError)) else 'TRANSPORT' if isinstance(exc,(TimeoutError,ConnectionError)) else 'UNEXPECTED'
 return TradingError(code,f'{operation}: {type(exc).__name__}: {exc}')
