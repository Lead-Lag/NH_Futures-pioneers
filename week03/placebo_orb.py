"""대조 실험: 같은 돌파 규칙을 30분마다 다른 시각에 걸어, 현물 개장(10:30·14:00) 때와 비교.
돌파 뒤 15·30·60분 움직임(돌파 방향 +, 1분봉 ATR 배수)과 30분 안 범위 복귀 비율.
    python placebo_orb.py   (bars_SFC_*.csv · bars_MHI_*.csv 가 같은 폴더에 있어야 함)
"""
import pandas as pd, numpy as np, glob
from backtest_orb import load, liquid_days, add_time
H = (15, 30, 60)
def analyze(prd, starts):
    d = load(glob.glob(f"bars_{prd}_*.csv")[0]); days = liquid_days(d)
    rows = []
    for day in days:
        x = d[(d.day == day) & (d.t >= "100000")].reset_index(drop=True)
        prev = x.close.shift(1)
        tr = pd.concat([x.high-x.low,(x.high-prev).abs(),(x.low-prev).abs()],axis=1).max(axis=1)
        atr = tr.rolling(14, min_periods=14).mean()
        close_at = dict(zip(x.t, x.close))
        for T in starts:
            rend, last = add_time(T,15), add_time(T,59)
            rng = x[(x.t>=T)&(x.t<rend)]
            if len(rng) < 14: continue
            hi, lo = rng.high.max(), rng.low.min()
            if hi - lo < 8: continue
            for i in x.index[(x.t>=rend)&(x.t<=last)]:
                c = x.close[i]; s = 1 if c>hi else (-1 if c<lo else 0)
                if s == 0 or i+1 >= len(x) or pd.isna(atr[i]): continue
                e, a, te = x.open[i+1], atr[i], x.t[i+1]
                rec = dict(day=day, T=T, side=s)
                for h in H:   # 진입 h분 뒤 봉 종가 (그 시각 봉이 없으면 제외 — 점심 휴장 등)
                    tc = close_at.get(add_time(te, h - 1))
                    rec[f"r{h}"] = s*(tc-e)/a if tc is not None else np.nan
                win = x[(x.t>=te)&(x.t<add_time(te,30))]
                rec["back_in30"] = bool(((win.close<=hi)&(win.close>=lo)).any())
                rows.append(rec); break
    return pd.DataFrame(rows)
def agg(g):
    out = {"n": len(g)}
    for h in H:
        v = g[f"r{h}"].dropna()
        out[f"r{h}_mean"] = v.mean(); out[f"r{h}_med"] = v.median()
        out[f"r{h}_t"] = v.mean()/(v.std(ddof=1)/np.sqrt(len(v))) if len(v)>2 and v.std()>0 else np.nan
    out["back30"] = g.back_in30.mean()
    return pd.Series(out)
for prd in ("SFC","MHI"):
    starts = [f"{h:02d}{m:02d}00" for h in range(10,17) for m in (0,30)]
    if prd=="SFC": starts=[s for s in starts if s<="163000"]
    else: starts=[s for s in starts if s>="103000" and not ("120000"<=s<"140000") and s<="160000"]
    r = analyze(prd, starts); r["group"] = np.where(r["T"].isin(["103000","140000"]), "현물 개장", "그 밖의 시각")
    print(prd); print(r.groupby("T").apply(agg)[["n","r30_mean","r30_med","back30"]].round(2).to_string())
    print(r.groupby("group").apply(agg).round(2).to_string()); print()
    r.to_csv(f"placebo_{prd}.csv", index=False)
