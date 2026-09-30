import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import engine, pandas as pd, numpy as np

def frame():
    n=220;c=np.linspace(100,120,n);return pd.DataFrame({'timestamp':range(n),'open':c-0.2,'high':c+0.5,'low':c-0.5,'close':c,'volume':np.ones(n)*1000,'turnover':c*1000})

def test_scan_market_with_mock(monkeypatch):
    ticks=[{'symbol':'BTCUSDT','turnover24h':'1000','lastPrice':'100','bid1Price':'99.99','ask1Price':'100.01','price24hPcnt':'0.01'},{'symbol':'ETHUSDT','turnover24h':'500','lastPrice':'50','bid1Price':'49.99','ask1Price':'50.01','price24hPcnt':'0.01'}]
    monkeypatch.setattr(engine,'tickers',lambda:ticks); monkeypatch.setattr(engine,'instruments',lambda:[{'symbol':'BTCUSDT','contractType':'LinearPerpetual','quoteCoin':'USDT','settleCoin':'USDT'},{'symbol':'ETHUSDT','contractType':'LinearPerpetual','quoteCoin':'USDT','settleCoin':'USDT'}]); monkeypatch.setattr(engine,'klines',lambda s,i,limit=220:frame()); monkeypatch.setattr(engine,'flow_features',lambda s:{'oi_change_pct':0,'orderbook_imbalance':0,'trade_delta_pct':0,'funding_rate':0,'spread_pct':0.01})
    out=engine.scan_market('15',2)
    assert out['ok'] and out['checked']==2 and len(out['results'])==2


def test_scan_market_diagnostic_limit_is_real(monkeypatch):
    symbols = [f"C{i}USDT" for i in range(7)]
    ticks=[{'symbol':s,'turnover24h':'1000','lastPrice':str(100+i),'bid1Price':str(99.99+i),'ask1Price':str(100.01+i),'price24hPcnt':'0.01'} for i,s in enumerate(symbols)]
    instruments=[{'symbol':s,'contractType':'LinearPerpetual','quoteCoin':'USDT','settleCoin':'USDT'} for s in symbols]
    monkeypatch.setattr(engine,'tickers',lambda:ticks); monkeypatch.setattr(engine,'instruments',lambda:instruments); monkeypatch.setattr(engine,'klines',lambda s,i,limit=220:frame()); monkeypatch.setattr(engine,'flow_features',lambda s:{'oi_change_pct':0,'orderbook_imbalance':0,'trade_delta_pct':0,'funding_rate':0,'spread_pct':0.01})
    out=engine.scan_market('15',3,full_market=False)
    assert out['universe_size']==7
    assert out['technical_target']==3
    assert out['scan_policy']['full_market'] is False

