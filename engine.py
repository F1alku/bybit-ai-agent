import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict

import httpx
import pandas as pd

BASE = 'https://api-testnet.bybit.com'
TIMEOUT = 15.0
CACHE_TTL = 8.0
_lock = threading.RLock()
_cache = {}
_state = {'balance': 1000.0, 'initial_balance': 1000.0, 'trades': [], 'open': []}


def _cached(key):
    item = _cache.get(key)
    if item and time.time() - item[0] < CACHE_TTL:
        return item[1]
    return None


def _put_cache(key, value):
    _cache[key] = (time.time(), value)
    return value


def bybit_get(path, params):
    key = (path, tuple(sorted((str(k), str(v)) for k, v in params.items())))
    cached = _cached(key)
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=TIMEOUT, headers={'User-Agent': 'BybitAI-Agent/2.0'}) as c:
            r = c.get(BASE + path, params=params)
            r.raise_for_status()
            j = r.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f'Bybit HTTP {e.response.status_code}') from e
    except httpx.RequestError as e:
        raise RuntimeError(f'Bybit connection error: {e}') from e
    except ValueError as e:
        raise RuntimeError('Bybit returned invalid JSON') from e
    if j.get('retCode') != 0:
        raise RuntimeError(f"Bybit {j.get('retCode')}: {j.get('retMsg', 'API error')}")
    return _put_cache(key, j.get('result', {}))


def instruments():
    result = []
    cursor = None
    for _ in range(5):
        p = {'category': 'linear', 'status': 'Trading', 'limit': 1000}
        if cursor:
            p['cursor'] = cursor
        j = bybit_get('/v5/market/instruments-info', p)
        result += j.get('list', [])
        cursor = j.get('nextPageCursor')
        if not cursor:
            break
    return [x for x in result if x.get('contractType') == 'LinearPerpetual' and x.get('quoteCoin') == 'USDT']


def tickers():
    return bybit_get('/v5/market/tickers', {'category': 'linear'}).get('list', [])


def market_snapshot(limit=20):
    rows = []
    for x in tickers():
        symbol = x.get('symbol', '')
        if not symbol.endswith('USDT'):
            continue
        try:
            rows.append({
                'symbol': symbol,
                'lastPrice': float(x.get('lastPrice') or 0),
                'turnover24h': float(x.get('turnover24h') or 0),
                'price24hPcnt': float(x.get('price24hPcnt') or 0) * 100,
                'highPrice24h': float(x.get('highPrice24h') or 0),
                'lowPrice24h': float(x.get('lowPrice24h') or 0),
                'volume24h': float(x.get('volume24h') or 0),
            })
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x['turnover24h'], reverse=True)
    return rows[:max(1, min(int(limit), 50))]


def klines(symbol, interval, limit=220):
    rows = bybit_get('/v5/market/kline', {
        'category': 'linear', 'symbol': symbol, 'interval': str(interval), 'limit': min(int(limit), 1000)
    }).get('list', [])
    if len(rows) < 60:
        raise RuntimeError(f'Not enough candles for {symbol} {interval}m')
    rows = list(reversed(rows))
    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
    for c in ['open', 'high', 'low', 'close', 'volume', 'turnover']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
    return df.dropna().reset_index(drop=True)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    prev = df.close.shift(1)
    tr = pd.concat([(df.high - df.low), (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    rs = g / l.replace(0, float('nan'))
    return (100 - (100 / (1 + rs))).fillna(50)


def vwap(df):
    return (df.close * df.volume).cumsum() / df.volume.cumsum().replace(0, float('nan'))


def frame_features(df):
    x = df.copy()
    x['ema20'] = ema(x.close, 20)
    x['ema50'] = ema(x.close, 50)
    x['rsi'] = rsi(x.close)
    x['atr'] = atr(x)
    x['vwap'] = vwap(x)
    x['vol_ma20'] = x.volume.rolling(20).mean()
    return x


def bias(df):
    x = frame_features(df)
    z = x.iloc[-1]
    if z.ema20 > z.ema50 and z.close > z.ema20:
        return 'LONG'
    if z.ema20 < z.ema50 and z.close < z.ema20:
        return 'SHORT'
    return 'NEUTRAL'


def sweep(df, lookback=20):
    if len(df) < lookback + 2:
        return False, False
    last = df.iloc[-1]
    prior = df.iloc[-lookback-1:-1]
    bull = bool(last.low < prior.low.min() and last.close > prior.low.min())
    bear = bool(last.high > prior.high.max() and last.close < prior.high.max())
    return bull, bear


def score(frames, setup='15'):
    if setup not in frames:
        raise ValueError('unsupported setup timeframe')
    f = {k: frame_features(v) for k, v in frames.items()}
    b4 = bias(frames['240'])
    b1 = bias(frames['60'])
    setup_df = f[setup]
    z = setup_df.iloc[-1]
    hi = setup_df.high.tail(50).max()
    lo = setup_df.low.tail(50).min()
    mid = (hi + lo) / 2
    bull, bear = sweep(frames[setup])
    ls = ss = 0
    reasons_long, reasons_short = [], []

    if b4 == 'LONG': ls += 20; reasons_long.append('4H trend')
    elif b4 == 'SHORT': ss += 20; reasons_short.append('4H trend')
    if b1 == 'LONG': ls += 15; reasons_long.append('1H trend')
    elif b1 == 'SHORT': ss += 15; reasons_short.append('1H trend')
    if bull: ls += 20; reasons_long.append('liquidity sweep')
    if bear: ss += 20; reasons_short.append('liquidity sweep')
    if z.close < mid: ls += 10; reasons_long.append('discount')
    elif z.close > mid: ss += 10; reasons_short.append('premium')
    if z.close > z.vwap: ls += 5; reasons_long.append('above VWAP')
    elif z.close < z.vwap: ss += 5; reasons_short.append('below VWAP')
    if pd.notna(z.vol_ma20) and z.volume > z.vol_ma20:
        if z.close >= z.open: ls += 5; reasons_long.append('volume confirmation')
        else: ss += 5; reasons_short.append('volume confirmation')
    z5 = f['5'].iloc[-1]
    if z5.close > z5.ema20: ls += 5; reasons_long.append('5M momentum')
    elif z5.close < z5.ema20: ss += 5; reasons_short.append('5M momentum')

    direction = 'WAIT'
    best = max(ls, ss)
    if ls >= 70 and ls > ss:
        direction = 'LONG'
    elif ss >= 70 and ss > ls:
        direction = 'SHORT'

    atrv = float(z.atr) if pd.notna(z.atr) and z.atr > 0 else max(float(z.close) * 0.003, 1e-9)
    price = float(z.close)
    if direction == 'LONG':
        sl = min(float(z.low), float(frames[setup].low.tail(20).min())) - 0.15 * atrv
        tp = price + 2 * (price - sl)
        reasons = reasons_long
    elif direction == 'SHORT':
        sl = max(float(z.high), float(frames[setup].high.tail(20).max())) + 0.15 * atrv
        tp = price - 2 * (sl - price)
        reasons = reasons_short
    else:
        sl = tp = None
        reasons = reasons_long if ls >= ss else reasons_short

    rr = None
    if direction == 'LONG' and sl < price:
        rr = round((tp - price) / (price - sl), 2)
    elif direction == 'SHORT' and sl > price:
        rr = round((price - tp) / (sl - price), 2)

    return {
        'direction': direction, 'score': float(best), 'price': price,
        'stop_loss': sl, 'take_profit': tp, 'rr': rr,
        'htf_4h': b4, 'htf_1h': b1, 'setup_tf': setup,
        'bull_sweep': bull, 'bear_sweep': bear, 'reasons': reasons,
        'long_score': ls, 'short_score': ss,
    }


def _scan_one(symbol, setup):
    frames = {k: klines(symbol, k, 220) for k in ['5', '15', '60', '240']}
    return score(frames, setup)


def scan_market(interval='15', limit_symbols=8):
    if interval not in {'5', '15', '60'}:
        raise ValueError('interval must be 5, 15 or 60')
    limit_symbols = max(3, min(int(limit_symbols), 12))
    tv = tickers()
    turnover = {x['symbol']: float(x.get('turnover24h') or 0) for x in tv}
    # Public tickers are enough to select liquid linear USDT perpetual candidates.
    symbols = sorted([s for s in turnover if s.endswith('USDT')], key=lambda s: turnover[s], reverse=True)[:limit_symbols]
    results, failures = [], []
    with ThreadPoolExecutor(max_workers=min(4, len(symbols) or 1)) as ex:
        futures = {ex.submit(_scan_one, s, interval): s for s in symbols}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                a = fut.result()
                a.update({'symbol': s, 'turnover24h': turnover.get(s, 0)})
                results.append(a)
            except Exception as e:
                failures.append({'symbol': s, 'error': str(e)})
    results.sort(key=lambda x: x['score'], reverse=True)
    return {'ok': True, 'mode': 'paper-only', 'setup_interval': interval, 'checked': len(symbols), 'results': results, 'failures': failures}


def paper_state():
    with _lock:
        s = {k: (v.copy() if isinstance(v, list) else v) for k, v in _state.items()}
        s['pnl'] = round(s['balance'] - s['initial_balance'], 4)
        s['return_pct'] = round(s['pnl'] / s['initial_balance'] * 100, 3)
        return s


def paper_open(d):
    side = str(d['side']).upper(); symbol = str(d['symbol']).upper()
    entry = float(d['entry']); sl = float(d['stop_loss']); tp = float(d['take_profit']); risk_pct = float(d['risk_pct'])
    if side not in ('LONG', 'SHORT'): raise ValueError('side must be LONG or SHORT')
    if not symbol.endswith('USDT'): raise ValueError('paper symbol must be a USDT perpetual')
    if entry <= 0 or sl <= 0 or tp <= 0: raise ValueError('prices must be positive')
    if not 0 < risk_pct <= 5: raise ValueError('risk_pct must be between 0 and 5')
    if side == 'LONG' and not (sl < entry < tp): raise ValueError('LONG requires SL < entry < TP')
    if side == 'SHORT' and not (tp < entry < sl): raise ValueError('SHORT requires TP < entry < SL')
    with _lock:
        if len(_state['open']) >= 2: raise ValueError('max 2 open paper positions')
        if any(x['symbol'] == symbol for x in _state['open']): raise ValueError('position for this symbol already open')
        risk = _state['balance'] * risk_pct / 100
        dist = abs(entry - sl)
        if dist <= 0: raise ValueError('stop distance must be positive')
        qty = risk / dist
        trade = {'id': len(_state['trades']) + len(_state['open']) + 1, 'symbol': symbol, 'side': side,
                 'entry': entry, 'stop_loss': sl, 'take_profit': tp, 'qty': qty, 'risk_usdt': risk,
                 'opened_at': int(time.time() * 1000)}
        _state['open'].append(trade)
        return paper_state()


def paper_mark_to_market():
    try:
        t = tickers()
        mp = {x['symbol']: float(x.get('lastPrice') or 0) for x in t}
    except Exception:
        return {'state': paper_state(), 'closed': []}
    closed = []
    with _lock:
        for trade in list(_state['open']):
            price = mp.get(trade['symbol'])
            if not price: continue
            hit = None
            if trade['side'] == 'LONG':
                if price <= trade['stop_loss']: hit = ('SL', trade['stop_loss'])
                elif price >= trade['take_profit']: hit = ('TP', trade['take_profit'])
            else:
                if price >= trade['stop_loss']: hit = ('SL', trade['stop_loss'])
                elif price <= trade['take_profit']: hit = ('TP', trade['take_profit'])
            if hit:
                label, exit_price = hit
                pnl = (exit_price - trade['entry']) * trade['qty'] if trade['side'] == 'LONG' else (trade['entry'] - exit_price) * trade['qty']
                rec = {**trade, 'exit': exit_price, 'pnl': round(pnl, 6), 'result': label, 'closed_at': int(time.time() * 1000)}
                _state['balance'] += pnl; _state['trades'].append(rec); _state['open'].remove(trade); closed.append(rec)
    return {'state': paper_state(), 'closed': closed}


def paper_reset():
    with _lock:
        _state['balance'] = 1000.0; _state['trades'] = []; _state['open'] = []
        return paper_state()
