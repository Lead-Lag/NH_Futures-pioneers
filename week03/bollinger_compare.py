"""H3: 2주차 볼린저 평균회귀 규칙을 A50·MHI 에 그대로 옮겨, 현물 개장 직후(10:30~11:30·14:00~15:00) vs 그 밖의 시간 성과 비교.
2주차 README 3-3절 규칙: 20봉·2.0σ, 밴드 밖→안 복귀 봉 마감에 진입, 중심선 도달(봉 마감) 청산, 손절, 30봉 시간 청산, 손절 뒤 3봉 대기.
상품에 맞춘 값(2주차와 같은 비율): 손절 = 1분봉 고가−저가 중앙값 × 1.7 (2주차 5틱 ÷ 3틱), 밴드폭 ≥ 8틱(왕복 비용 2틱 × 4).
체결: 신호·청산은 다음 봉 시가 ± 미끄러짐 1틱, 손절은 봉 고가·저가로 판정(backtest_orb.py 와 같은 가정).
    python bollinger_compare.py   (all_SFC.csv · all_MHI.csv: 백테스트·실행 분봉 합본)
"""
import math, numpy as np, pandas as pd
from backtest_orb import load, liquid_days
SES={"SFC":[("100000","122500"),("130000","155500")],"MHI":[("101500","125500"),("140000","165500")]}
OPEN=[("103000","113000"),("140000","150000")]
def run(prd, slip=1):
    d=load(f"all_{prd}.csv"); days=liquid_days(d); rows=[]
    hl=(d.high-d.low); stop_t=max(2,math.ceil(1.7*hl[hl>0].median()))
    for day in days:
        x=d[d.day==day].reset_index(drop=True)
        for s0,s1 in SES[prd]:
            w=x[(x.t>=s0)&(x.t<s1)].reset_index(drop=True)
            if len(w)<40: continue
            mid=w.close.rolling(20).mean(); sd=w.close.rolling(20).std(ddof=0); up=mid+2*sd; lo=mid-2*sd
            pos=0; cool=-1; i=21
            while i < len(w)-1:
                if pos==0:
                    if i<=cool or (up[i]-lo[i])<8: i+=1; continue
                    side=1 if (w.close[i-1]<lo[i-1] and w.close[i]>=lo[i]) else (-1 if (w.close[i-1]>up[i-1] and w.close[i]<=up[i]) else 0)
                    if side==0: i+=1; continue
                    e=w.open[i+1]+side*slip; st=e-side*stop_t; ent_t=w.t[i]; j=i+1; pos=side
                    while j < len(w):
                        b=w.iloc[j]
                        if (side==1 and b.low<=st) or (side==-1 and b.high>=st):
                            gap=(side==1 and b.open<st) or (side==-1 and b.open>st)
                            ex=(b.open if gap else st)-side*slip; rs="stop"; break
                        if (side==1 and b.close>=mid[j]) or (side==-1 and b.close<=mid[j]) or j-(i+1)>=30 or j==len(w)-1:
                            k=min(j+1,len(w)-1); ex=w.open[k]-side*slip; rs="mid" if j-(i+1)<30 else "time"; j=k; break
                        j+=1
                    tk=round(side*(ex-e)); op=any(a<=ent_t<b for a,b in OPEN)
                    rows.append(dict(day=day,t=ent_t,side=side,ticks=tk,R=tk/stop_t,reason=rs,open=op))
                    if rs=="stop": cool=j+3
                    pos=0; i=j+1
                else: i+=1
    return pd.DataFrame(rows), stop_t
if __name__=="__main__":
    for prd in ("SFC","MHI"):
        t,st=run(prd); t["grp"]=np.where(t.open,"개장 직후","그 밖의 시간")
        g=t.groupby("grp").agg(n=("ticks","size"),win=("ticks",lambda v:(v>0).mean()),avg_ticks=("ticks","mean"),avg_R=("R","mean"),sum_ticks=("ticks","sum"))
        print(prd,"손절",st,"틱"); print(g.round(2).to_string()); print()
        t.to_csv(f"bollinger_{prd}_trades.csv",index=False)
