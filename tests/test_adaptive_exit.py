import pandas as pd

import engine


def _df(closes):
    n=len(closes)
    s=pd.Series([float(x) for x in closes])
    # Stable synthetic OHLCV; the bias/EMA calculations are what matter here.
    return pd.DataFrame({
        'timestamp': range(n),
        'open': s,
        'high': s + 0.01,
        'low': s - 0.01,
        'close': s,
        'volume': [100.0]*n,
        'turnover': [100.0]*n,
    })


def test_adaptive_exit_can_close_before_configured_risk(monkeypatch):
    # Long position: 5m/15m/1h all reverse down. The loss is intentionally
    # much smaller than a hypothetical configured risk limit.
    up = list(range(100, 160))
    down = list(range(160, 100, -1))
    frames = {
        ('COINUSDT','5',80): _df(up[-30:] + down[-50:]),
        ('COINUSDT','15',80): _df(up[-30:] + down[-50:]),
        ('COINUSDT','60',80): _df(up[-30:] + down[-50:]),
    }
    monkeypatch.setattr(engine, 'klines', lambda symbol, interval, limit=80: frames[(symbol, str(interval), limit)].copy())
    monkeypatch.setattr(engine, 'news_snapshot', lambda symbol=None: {'status':'ok','impact':'low','top':[]})
    p={'symbol':'COINUSDT','side':'Buy','avgPrice':'110','markPrice':'109.8','positionIM':'100','unrealisedPnl':'-0.20','size':'1'}
    r=engine._position_exit_analysis(p)
    assert r['pnl_on_margin_pct'] == -0.2
    assert r['decision'] == 'EXIT'
    assert r['confirmations'] >= 2


def test_adaptive_exit_holds_when_trend_is_aligned(monkeypatch):
    up = list(range(100, 220))
    frames = {('COINUSDT',str(i),80): _df(up[-80:]) for i in (5,15,60)}
    monkeypatch.setattr(engine, 'klines', lambda symbol, interval, limit=80: frames[(symbol, str(interval), limit)].copy())
    monkeypatch.setattr(engine, 'news_snapshot', lambda symbol=None: {'status':'ok','impact':'low','top':[]})
    p={'symbol':'COINUSDT','side':'Buy','avgPrice':'200','markPrice':'201','positionIM':'100','unrealisedPnl':'1','size':'1'}
    r=engine._position_exit_analysis(p)
    assert r['decision'] in ('HOLD / TRAIL','REASSESS')
    assert r['decision'] != 'EXIT'
