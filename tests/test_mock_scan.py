import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import engine, pandas as pd, numpy as np

def frame():
    n=220;c=np.linspace(100,120,n);return pd.DataFrame({'timestamp':range(n),'open':c-0.2,'high':c+0.5,'low':c-0.5,'close':c,'volume':np.ones(n)*1000,'turnover':c*1000})

def test_scan_market_with_mock(monkeypatch):
    ticks=[{'symbol':'BTCUSDT','turnover24h':'1000'},{'symbol':'ETHUSDT','turnover24h':'500'}]
    monkeypatch.setattr(engine,'tickers',lambda:ticks); monkeypatch.setattr(engine,'klines',lambda s,i,limit=220:frame())
    out=engine.scan_market('15',2)
    assert out['ok'] and out['checked']==2 and len(out['results'])==2
