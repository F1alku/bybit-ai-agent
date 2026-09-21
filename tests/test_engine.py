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

def test_private_post_allows_unchanged_leverage_code(monkeypatch):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {'retCode': 110043, 'retMsg': 'Set leverage has not been modified', 'result': {}}
    monkeypatch.setattr(engine._http, 'post', lambda *a, **k: Resp())
    monkeypatch.setattr(engine, 'MODE', 'demo')
    monkeypatch.setattr(engine, 'DEMO_API_KEY', 'k')
    monkeypatch.setattr(engine, 'DEMO_API_SECRET', 's')
    out = engine.bybit_private_post('/v5/position/set-leverage', {'category':'linear','symbol':'BTCUSDT','buyLeverage':'10','sellLeverage':'10'}, allow_ret_codes={110043})
    assert out == {}


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


def test_json_safe_handles_nonfinite_numpy(monkeypatch):
    import numpy as np
    x=engine._json_safe({'a': np.float64(1.25), 'b': float('nan'), 'c': [np.float64(2.0)]})
    assert x == {'a':1.25,'b':None,'c':[2.0]}

def test_scan_error_is_json_contract(monkeypatch):
    monkeypatch.setattr(app, 'scan_market', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    c=TestClient(app.app)
    r=c.post('/api/scan', json={'interval':'15','limit_symbols':24})
    assert r.status_code==200 and r.json()['status']=='running'
    import time
    for _ in range(30):
        rr=c.get('/api/scan/'+r.json()['job_id'])
        if rr.json().get('status')=='error':
            assert rr.status_code==200 and rr.json()['error']=='boom'
            return
        time.sleep(0.02)
    assert False, 'scan job did not finish'


def test_score_entry_threshold_and_blockers():
    f={k:candles() for k in ['5','15','60','240','D']}
    x=engine.score(f,'15',entry_threshold=60,strategy='normal')
    assert x['entry_threshold']==60 and 'blockers' in x and 'market_regime' in x
    assert x['decision'] in {'WAIT','OPEN LONG','OPEN SHORT'}


def test_strategy_gate_persists(monkeypatch, tmp_path):
    import journal
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(tmp_path/'j.db'))
    journal.set_setting('scalp_gate', 55)
    assert journal.get_setting('scalp_gate') == '55'

def test_strategy_config_scales_gate_without_unbound_local(monkeypatch, tmp_path):
    import journal, trader
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(tmp_path/'j.db'))
    journal.set_setting('scalp_gate', 55)
    journal.set_setting('normal_gate', 70)
    import app
    monkeypatch.setitem(app.strategy_state, 'mode', 'scalp')
    mode, interval, gate = trader._strategy_config()
    assert (mode, interval, gate) == ('scalp', '5', 55)


def test_profit_lock_keeps_base_trading_capital_and_never_unlocks(monkeypatch, tmp_path):
    import journal
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(tmp_path/'capital.db'))
    monkeypatch.setattr(engine, 'MODE', 'demo')
    monkeypatch.setattr(engine, 'BOT_BASE_CAPITAL', 10.0)
    monkeypatch.setattr(engine, 'PROFIT_LOCK_STEP', 5.0)
    realized = {'value': 0.0}
    monkeypatch.setattr(journal, 'realized_total', lambda mode=None: realized['value'])
    first = engine._bot_capital_view()
    assert first['trading_capital'] == 10.0 and first['locked_profit'] == 0.0
    realized['value'] = 5.0
    locked = engine._bot_capital_view()
    assert locked['trading_capital'] == 10.0 and locked['locked_profit'] == 5.0
    realized['value'] = 2.0
    drawdown = engine._bot_capital_view()
    assert drawdown['trading_capital'] == 7.0 and drawdown['locked_profit'] == 5.0
    realized['value'] = 10.0
    second_lock = engine._bot_capital_view()
    assert second_lock['trading_capital'] == 10.0 and second_lock['locked_profit'] == 10.0


def test_profit_lock_does_not_use_locked_profit_for_position_risk(monkeypatch, tmp_path):
    import journal
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(tmp_path/'capital-risk.db'))
    monkeypatch.setattr(engine, 'MODE', 'demo')
    monkeypatch.setattr(engine, 'BOT_BASE_CAPITAL', 10.0)
    monkeypatch.setattr(engine, 'PROFIT_LOCK_STEP', 5.0)
    realized = {'value': 0.0}
    monkeypatch.setattr(journal, 'realized_total', lambda mode=None: realized['value'])
    # Establish the baseline before the profit is generated.
    engine._bot_capital_view()
    realized['value'] = 5.0
    monkeypatch.setattr(engine, '_demo_available_usdt', lambda: (1000.0, 1000.0, {}))
    monkeypatch.setattr(engine, '_demo_positions', lambda: [])
    monkeypatch.setattr(engine, '_symbol_rules', lambda symbol: (0.001, 0.001, 100.0))
    qty, margin, available, equity, cap = engine._demo_risk_qty('BTCUSDT', 100.0, 99.0)
    assert cap['trading_capital'] == 10.0 and cap['locked_profit'] == 5.0
    assert margin <= 10.0 * 0.95 + 1e-9


def test_news_filter_ignores_unconfirmed_headline(monkeypatch):
    import news_engine
    signal={'symbol':'SUIUSDT','decision':'OPEN LONG','direction':'LONG','blockers':[],'reasons':[]}
    news={'btc_reaction':{'strength':'none','pct_15m':0,'pct_30m':0}}
    news_engine._cache['items']=[{'id':'1','source':'CoinDesk','title':'SEC approves crypto ETF','summary':'','url':'','published_at':__import__('time').time()-5*60,'text':'SEC approves crypto ETF','kind':'crypto'}]
    out=news_engine.apply_to_signal(signal,news,'scalp')
    assert out['decision']=='OPEN LONG' and out['news_impact'] in ('medium','high')


def test_news_filter_blocks_confirmed_high_impact_scalp(monkeypatch):
    import news_engine, time
    signal={'symbol':'SUIUSDT','decision':'OPEN LONG','direction':'LONG','blockers':[],'reasons':[]}
    news={'btc_reaction':{'strength':'strong','pct_15m':0.8,'pct_30m':1.2}}
    news_engine._cache['items']=[{'id':'2','source':'Federal Reserve','title':'Federal Reserve cuts rates','summary':'','url':'','published_at':time.time()-5*60,'text':'Federal Reserve cuts rates','kind':'macro'}]
    out=news_engine.apply_to_signal(signal,news,'scalp')
    assert out['decision']=='WAIT' and any('NEWS:' in x for x in out['blockers'])


def test_news_filter_does_not_block_old_confirmed_event():
    import news_engine, time
    signal={'symbol':'SUIUSDT','decision':'OPEN LONG','direction':'LONG','blockers':[],'reasons':[]}
    news={'btc_reaction':{'strength':'strong','pct_15m':0.8,'pct_30m':1.2}}
    news_engine._cache['items']=[{'id':'3','source':'Federal Reserve','title':'Federal Reserve cuts rates','summary':'','url':'','published_at':time.time()-120*60,'text':'Federal Reserve cuts rates','kind':'macro'}]
    out=news_engine.apply_to_signal(signal,news,'normal')
    assert out['decision']=='OPEN LONG'


def test_closed_pnl_fees_are_net_realized(monkeypatch, tmp_path):
    import journal
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(tmp_path/'fees.db'))
    journal.sync_closed_pnl('demo', [{'orderId':'o1','symbol':'BTCUSDT','side':'Buy','qty':'1','avgEntryPrice':'100','avgExitPrice':'105','closedPnl':'5','openFee':'0.10','closeFee':'0.20','createdTime':'1','updatedTime':'2'}])
    assert abs(journal.realized_total('demo') - 4.7) < 1e-9


def test_capital_sync_imports_exchange_closed_pnl_automatically(monkeypatch, tmp_path):
    import journal
    monkeypatch.setattr(journal, 'SQLITE_PATH', str(tmp_path/'auto-sync.db'))
    monkeypatch.setattr(engine, 'MODE', 'demo')
    monkeypatch.setattr(engine, 'BOT_BASE_CAPITAL', 10.0)
    monkeypatch.setattr(engine, 'PROFIT_LOCK_STEP', 5.0)
    monkeypatch.setattr(engine, '_demo_closed_pnl', lambda limit=100: [{'orderId':'auto1','symbol':'BTCUSDT','closedPnl':'5','openFee':'0','closeFee':'0','createdTime':'1','updatedTime':'2'}])
    first = engine._bot_capital_view()
    assert first['locked_profit'] == 5.0 and first['trading_capital'] == 10.0


def test_news_confirmation_requires_direction_match(monkeypatch):
    import news_engine, time
    signal={'symbol':'SUIUSDT','decision':'OPEN LONG','direction':'LONG','blockers':[],'reasons':[]}
    news={'btc_reaction':{'strength':'strong','pct_15m':-0.8,'pct_30m':-1.2,'direction':'bearish'}}
    news_engine._cache['items']=[{'id':'dir1','source':'Federal Reserve','title':'Federal Reserve cuts rates','summary':'','url':'','published_at':time.time()-5*60,'text':'Federal Reserve cuts rates','kind':'macro'}]
    out=news_engine.apply_to_signal(signal,news,'scalp')
    assert out['decision']=='OPEN LONG' and out['news_market_confirmed'] is False


def test_news_asset_matching_does_not_match_substring(monkeypatch):
    import news_engine
    item={'kind':'crypto','text':'This is a business update with no relevant asset ticker'}
    rel, level, asset = news_engine._relevance(item, 'SUIUSDT')
    assert asset == 'market'


def test_news_source_status_degraded_when_all_feeds_fail(monkeypatch):
    import news_engine
    class Bad:
        def get(self,*a,**k): raise RuntimeError('offline')
    monkeypatch.setattr(news_engine, '_client', Bad())
    news_engine._cache.update({'ts':0,'items':[],'sources':{}})
    snap=news_engine.snapshot(force=True)
    assert snap['status']=='degraded'
    assert snap['sources'] and not any(v.get('ok') for v in snap['sources'].values())

def test_symbol_constraints_and_min_notional_risk_guard(monkeypatch):
    monkeypatch.setattr(engine, 'MODE', 'demo')
    monkeypatch.setattr(engine, '_demo_available_usdt', lambda: (1000.0, 1000.0, {}))
    monkeypatch.setattr(engine, '_demo_positions', lambda: [])
    monkeypatch.setattr(engine, '_bot_capital_view', lambda unrealized_pnl=0.0, reserved_margin=0.0: {'base_capital':10,'trading_capital':10,'locked_profit':0,'bot_equity':10,'bot_available_capital':10,'profit_lock_step':5})
    monkeypatch.setattr(engine, 'instruments', lambda: [{'symbol':'XYZUSDT','lotSizeFilter':{'qtyStep':'1','minOrderQty':'1','maxOrderQty':'100','minNotionalValue':'5'},'leverageFilter':{'minLeverage':'1','maxLeverage':'5','leverageStep':'0.5'},'priceFilter':{'tickSize':'0.01'}}])
    try:
        engine._demo_risk_qty('XYZUSDT', 1.0, 0.9, requested_leverage=10, risk_pct=2)
        assert False
    except ValueError as e:
        assert 'max is 5' in str(e)
    try:
        engine._demo_risk_qty('XYZUSDT', 1.0, 0.5, requested_leverage=5, risk_pct=2)
        assert False
    except ValueError as e:
        assert 'minimum Bybit order value' in str(e)

def test_full_market_scan_does_not_apply_24_cap(monkeypatch):
    monkeypatch.setattr(engine, '_fast_market_universe', lambda: ([{'symbol':f'X{i}USDT','turnover24h':1000,'lastPrice':100,'spreadPct':0.01} for i in range(30)], {}))
    # Fail before network/candle work; we only verify that full mode constructs all symbols.
    seen=[]
    def tech(symbol, interval, tm):
        seen.append(symbol)
        raise RuntimeError('stop')
    monkeypatch.setattr(engine, '_technical_one', tech)
    monkeypatch.setattr(engine, 'news_snapshot', lambda: {'status':'ok','impact':'low','items':[],'btc_reaction':{}})
    out=engine.scan_market('15', 24, entry_threshold=70, strategy='normal', full_market=True)
    assert len(set(seen)) == 30 and out['scan_policy']['full_market'] is True


def test_full_market_universe_keeps_thin_symbols(monkeypatch):
    monkeypatch.setattr(engine, 'instruments', lambda: [
        {'symbol':'BTCUSDT'}, {'symbol':'THINUSDT'}
    ])
    monkeypatch.setattr(engine, '_ticker_map', lambda: {
        'BTCUSDT': {'symbol':'BTCUSDT','turnover24h':'1000','lastPrice':'100','bid1Price':'99.9','ask1Price':'100.1','price24hPcnt':'0.01'},
        'THINUSDT': {'symbol':'THINUSDT','turnover24h':'0','lastPrice':'1','bid1Price':'0.98','ask1Price':'1.02','price24hPcnt':'-0.01'},
    })
    rows, _ = engine._fast_market_universe()
    assert {x['symbol'] for x in rows} == {'BTCUSDT','THINUSDT'}


def test_kline_cache_avoids_duplicate_http(monkeypatch):
    engine._cache.clear()
    calls=[]
    def fake_get(path, params):
        calls.append((path, tuple(sorted(params.items()))))
        return {'list': [[str(i), '100','101','99','100','1000','100000'] for i in range(100,0,-1)]}
    monkeypatch.setattr(engine, 'bybit_get', fake_get)
    a=engine.klines('BTCUSDT','15',100)
    b=engine.klines('BTCUSDT','15',100)
    assert len(calls)==1 and len(a)==len(b)


def test_portfolio_risk_uses_confirmed_stop(monkeypatch):
    monkeypatch.setattr(engine, '_demo_available_usdt', lambda: (10.0, 10.0, {}))
    monkeypatch.setattr(engine, '_demo_positions', lambda: [{'size':'0.1','avgPrice':'100','positionIM':'10','stopLoss':'99'}])
    monkeypatch.setattr(engine, '_bot_capital_view', lambda unrealized_pnl=0, reserved_margin=0: {'bot_available_capital':10.0})
    monkeypatch.setattr(engine, '_effective_leverage', lambda symbol, requested: (10.0, {'min_notional':0,'max_leverage':10}))
    monkeypatch.setattr(engine, '_symbol_rules', lambda symbol: (0.001,0.001,100.0))
    qty, margin, available, equity, cap, lev, constraints, actual = engine._demo_risk_qty('BTCUSDT',100,99,10,2,True)
    assert actual == 0.2
