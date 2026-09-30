import pandas as pd
import engine

def _df(vals):
    s=pd.Series(vals,dtype=float); return pd.DataFrame({'timestamp':range(len(s)),'open':s,'high':s+0.1,'low':s-0.1,'close':s,'volume':[200.0]*len(s),'turnover':[200.0]*len(s)})

def test_thesis_strong_hold(monkeypatch):
    up=list(range(100,220)); frames={('XUSDT','5',80):_df(up[-80:]),('XUSDT','15',80):_df(up[-80:]),('XUSDT','60',80):_df(up[-80:])}
    monkeypatch.setattr(engine,'klines',lambda symbol,interval,limit=80: frames[(symbol,str(interval),limit)].copy()); monkeypatch.setattr(engine,'news_snapshot',lambda symbol=None:{'status':'ok','impact':'low','top':[]})
    from thesis_engine import analyze_position
    r=analyze_position({'symbol':'XUSDT','side':'Buy','avgPrice':'200','markPrice':'201','positionIM':'100','unrealisedPnl':'3'})
    assert r['thesis_health']>=80 and r['thesis_decision']=='STRONG_HOLD' and r['positive_factors']

def test_thesis_detects_reversal(monkeypatch):
    vals=list(range(100,160))+list(range(160,100,-1)); frames={('XUSDT','5',80):_df(vals[-80:]),('XUSDT','15',80):_df(vals[-80:]),('XUSDT','60',80):_df(vals[-80:])}
    monkeypatch.setattr(engine,'klines',lambda symbol,interval,limit=80: frames[(symbol,str(interval),limit)].copy()); monkeypatch.setattr(engine,'news_snapshot',lambda symbol=None:{'status':'ok','impact':'low','top':[]})
    from thesis_engine import analyze_position
    r=analyze_position({'symbol':'XUSDT','side':'Buy','avgPrice':'150','markPrice':'149','positionIM':'100','unrealisedPnl':'-1'})
    assert r['thesis_health']<62 and r['negative_factors']
