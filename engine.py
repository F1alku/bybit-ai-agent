import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx
import pandas as pd

BASE = 'https://api-testnet.bybit.com'
TIMEOUT = 15.0
CACHE_TTL = 8.0
MAX_POSITIONS = 2
RISK_PCT_DEFAULT = 2.0
LEVERAGE = 10.0
START_BALANCE = 10.0

# Scan budget: broad market discovery is one ticker request; expensive candle/microstructure
# calls are reserved for a small ranked subset.
TECH_CANDIDATES = 24
DEEP_CANDIDATES = 8
MICRO_CANDIDATES = 4
INSTRUMENT_CACHE_TTL = 600.0
DAILY_LOSS_LIMIT_PCT = 2.0
MAX_CONSECUTIVE_LOSSES = 3
LOSS_COOLDOWN_SEC = 30 * 60
# Simulation assumptions only; change them when you know the fee/slippage model you want.
PAPER_FEE_RATE = 0.00055
PAPER_SLIPPAGE_RATE = 0.0002

_lock = threading.RLock()
_cache = {}
_state = {
    'balance': START_BALANCE,
    'initial_balance': START_BALANCE,
    'trades': [],
    'open': [],
    'day': datetime.now(timezone.utc).date().isoformat(),
    'day_start_balance': START_BALANCE,
    'loss_streak': 0,
    'last_loss_at': 0,
    'peak_equity': START_BALANCE,
}


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
        with httpx.Client(timeout=TIMEOUT, headers={'User-Agent': 'BybitAI-Agent/5.1'}) as c:
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


def tickers():
    return bybit_get('/v5/market/tickers', {'category': 'linear'}).get('list', [])


def instruments():
    key = ('__instruments__',)
    item = _cache.get(key)
    if item and time.time() - item[0] < INSTRUMENT_CACHE_TTL:
        return item[1]
    result = []
    cursor = None
    for _ in range(10):
        p = {'category': 'linear', 'status': 'Trading', 'limit': 1000}
        if cursor:
            p['cursor'] = cursor
        j = bybit_get('/v5/market/instruments-info', p)
        result += j.get('list', [])
        cursor = j.get('nextPageCursor')
        if not cursor:
            break
    out = [x for x in result if x.get('contractType') == 'LinearPerpetual'
           and x.get('quoteCoin') == 'USDT' and x.get('settleCoin') == 'USDT']
    _cache[key] = (time.time(), out)
    return out


def market_snapshot(limit=20):
    allowed = {x.get('symbol') for x in instruments() if x.get('symbol')}
    rows = []
    for x in tickers():
        symbol = x.get('symbol', '')
        if symbol not in allowed:
            continue
        try:
            bid = float(x.get('bid1Price') or 0)
            ask = float(x.get('ask1Price') or 0)
            last = float(x.get('lastPrice') or 0)
            spread_pct = ((ask - bid) / last * 100) if last > 0 and ask >= bid > 0 else None
            rows.append({
                'symbol': symbol, 'lastPrice': last,
                'turnover24h': float(x.get('turnover24h') or 0),
                'price24hPcnt': float(x.get('price24hPcnt') or 0) * 100,
                'highPrice24h': float(x.get('highPrice24h') or 0),
                'lowPrice24h': float(x.get('lowPrice24h') or 0),
                'volume24h': float(x.get('volume24h') or 0),
                'fundingRate': float(x.get('fundingRate') or 0),
                'openInterest': float(x.get('openInterest') or 0),
                'spreadPct': spread_pct,
            })
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x['turnover24h'], reverse=True)
    cap = max(1, min(int(limit), 100))
    return rows[:cap]

def klines(symbol, interval, limit=220):
    rows = bybit_get('/v5/market/kline', {
        'category': 'linear', 'symbol': symbol, 'interval': str(interval), 'limit': min(int(limit), 1000)
    }).get('list', [])
    if len(rows) < 65:
        raise RuntimeError(f'Not enough candles for {symbol} {interval}m')
    rows = list(reversed(rows))
    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
    for c in ['open', 'high', 'low', 'close', 'volume', 'turnover']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
    df = df.dropna().reset_index(drop=True)
    return df.iloc[:-1].reset_index(drop=True)  # closed candles only


def open_interest(symbol, interval='15min', limit=10):
    return bybit_get('/v5/market/open-interest', {
        'category': 'linear', 'symbol': symbol, 'intervalTime': interval, 'limit': min(int(limit), 200)
    }).get('list', [])


def orderbook(symbol, limit=50):
    return bybit_get('/v5/market/orderbook', {
        'category': 'linear', 'symbol': symbol, 'limit': min(int(limit), 1000)
    })


def recent_trades(symbol, limit=500):
    return bybit_get('/v5/market/recent-trade', {
        'category': 'linear', 'symbol': symbol, 'limit': min(int(limit), 1000)
    }).get('list', [])


def funding_history(symbol, limit=1):
    return bybit_get('/v5/market/funding/history', {
        'category': 'linear', 'symbol': symbol, 'limit': min(int(limit), 200)
    }).get('list', [])


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
    x['atr_pct'] = x.atr / x.close.replace(0, float('nan')) * 100
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
    return bool(last.low < prior.low.min() and last.close > prior.low.min()), bool(last.high > prior.high.max() and last.close < prior.high.max())


def structure_confirmation(df, lookback=5):
    if len(df) < lookback + 2:
        return False, False
    last = df.iloc[-1]
    prev = df.iloc[-lookback-1:-1]
    return bool(last.close > prev.high.max()), bool(last.close < prev.low.min())


def flow_features(symbol):
    out = {'oi_change_pct': 0.0, 'orderbook_imbalance': 0.0, 'trade_delta_pct': 0.0,
           'funding_rate': 0.0, 'spread_pct': None}
    try:
        oi = open_interest(symbol, '15min', 10)
        vals = [float(x.get('openInterest') or 0) for x in oi if float(x.get('openInterest') or 0) > 0]
        # Bybit returns the newest OI observation first.
        if len(vals) >= 2 and vals[-1] > 0:
            out['oi_change_pct'] = (vals[0] / vals[-1] - 1) * 100
    except Exception:
        pass
    try:
        ob = orderbook(symbol, 50)
        bids = [(float(p), float(q)) for p, q in ob.get('b', []) if float(p) > 0 and float(q) >= 0]
        asks = [(float(p), float(q)) for p, q in ob.get('a', []) if float(p) > 0 and float(q) >= 0]
        bn = sum(p * q for p, q in bids[:20]); an = sum(p * q for p, q in asks[:20])
        if bn + an > 0:
            out['orderbook_imbalance'] = (bn - an) / (bn + an) * 100
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            if mid > 0:
                out['spread_pct'] = (asks[0][0] - bids[0][0]) / mid * 100
    except Exception:
        pass
    try:
        trades = recent_trades(symbol, 500)
        buy = sum(float(x.get('size') or 0) for x in trades if x.get('side') == 'Buy')
        sell = sum(float(x.get('size') or 0) for x in trades if x.get('side') == 'Sell')
        if buy + sell > 0:
            out['trade_delta_pct'] = (buy - sell) / (buy + sell) * 100
    except Exception:
        pass
    try:
        fr = funding_history(symbol, 1)
        if fr:
            out['funding_rate'] = float(fr[0].get('fundingRate') or 0)
    except Exception:
        pass
    return out


def score(frames, setup='15', micro=None, live_price=None, btc_context=None):
    if setup not in frames:
        raise ValueError('unsupported setup timeframe')
    f = {k: frame_features(v) for k, v in frames.items()}
    b4, b1 = bias(frames['240']), bias(frames['60'])
    setup_df = f[setup]; z = setup_df.iloc[-1]
    hi, lo = setup_df.high.tail(50).max(), setup_df.low.tail(50).min()
    rng = max(float(hi - lo), 1e-12); pos = (float(z.close) - float(lo)) / rng
    bull_sweep, bear_sweep = sweep(frames[setup])
    bull_break, bear_break = structure_confirmation(frames[setup])
    micro = micro or {}
    oi_chg = float(micro.get('oi_change_pct') or 0); ob_imb = float(micro.get('orderbook_imbalance') or 0)
    delta = float(micro.get('trade_delta_pct') or 0); funding = float(micro.get('funding_rate') or 0)
    spread = micro.get('spread_pct')
    ls = ss = 0.0; reasons_long, reasons_short = [], []
    if b4 == 'LONG': ls += 20; reasons_long.append('4H trend')
    elif b4 == 'SHORT': ss += 20; reasons_short.append('4H trend')
    if b1 == 'LONG': ls += 15; reasons_long.append('1H trend')
    elif b1 == 'SHORT': ss += 15; reasons_short.append('1H trend')
    if bull_sweep: ls += 12; reasons_long.append('liquidity sweep')
    if bear_sweep: ss += 12; reasons_short.append('liquidity sweep')
    if bull_break: ls += 10; reasons_long.append('structure break')
    if bear_break: ss += 10; reasons_short.append('structure break')
    if pos <= 0.45: ls += 8; reasons_long.append('discount')
    elif pos >= 0.55: ss += 8; reasons_short.append('premium')
    if z.close > z.vwap: ls += 5; reasons_long.append('above VWAP')
    elif z.close < z.vwap: ss += 5; reasons_short.append('below VWAP')
    if pd.notna(z.vol_ma20) and z.volume > z.vol_ma20 * 1.05:
        if z.close >= z.open: ls += 5; reasons_long.append('volume confirmation')
        else: ss += 5; reasons_short.append('volume confirmation')
    z5 = f['5'].iloc[-1]
    if z5.close > z5.ema20: ls += 5; reasons_long.append('5M momentum')
    elif z5.close < z5.ema20: ss += 5; reasons_short.append('5M momentum')
    if 52 <= z.rsi <= 68: ls += 5; reasons_long.append('RSI confirmation')
    elif 32 <= z.rsi <= 48: ss += 5; reasons_short.append('RSI confirmation')
    if oi_chg > 1.0: ls += 5; reasons_long.append('OI rising')
    elif oi_chg < -1.0: ss += 5; reasons_short.append('OI falling')
    if ob_imb > 8: ls += 4; reasons_long.append('bid imbalance')
    elif ob_imb < -8: ss += 4; reasons_short.append('ask imbalance')
    if delta > 8: ls += 6; reasons_long.append('buy delta')
    elif delta < -8: ss += 6; reasons_short.append('sell delta')

    atrv = float(z.atr) if pd.notna(z.atr) and z.atr > 0 else max(float(z.close) * 0.003, 1e-9)
    atr_pct = float(z.atr_pct) if pd.notna(z.atr_pct) else 0.0
    extension = abs(float(z.close) - float(z.ema20)) / atrv
    if extension > 1.8:
        if z.close > z.ema20: ls = max(0.0, ls - 10); reasons_long.append('overextended -10')
        else: ss = max(0.0, ss - 10); reasons_short.append('overextended -10')
    if funding > 0.0015:
        ls = max(0.0, ls - 3); reasons_long.append('funding crowded -3')
    elif funding < -0.0015:
        ss = max(0.0, ss - 3); reasons_short.append('funding crowded -3')

    spread_bad = spread is not None and spread > 0.12
    if spread_bad:
        ls = max(0.0, ls - 8); ss = max(0.0, ss - 8)
        reasons_long.append('wide spread -8'); reasons_short.append('wide spread -8')

    # Avoid dead markets and abnormal spikes for a short-horizon setup.
    volatility_bad = atr_pct > 5.0 or (atr_pct > 0 and atr_pct < 0.08)
    if volatility_bad:
        ls = max(0.0, ls - 8); ss = max(0.0, ss - 8)

    btc_block_long = bool(btc_context and btc_context.get('symbol') != 'BTCUSDT' and btc_context.get('b4') == 'SHORT' and btc_context.get('b1') == 'SHORT')
    btc_block_short = bool(btc_context and btc_context.get('symbol') != 'BTCUSDT' and btc_context.get('b4') == 'LONG' and btc_context.get('b1') == 'LONG')
    if btc_block_long: ls = max(0.0, ls - 15); reasons_long.append('BTC risk-off -15')
    if btc_block_short: ss = max(0.0, ss - 15); reasons_short.append('BTC risk-on -15')

    long_gate = (b4 == 'LONG' and b1 == 'LONG' and pos <= 0.50 and (bull_sweep or bull_break)
                 and z.close > z.vwap and z5.close > z5.ema20 and delta > -5 and oi_chg > -5
                 and not spread_bad and not volatility_bad and not btc_block_long)
    short_gate = (b4 == 'SHORT' and b1 == 'SHORT' and pos >= 0.50 and (bear_sweep or bear_break)
                  and z.close < z.vwap and z5.close < z5.ema20 and delta < 5 and oi_chg < 5
                  and not spread_bad and not volatility_bad and not btc_block_short)

    direction = 'WAIT'; best = max(ls, ss)
    if ls >= 70 and ls > ss and long_gate: direction = 'LONG'
    elif ss >= 70 and ss > ls and short_gate: direction = 'SHORT'
    price = float(live_price if live_price and live_price > 0 else z.close)
    if direction == 'LONG':
        sl = min(float(z.low), float(frames[setup].low.tail(20).min())) - 0.15 * atrv
        tp = price + 2 * max(price - sl, atrv * 0.5); reasons = reasons_long
    elif direction == 'SHORT':
        sl = max(float(z.high), float(frames[setup].high.tail(20).max())) + 0.15 * atrv
        tp = price - 2 * max(sl - price, atrv * 0.5); reasons = reasons_short
    else:
        sl = tp = None; reasons = reasons_long if ls >= ss else reasons_short
        reasons.append('entry gate not confirmed' if best >= 70 else 'score below 70')
    rr = None
    if direction == 'LONG' and sl < price: rr = round((tp - price) / (price - sl), 2)
    elif direction == 'SHORT' and sl > price: rr = round((price - tp) / (sl - price), 2)
    return {
        'direction': direction, 'score': round(min(100.0, float(best)), 1), 'price': price,
        'stop_loss': sl, 'take_profit': tp, 'rr': rr, 'htf_4h': b4, 'htf_1h': b1,
        'setup_tf': setup, 'bull_sweep': bull_sweep, 'bear_sweep': bear_sweep,
        'bull_structure_break': bull_break, 'bear_structure_break': bear_break,
        'entry_gate_long': long_gate, 'entry_gate_short': short_gate,
        'range_position': round(pos, 3), 'atr_pct': round(atr_pct, 3), 'reasons': reasons,
        'long_score': round(min(100.0, ls), 1), 'short_score': round(min(100.0, ss), 1),
        'oi_change_pct': round(oi_chg, 2), 'orderbook_imbalance': round(ob_imb, 2),
        'trade_delta_pct': round(delta, 2), 'funding_rate': funding,
        'spread_pct': round(spread, 4) if spread is not None else None,
    }


def _scan_one(symbol, setup, live_price, micro=None, btc_context=None):
    frames = {k: klines(symbol, k, 220) for k in ['5', '15', '60', '240']}
    return score(frames, setup, micro=micro, live_price=live_price, btc_context=btc_context)


def _ticker_map():
    return {x.get('symbol'): x for x in tickers() if x.get('symbol')}


def _fast_market_universe():
    allowed = {x.get('symbol') for x in instruments() if x.get('symbol')}
    tm = _ticker_map()
    rows = []
    for symbol, x in tm.items():
        if symbol not in allowed:
            continue
        try:
            turnover = float(x.get('turnover24h') or 0)
            last = float(x.get('lastPrice') or 0)
            bid = float(x.get('bid1Price') or 0); ask = float(x.get('ask1Price') or 0)
            spread = ((ask-bid)/last*100) if last > 0 and ask >= bid > 0 else 999.0
            chg = abs(float(x.get('price24hPcnt') or 0))*100
            # Broad discovery: exclude dead/obviously broken markets, but don't hard-cap by symbol.
            if last <= 0 or turnover <= 0 or spread >= 1.0:
                continue
            rows.append({'symbol': symbol, 'turnover24h': turnover, 'lastPrice': last,
                         'spreadPct': spread, 'absChange24h': chg,
                         'price24hPcnt': float(x.get('price24hPcnt') or 0)*100})
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: (x['turnover24h'], -x['spreadPct']), reverse=True)
    return rows, tm


def _technical_one(symbol, interval, tm):
    # Cheap technical pass: 4H + 1H + setup. 5M and microstructure wait for the shortlist.
    frames = {k: klines(symbol, k, 180) for k in ['15', '60', '240']}
    b4, b1 = bias(frames['240']), bias(frames['60'])
    f15 = frame_features(frames['15']); z = f15.iloc[-1]
    bull_sweep, bear_sweep = sweep(frames['15'])
    bull_break, bear_break = structure_confirmation(frames['15'])
    hi, lo = frames['15'].high.tail(50).max(), frames['15'].low.tail(50).min()
    rng = max(float(hi-lo), 1e-12); pos = (float(z.close)-float(lo))/rng
    long_hint = (20 if b4=='LONG' else 0) + (15 if b1=='LONG' else 0) + (12 if bull_sweep else 0) + (10 if bull_break else 0) + (8 if pos <= .45 else 0)
    short_hint = (20 if b4=='SHORT' else 0) + (15 if b1=='SHORT' else 0) + (12 if bear_sweep else 0) + (10 if bear_break else 0) + (8 if pos >= .55 else 0)
    hint = max(long_hint, short_hint)
    return {'symbol': symbol, 'price': float(tm[symbol].get('lastPrice') or z.close),
            'turnover24h': float(tm[symbol].get('turnover24h') or 0),
            'spreadPct': float(((float(tm[symbol].get('ask1Price') or 0)-float(tm[symbol].get('bid1Price') or 0))/max(float(tm[symbol].get('lastPrice') or 1),1e-9))*100),
            'htf_4h': b4, 'htf_1h': b1, 'hint': hint, 'frames': frames}


def _deep_one(item, interval, btc_context):
    symbol=item['symbol']; frames=item['frames']
    frames['5'] = klines(symbol, '5', 180)
    # Reuse the 15/60/240 frames already fetched in the technical stage.
    return score(frames, interval, micro=None, live_price=item['price'], btc_context=btc_context)


def scan_market(interval='15', limit_symbols=TECH_CANDIDATES):
    if interval not in {'5', '15', '60'}:
        raise ValueError('interval must be 5, 15 or 60')
    universe, tm = _fast_market_universe()
    if not universe:
        return {'ok': True, 'mode':'paper-only', 'setup_interval':interval, 'universe_size':0, 'checked':0, 'technical_checked':0, 'deep_checked':0, 'micro_checked':0, 'results':[], 'failures':[]}

    # Always include BTC if it is a valid market; it is a regime filter, not a trade candidate priority.
    top = universe[:TECH_CANDIDATES]
    btc_row = next((x for x in universe if x['symbol']=='BTCUSDT'), None)
    if btc_row and all(x['symbol']!='BTCUSDT' for x in top):
        top[-1] = btc_row

    preliminary=[]; failures=[]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures={ex.submit(_technical_one,x['symbol'],interval,tm):x['symbol'] for x in top}
        for fut in as_completed(futures):
            s=futures[fut]
            try: preliminary.append(fut.result())
            except Exception as e: failures.append({'symbol':s,'stage':'technical','error':str(e)})
    preliminary.sort(key=lambda x:(x['hint'], x['turnover24h']), reverse=True)

    # Deep pass on only the strongest technical candidates.
    deep=preliminary[:DEEP_CANDIDATES]
    btc_item=next((x for x in preliminary if x['symbol']=='BTCUSDT'),None)
    if btc_item and all(x['symbol']!='BTCUSDT' for x in deep): deep[-1]=btc_item
    btc_context={'symbol':'BTCUSDT','b4':btc_item['htf_4h'],'b1':btc_item['htf_1h']} if btc_item else None
    scored=[]
    for item in deep:
        try:
            a=_deep_one(item,interval,btc_context); a.update({'symbol':item['symbol'],'turnover24h':item['turnover24h'],'enriched':False}); scored.append(a)
        except Exception as e: failures.append({'symbol':item['symbol'],'stage':'deep','error':str(e)})

    scored.sort(key=lambda x:x['score'], reverse=True)
    micro_targets=[x for x in scored if x['symbol']!='BTCUSDT'][:MICRO_CANDIDATES]
    if not micro_targets and scored: micro_targets=scored[:MICRO_CANDIDATES]
    micro_map={}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures={ex.submit(flow_features,x['symbol']):x['symbol'] for x in micro_targets}
        for fut in as_completed(futures):
            s=futures[fut]
            try: micro_map[s]=fut.result()
            except Exception as e: failures.append({'symbol':s,'stage':'micro','error':str(e)})

    results=[]
    for x in scored:
        s=x['symbol']
        if s in micro_map:
            try:
                # Frames are still held in the deep item, avoiding duplicate candle calls.
                item=next(i for i in deep if i['symbol']==s)
                a=score(item['frames'],interval,micro=micro_map[s],live_price=item['price'],btc_context=btc_context)
                a.update({'symbol':s,'turnover24h':x['turnover24h'],'enriched':True}); results.append(a)
                continue
            except Exception as e: failures.append({'symbol':s,'stage':'rescore','error':str(e)})
        x.pop('frames',None); results.append(x)
    results.sort(key=lambda x:x.get('score',x.get('hint',0)), reverse=True)
    return {'ok':True,'mode':'paper-only','setup_interval':interval,
            'universe_size':len(universe),'checked':len(top),'technical_checked':len(preliminary),
            'deep_checked':len(deep),'micro_checked':len(micro_map),
            'results':results,'failures':failures,
            'scan_policy':{'universe':'all active USDT linear perpetuals','technical_cap':TECH_CANDIDATES,
                           'deep_cap':DEEP_CANDIDATES,'micro_cap':MICRO_CANDIDATES}}

def _roll_day_locked():
    today = datetime.now(timezone.utc).date().isoformat()
    if _state['day'] != today:
        _state['day'] = today; _state['day_start_balance'] = _state['balance']; _state['loss_streak'] = 0; _state['last_loss_at'] = 0


def _paper_equity_locked(prices=None):
    prices = prices or {}
    eq = _state['balance']; unreal = 0.0
    for t in _state['open']:
        p = prices.get(t['symbol'], t['entry'])
        gross = (p - t['entry']) * t['qty'] if t['side'] == 'LONG' else (t['entry'] - p) * t['qty']
        unreal += gross
    return eq + unreal, unreal


def paper_state():
    with _lock:
        _roll_day_locked()
        s = {k: (v.copy() if isinstance(v, list) else v) for k, v in _state.items()}
        s['pnl'] = round(s['balance'] - s['initial_balance'], 4)
        s['return_pct'] = round(s['pnl'] / s['initial_balance'] * 100, 3)
        daily_pnl = s['balance'] - s['day_start_balance']
        s['daily_pnl'] = round(daily_pnl, 4)
        s['daily_loss_pct'] = round(max(0.0, -daily_pnl) / max(s['day_start_balance'], 1e-9) * 100, 3)
        s['risk_locked'] = s['daily_loss_pct'] >= DAILY_LOSS_LIMIT_PCT or s['loss_streak'] >= MAX_CONSECUTIVE_LOSSES
        wins = sum(1 for t in _state['trades'] if t.get('pnl', 0) > 0); losses = sum(1 for t in _state['trades'] if t.get('pnl', 0) < 0)
        s['win_rate'] = round(wins / max(1, wins + losses) * 100, 2)
        s['profit_factor'] = round(sum(max(0, t.get('pnl', 0)) for t in _state['trades']) / max(1e-9, sum(-min(0, t.get('pnl', 0)) for t in _state['trades'])), 2) if losses else None
        s['open_count'] = len(_state['open']); s['leverage'] = LEVERAGE; s['risk_pct_default'] = RISK_PCT_DEFAULT; s['assumptions'] = {'fee_rate': PAPER_FEE_RATE, 'slippage_rate': PAPER_SLIPPAGE_RATE, 'leverage': LEVERAGE}
        return s


def paper_open(d):
    side = str(d['side']).upper(); symbol = str(d['symbol']).upper(); entry = float(d['entry']); sl = float(d['stop_loss']); tp = float(d['take_profit']); risk_pct = float(d['risk_pct'])
    if side not in ('LONG', 'SHORT'): raise ValueError('side must be LONG or SHORT')
    if not symbol.endswith('USDT'): raise ValueError('paper symbol must be a USDT perpetual')
    if entry <= 0 or sl <= 0 or tp <= 0: raise ValueError('prices must be positive')
    if not 0 < risk_pct <= 5: raise ValueError('risk_pct must be between 0 and 5')
    if side == 'LONG' and not (sl < entry < tp): raise ValueError('LONG requires SL < entry < TP')
    if side == 'SHORT' and not (tp < entry < sl): raise ValueError('SHORT requires TP < entry < SL')
    with _lock:
        _roll_day_locked()
        if _state['balance'] <= 0: raise ValueError('balance depleted')
        if (_state['day_start_balance'] - _state['balance']) / max(_state['day_start_balance'], 1e-9) * 100 >= DAILY_LOSS_LIMIT_PCT: raise ValueError('daily loss limit reached')
        if _state['loss_streak'] >= MAX_CONSECUTIVE_LOSSES: raise ValueError('loss streak lock active')
        if _state['last_loss_at'] and time.time() - _state['last_loss_at'] < LOSS_COOLDOWN_SEC: raise ValueError('cooldown active after loss')
        if len(_state['open']) >= MAX_POSITIONS: raise ValueError(f'max {MAX_POSITIONS} open paper positions')
        if any(x['symbol'] == symbol for x in _state['open']): raise ValueError('position for this symbol already open')
        risk = _state['balance'] * risk_pct / 100; dist = abs(entry - sl)
        qty = risk / dist
        notional = entry * qty
        margin_required = notional / LEVERAGE
        if margin_required > _state['balance'] * 0.95:
            # With a tiny account, reject positions whose margin would consume nearly all free balance.
            raise ValueError('margin requirement too high for current $10-style paper balance')
        entry_exec = entry * (1 + PAPER_SLIPPAGE_RATE if side == 'LONG' else 1 - PAPER_SLIPPAGE_RATE)
        fee = entry_exec * qty * PAPER_FEE_RATE
        _state['balance'] -= fee
        trade = {'id': len(_state['trades']) + len(_state['open']) + 1, 'symbol': symbol, 'side': side, 'entry': entry,
                 'entry_exec': entry_exec, 'stop_loss': sl, 'take_profit': tp, 'qty': qty, 'risk_usdt': risk,
                 'entry_fee': fee, 'leverage': LEVERAGE, 'notional': notional, 'margin_required': margin_required, 'opened_at': int(time.time() * 1000)}
        _state['open'].append(trade); return paper_state()


def paper_mark_to_market():
    try:
        t = tickers(); mp = {x['symbol']: float(x.get('lastPrice') or 0) for x in t}
    except Exception:
        return {'state': paper_state(), 'closed': []}
    closed = []
    with _lock:
        _roll_day_locked()
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
                exit_exec = exit_price * (1 - PAPER_SLIPPAGE_RATE if trade['side'] == 'LONG' else 1 + PAPER_SLIPPAGE_RATE)
                pnl = (exit_exec - trade['entry_exec']) * trade['qty'] if trade['side'] == 'LONG' else (trade['entry_exec'] - exit_exec) * trade['qty']
                exit_fee = abs(exit_exec * trade['qty']) * PAPER_FEE_RATE
                pnl -= exit_fee
                rec = {**trade, 'exit': exit_price, 'exit_exec': exit_exec, 'exit_fee': exit_fee, 'pnl': round(pnl, 6), 'result': label, 'closed_at': int(time.time() * 1000)}
                _state['balance'] += pnl; _state['trades'].append(rec); _state['open'].remove(trade)
                if pnl < 0: _state['loss_streak'] += 1; _state['last_loss_at'] = time.time()
                else: _state['loss_streak'] = 0
                closed.append(rec)
        equity, unreal = _paper_equity_locked(mp); _state['peak_equity'] = max(_state['peak_equity'], equity)
    out = paper_state(); out['equity'] = round(equity, 6); out['unrealized_pnl'] = round(unreal, 6); out['drawdown_pct'] = round(max(0, (_state['peak_equity'] - equity) / max(_state['peak_equity'], 1e-9) * 100), 3)
    return {'state': out, 'closed': closed}


def paper_reset():
    with _lock:
        _state['balance'] = START_BALANCE; _state['initial_balance'] = START_BALANCE; _state['trades'] = []; _state['open'] = []
        _state['day'] = datetime.now(timezone.utc).date().isoformat(); _state['day_start_balance'] = START_BALANCE
        _state['loss_streak'] = 0; _state['last_loss_at'] = 0; _state['peak_equity'] = START_BALANCE
        return paper_state()
