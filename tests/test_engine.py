import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pandas as pd
import numpy as np
from fastapi.testclient import TestClient
import app, engine

def candles(n=220):
    close=np.linspace(100,120,n)+np.sin(np.arange(n))*0.5
    return pd.DataFrame({'timestamp':np.arange(n),'open':close-0.2,'high':close+0.5,'low':close-0.5,'close':close,'volume':np.full(n,1000.0),'turnover':close*1000})

def test_features_no_nan_tail():
    x=engine.frame_features(candles()); assert np.isfinite(x[['ema20','ema50','rsi','atr','vwap','atr_pct']].tail(1).to_numpy()).all()

def test_score_schema():
    f={k:candles() for k in ['5','15','60','240']}; s=engine.score(f,'15')
    assert s['direction'] in {'LONG','SHORT','WAIT'} and 0<=s['score']<=100 and s['price']>0

def test_paper_long_validation_and_fee():
    engine.paper_reset(); s=engine.paper_open({'symbol':'BTCUSDT','side':'LONG','entry':100,'stop_loss':99,'take_profit':102,'risk_pct':0.5})
    assert len(s['open'])==1 and s['balance'] < 10 and s['open'][0]['entry_fee'] > 0; engine.paper_reset()

def test_api_all_routes(monkeypatch):
    monkeypatch.setattr(app, 'market_snapshot', lambda n=20:[{'symbol':'BTCUSDT','lastPrice':100.0,'turnover24h':1000.0,'price24hPcnt':1.0,'highPrice24h':101.0,'lowPrice24h':99.0,'volume24h':10.0}])
    c=TestClient(app.app)
    assert c.get('/api/health').status_code==200
    assert c.get('/api/markets').json()['ok'] is True
    assert c.get('/api/paper').status_code==200
    assert c.post('/api/paper/reset').status_code==200

def test_paper_rejects_bad_geometry():
    engine.paper_reset()
    try: engine.paper_open({'symbol':'BTCUSDT','side':'LONG','entry':100,'stop_loss':101,'take_profit':102,'risk_pct':0.5}); assert False
    except ValueError: assert True

def test_flow_features_mock_and_oi_sign(monkeypatch):
    monkeypatch.setattr(engine, 'open_interest', lambda symbol, interval='15min', limit=10: [{'openInterest':'105'},{'openInterest':'100'}])
    monkeypatch.setattr(engine, 'orderbook', lambda symbol, limit=50: {'b':[['100','10']], 'a':[['101','5']]})
    monkeypatch.setattr(engine, 'recent_trades', lambda symbol, limit=500: [{'side':'Buy','size':'10'},{'side':'Sell','size':'5'}])
    monkeypatch.setattr(engine, 'funding_history', lambda symbol, limit=1: [{'fundingRate':'0.0001'}])
    x=engine.flow_features('BTCUSDT')
    assert x['oi_change_pct']>0 and x['orderbook_imbalance']>0 and x['trade_delta_pct']>0 and x['funding_rate']>0

def test_daily_risk_lock():
    engine.paper_reset(); engine._state['day_start_balance']=10.0; engine._state['balance']=7.9
    try: engine.paper_open({'symbol':'BTCUSDT','side':'LONG','entry':100,'stop_loss':99,'take_profit':102,'risk_pct':0.5}); assert False
    except ValueError as e: assert 'daily loss limit' in str(e)
    engine.paper_reset()

def test_budget_and_adaptive_margin(monkeypatch):
    monkeypatch.setattr(engine, 'instruments', lambda: [{'symbol':'BTCUSDT','lotSizeFilter':{'qtyStep':'0.001','minOrderQty':'0.001','maxOrderQty':'100'}}])
    engine.paper_reset(); engine.set_paper_budget(2.0)
    s=engine.paper_open({'symbol':'BTCUSDT','side':'LONG','entry':100,'stop_loss':99,'take_profit':102,'risk_pct':2.0})
    assert s['open'][0]['margin_required'] <= 2.0 * 0.95 + 1e-9
    assert s['available_margin_budget'] < 2.0
    engine.paper_reset()

def test_demo_open_rejects_bad_geometry(monkeypatch):
    monkeypatch.setattr(engine, 'MODE', 'demo')
    try:
        engine.demo_open({'symbol':'BTCUSDT','side':'LONG','entry':100,'stop_loss':101,'take_profit':102,'risk_pct':2})
        assert False
    except ValueError as e:
        assert 'LONG requires' in str(e)

def test_demo_state_survives_closed_pnl_failure(monkeypatch):
    monkeypatch.setattr(engine, 'MODE', 'demo')
    monkeypatch.setattr(engine, 'DEMO_API_KEY', 'k')
    monkeypatch.setattr(engine, 'DEMO_API_SECRET', 's')
    monkeypatch.setattr(engine, '_demo_wallet', lambda: {'list':[{'totalEquity':'1000','totalAvailableBalance':'900','totalPerpUPL':'0','coin':[{'coin':'USDT','walletBalance':'1000'}]}]})
    monkeypatch.setattr(engine, '_demo_positions', lambda: [])
    monkeypatch.setattr(engine, '_demo_closed_pnl', lambda limit=100: (_ for _ in ()).throw(RuntimeError('temporary closed pnl failure')))
    state = engine.demo_state()
    assert state['configured'] is True and state['degraded'] is True
    assert state['equity'] == 1000.0 and state['positions'] == []
    assert state['warnings']


def test_private_get_retries_transient_http(monkeypatch):
    class Resp:
        def __init__(self, status, payload): self.status_code=status; self._payload=payload
        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx
                req=httpx.Request('GET','https://example.test')
                raise httpx.HTTPStatusError('x', request=req, response=httpx.Response(self.status_code, request=req))
        def json(self): return self._payload
    calls=[]
    def fake_get(*a, **k):
        calls.append(1)
        return Resp(503,{}) if len(calls)==1 else Resp(200,{'retCode':0,'result':{'ok':1}})
    monkeypatch.setattr(engine._http, 'get', fake_get)
    monkeypatch.setattr(engine, 'MODE', 'demo'); monkeypatch.setattr(engine, 'DEMO_API_KEY', 'k'); monkeypatch.setattr(engine, 'DEMO_API_SECRET', 's')
    out=engine.bybit_private_get('/v5/account/info', {}, retries=1)
    assert out == {'ok':1} and len(calls)==2


def test_demo_state_uses_last_good_state_on_wallet_failure(monkeypatch):
    monkeypatch.setattr(engine, 'MODE', 'demo')
    monkeypatch.setattr(engine, 'DEMO_API_KEY', 'k')
    monkeypatch.setattr(engine, 'DEMO_API_SECRET', 's')
    good_wallet = {'list':[{'totalEquity':'1000','totalAvailableBalance':'900','totalPerpUPL':'0','coin':[{'coin':'USDT','walletBalance':'1000'}]}]}
    monkeypatch.setattr(engine, '_demo_wallet', lambda: good_wallet)
    monkeypatch.setattr(engine, '_demo_positions', lambda: [])
    monkeypatch.setattr(engine, '_demo_closed_pnl', lambda limit=100: [])
    first = engine.demo_state()
    monkeypatch.setattr(engine, '_demo_wallet', lambda: (_ for _ in ()).throw(RuntimeError('wallet timeout')))
    second = engine.demo_state()
    assert second['stale'] is True and second['equity'] == first['equity'] and second['usdt_wallet_balance'] == first['usdt_wallet_balance']
