import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx
import pandas as pd

import os
import hashlib
import hmac
import json

MODE = os.getenv('BYBIT_MODE', 'paper').lower()
if MODE not in ('paper', 'demo', 'live'):
    MODE = 'paper'
BASE = {'demo':'https://api-demo.bybit.com', 'live':'https://api.bybit.com', 'paper':'https://api-testnet.bybit.com'}[MODE]
DEMO_API_KEY = os.getenv('BYBIT_DEMO_API_KEY', '')
DEMO_API_SECRET = os.getenv('BYBIT_DEMO_API_SECRET', '')
LIVE_API_KEY = os.getenv('BYBIT_LIVE_API_KEY', '')
LIVE_API_SECRET = os.getenv('BYBIT_LIVE_API_SECRET', '')
LIVE_TRADING_ARMED = os.getenv('LIVE_TRADING_ARMED', 'false').lower() == 'true'
TIMEOUT = 10.0
CACHE_TTL = 20.0
RETRY_COUNT = 2
PRIVATE_RETRY_COUNT = 2
DEMO_ACCOUNT_CACHE_TTL = 8.0
DEMO_CLOSED_PNL_CACHE_TTL = 30.0
MAX_POSITIONS = 4
RISK_PCT_DEFAULT = 2.0
LEVERAGE = 10.0
START_BALANCE = 10.0
DEMO_TRADING_BUDGET = float(os.getenv('DEMO_TRADING_BUDGET', '100'))
DEMO_MAX_DAILY_LOSS = float(os.getenv('DEMO_MAX_DAILY_LOSS', '20'))
DEMO_RISK_PCT = float(os.getenv('DEMO_RISK_PCT', '2'))
LIVE_TRADING_BUDGET = float(os.getenv('LIVE_TRADING_BUDGET', '100'))
LIVE_MAX_DAILY_LOSS = float(os.getenv('LIVE_MAX_DAILY_LOSS', '20'))
LIVE_RISK_PCT = float(os.getenv('LIVE_RISK_PCT', '2'))

# Scan budget: broad market discovery is one ticker request; expensive candle/microstructure
# calls are reserved for a small ranked subset.
TECH_CANDIDATES = 24
DEEP_CANDIDATES = 12
MICRO_CANDIDATES = 6
INSTRUMENT_CACHE_TTL = 600.0
DAILY_LOSS_LIMIT_PCT = 6.0
MAX_CONSECUTIVE_LOSSES = 3
LOSS_COOLDOWN_SEC = 30 * 60
ENTRY_SCORE_MIN = 70
ENTRY_RR_MIN = float(os.getenv('ENTRY_RR_MIN', '1.5'))
SCALP_TIME_STOP_MIN = int(os.getenv('SCALP_TIME_STOP_MIN', '15'))
MAX_MARGIN_FRACTION = 0.95
# Simulation assumptions only; change them when you know the fee/slippage model you want.
PAPER_FEE_RATE = 0.00055
PAPER_SLIPPAGE_RATE = 0.0002

_lock = threading.RLock()
_http = httpx.Client(timeout=TIMEOUT, headers={'User-Agent': 'BybitAI-Agent/5.8.0'}, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
_cache = {}
_state = {
    'balance': START_BALANCE,
    'trading_budget': START_BALANCE,
    'reserved_margin': 0.0,
    'initial_balance': START_BALANCE,
    'trades': [],
    'open': [],
    'day': datetime.now(timezone.utc).date().isoformat(),
    'day_start_balance': START_BALANCE,
    'loss_streak': 0,
    'last_loss_at': 0,
    'peak_equity': START_BALANCE,
    'mark_prices': {},
}

_demo_guard = {'day': datetime.now(timezone.utc).date().isoformat(), 'start_equity': None}


def _cached(key):
    item = _cache.get(key)
    if item and time.time() - item[0] < CACHE_TTL:
        return item[1]
    return None


def _put_cache(key, value):
    _cache[key] = (time.time(), value)
    return value


def _auth_headers(method, path, payload):
    if MODE == 'demo':
        key, secret = DEMO_API_KEY, DEMO_API_SECRET
    elif MODE == 'live':
        key, secret = LIVE_API_KEY, LIVE_API_SECRET
    else:
        return {}
    if not key or not secret:
        return {}
    ts = str(int(time.time() * 1000)); recv = '5000'
    from urllib.parse import urlencode
    body = urlencode(sorted(payload.items())) if method == 'GET' else json.dumps(payload, separators=(',', ':'))
    sign = hmac.new(secret.encode(), (ts + key + recv + body).encode(), hashlib.sha256).hexdigest()
    return {'X-BAPI-API-KEY': key, 'X-BAPI-TIMESTAMP': ts, 'X-BAPI-RECV-WINDOW': recv, 'X-BAPI-SIGN': sign}

def bybit_get(path, params):
    key = (MODE, path, tuple(sorted((str(k), str(v)) for k, v in params.items())))
    cached = _cached(key)
    if cached is not None: return cached
    last_error = None
    for attempt in range(RETRY_COUNT + 1):
        try:
            r = _http.get(BASE + path, params=params, headers=_auth_headers('GET', path, params))
            if r.status_code in (429, 500, 502, 503, 504) and attempt < RETRY_COUNT:
                time.sleep(0.45 * (2 ** attempt)); continue
            r.raise_for_status()
            try:
                j = r.json()
            except ValueError:
                raise RuntimeError(f'Bybit returned invalid JSON (HTTP {r.status_code})')
            if j.get('retCode') != 0:
                raise RuntimeError(f"Bybit {j.get('retCode')}: {j.get('retMsg', 'API error')}")
            return _put_cache(key, j.get('result', {}))
        except httpx.HTTPStatusError as e:
            last_error = RuntimeError(f'Bybit HTTP {e.response.status_code}')
        except httpx.RequestError as e:
            last_error = RuntimeError(f'Bybit connection error: {e}')
        except RuntimeError as e:
            last_error = e
        if attempt < RETRY_COUNT:
            time.sleep(0.45 * (2 ** attempt))
    raise last_error or RuntimeError('Bybit request failed')

def bybit_private_post(path, body):
    if MODE == 'demo':
        key, secret = DEMO_API_KEY, DEMO_API_SECRET
    elif MODE == 'live':
        if not LIVE_TRADING_ARMED: raise RuntimeError('LIVE trading is not armed')
        key, secret = LIVE_API_KEY, LIVE_API_SECRET
    else:
        raise RuntimeError('Private Bybit API is disabled in paper mode')
    if not key or not secret: raise RuntimeError(f'{MODE.upper()} API key/secret are not configured')
    # Never blindly retry order creation: a network timeout can happen after Bybit accepted
    # the order, and a retry could create a duplicate. Non-order control endpoints are safe
    # to retry on transient transport/server failures.
    attempts = 1 if path == '/v5/order/create' else (PRIVATE_RETRY_COUNT + 1)
    last_error = None
    for attempt in range(attempts):
        try:
            r = _http.post(BASE + path, json=body, headers={**_auth_headers('POST', path, body), 'Content-Type':'application/json'})
            if r.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(0.45 * (2 ** attempt)); continue
            r.raise_for_status()
            try:
                j = r.json()
            except ValueError:
                raise RuntimeError(f'Bybit returned invalid JSON (HTTP {r.status_code})')
            if j.get('retCode') != 0:
                raise RuntimeError(f"Bybit {j.get('retCode')}: {j.get('retMsg', 'API error')}")
            return j.get('result', {})
        except httpx.HTTPStatusError as e:
            last_error = RuntimeError(f'Bybit HTTP {e.response.status_code}')
        except httpx.RequestError as e:
            last_error = RuntimeError(f'Bybit connection error: {e}')
        except RuntimeError as e:
            last_error = e
        if attempt + 1 < attempts:
            time.sleep(0.45 * (2 ** attempt))
    raise last_error or RuntimeError('Bybit private request failed')


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


def score(frames, setup='15', micro=None, live_price=None, btc_context=None, entry_threshold=ENTRY_SCORE_MIN, strategy='normal'):
    if setup not in frames:
        raise ValueError('unsupported setup timeframe')
    f = {k: frame_features(v) for k, v in frames.items()}
    bd = bias(frames['D']) if 'D' in frames else 'NEUTRAL'
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
    if bd == 'LONG': ls += 10; reasons_long.append('1D trend')
    elif bd == 'SHORT': ss += 10; reasons_short.append('1D trend')
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

    price = float(live_price if live_price and live_price > 0 else z.close)
    # Build structural stop/target before deciding. This lets the entry gate reject
    # trades with poor geometry rather than treating a high market score as permission.
    long_sl = min(float(z.low), float(frames[setup].low.tail(20).min())) - 0.15 * atrv
    long_tp = price + 2 * max(price - long_sl, atrv * 0.5)
    short_sl = max(float(z.high), float(frames[setup].high.tail(20).max())) + 0.15 * atrv
    short_tp = price - 2 * max(short_sl - price, atrv * 0.5)
    long_rr = ((long_tp - price) / (price - long_sl)) if long_sl < price else 0.0
    short_rr = ((price - short_tp) / (short_sl - price)) if short_sl > price else 0.0
    spread_cost = max(float(spread or 0), 0.0) / 100.0
    round_trip_cost = 2 * (PAPER_FEE_RATE + PAPER_SLIPPAGE_RATE) + spread_cost
    long_net_edge = max(0.0, (long_tp - price) / max(price, 1e-12) - round_trip_cost)
    short_net_edge = max(0.0, (price - short_tp) / max(price, 1e-12) - round_trip_cost)

    def blockers(side):
        out=[]
        is_long=side=='LONG'
        if (b4 if is_long else b4) != side: out.append('4H trend против')
        if (b1 if is_long else b1) != side: out.append('1H trend против')
        if is_long and pos > 0.50: out.append('LONG: цена в premium')
        if not is_long and pos < 0.50: out.append('SHORT: цена в discount')
        if not (bull_sweep or bull_break) if is_long else not (bear_sweep or bear_break): out.append('нет подтверждения структуры')
        if is_long and z.close <= z.vwap: out.append('цена ниже VWAP')
        if not is_long and z.close >= z.vwap: out.append('цена выше VWAP')
        if is_long and z5.close <= z5.ema20: out.append('5M momentum против')
        if not is_long and z5.close >= z5.ema20: out.append('5M momentum против')
        if is_long and delta <= -5: out.append('sell delta')
        if not is_long and delta >= 5: out.append('buy delta')
        if is_long and oi_chg <= -5: out.append('OI падает')
        if not is_long and oi_chg >= 5: out.append('OI растёт против SHORT')
        if spread_bad: out.append('широкий spread')
        if volatility_bad: out.append('аномальная волатильность')
        if is_long and btc_block_long: out.append('BTC risk-off')
        if not is_long and btc_block_short: out.append('BTC risk-on')
        rr = long_rr if is_long else short_rr
        net = long_net_edge if is_long else short_net_edge
        if rr < ENTRY_RR_MIN: out.append(f'R:R ниже {ENTRY_RR_MIN:g}')
        if net <= 0: out.append('ожидаемый net edge после расходов ≤ 0')
        if (ls if is_long else ss) < float(entry_threshold): out.append(f'score ниже {int(entry_threshold)}')
        return out

    best = max(ls, ss)
    candidate = 'LONG' if ls > ss else ('SHORT' if ss > ls else 'WAIT')
    long_blockers = blockers('LONG'); short_blockers = blockers('SHORT')
    long_gate = not long_blockers
    short_gate = not short_blockers
    direction = 'LONG' if candidate=='LONG' and long_gate else ('SHORT' if candidate=='SHORT' and short_gate else 'WAIT')
    if direction == 'LONG':
        sl, tp, rr, reasons = long_sl, long_tp, long_rr, reasons_long
    elif direction == 'SHORT':
        sl, tp, rr, reasons = short_sl, short_tp, short_rr, reasons_short
    else:
        sl = tp = rr = None
        reasons = reasons_long if candidate=='LONG' else reasons_short if candidate=='SHORT' else []
    blockers_out = long_blockers if candidate=='LONG' else short_blockers if candidate=='SHORT' else ['нет явного directional перевеса']
    if direction == 'WAIT': reasons = list(reasons) + ['BLOCK: '+x for x in blockers_out]
    regime = 'HIGH_VOLATILITY' if atr_pct > 5 else ('LOW_VOLATILITY' if 0 < atr_pct < 0.08 else ('TREND_UP' if b4=='LONG' and b1=='LONG' else 'TREND_DOWN' if b4=='SHORT' and b1=='SHORT' else 'RANGE'))
    timing = 'NOW' if direction in ('LONG','SHORT') else ('BREAKOUT' if 'нет подтверждения структуры' in blockers_out else 'WAIT')
    if candidate=='LONG' and pos > 0.50: timing='PULLBACK'
    if candidate=='SHORT' and pos < 0.50: timing='PULLBACK'
    quality = 'A+' if direction in ('LONG','SHORT') and best >= float(entry_threshold)+10 else ('A' if direction in ('LONG','SHORT') else 'WAIT')
    return {
        'direction': direction, 'signal_direction': candidate, 'score': round(min(100.0, float(best)), 1),
        'entry_score': round(min(100.0, float(best)), 1), 'entry_threshold': int(entry_threshold),
        'signal_quality': quality, 'decision': f'OPEN {direction}' if direction in ('LONG','SHORT') else 'WAIT',
        'entry_timing': timing, 'market_regime': regime, 'blockers': blockers_out,
        'price': price, 'stop_loss': sl, 'take_profit': tp, 'rr': round(rr,2) if rr is not None else None,
        'expected_net_edge_pct': round((long_net_edge if candidate=='LONG' else short_net_edge)*100, 4),
        'htf_1d': bd, 'htf_4h': b4, 'htf_1h': b1, 'setup_tf': setup,
        'bull_sweep': bull_sweep, 'bear_sweep': bear_sweep, 'bull_structure_break': bull_break, 'bear_structure_break': bear_break,
        'entry_gate_long': long_gate, 'entry_gate_short': short_gate, 'range_position': round(pos, 3), 'atr_pct': round(atr_pct, 3),
        'reasons': reasons, 'long_score': round(min(100.0, ls), 1), 'short_score': round(min(100.0, ss), 1),
        'oi_change_pct': round(oi_chg, 2), 'orderbook_imbalance': round(ob_imb, 2), 'trade_delta_pct': round(delta, 2),
        'funding_rate': funding, 'spread_pct': round(spread, 4) if spread is not None else None,
        'scalp_time_stop_min': SCALP_TIME_STOP_MIN if strategy=='scalp' else None,
    }


def _scan_one(symbol, setup, live_price, micro=None, btc_context=None):
    frames = {k: klines(symbol, k, 220) for k in ['5', '15', '60', '240', 'D']}
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
    setup = str(interval)
    frames = {k: klines(symbol, k, 180) for k in sorted({'15', '60', '240', 'D', setup}, key=lambda x: ['5','15','60','240','D'].index(x))}
    bd, b4, b1 = bias(frames['D']), bias(frames['240']), bias(frames['60'])
    fsetup = frame_features(frames[setup]); z = fsetup.iloc[-1]
    bull_sweep, bear_sweep = sweep(frames[setup])
    bull_break, bear_break = structure_confirmation(frames[setup])
    hi, lo = frames[setup].high.tail(50).max(), frames[setup].low.tail(50).min()
    rng = max(float(hi-lo), 1e-12); pos = (float(z.close)-float(lo))/rng
    long_hint = (10 if bd=='LONG' else 0) + (20 if b4=='LONG' else 0) + (15 if b1=='LONG' else 0) + (12 if bull_sweep else 0) + (10 if bull_break else 0) + (8 if pos <= .45 else 0)
    short_hint = (10 if bd=='SHORT' else 0) + (20 if b4=='SHORT' else 0) + (15 if b1=='SHORT' else 0) + (12 if bear_sweep else 0) + (10 if bear_break else 0) + (8 if pos >= .55 else 0)
    hint = max(long_hint, short_hint)
    return {'symbol': symbol, 'price': float(tm[symbol].get('lastPrice') or z.close),
            'turnover24h': float(tm[symbol].get('turnover24h') or 0),
            'spreadPct': float(((float(tm[symbol].get('ask1Price') or 0)-float(tm[symbol].get('bid1Price') or 0))/max(float(tm[symbol].get('lastPrice') or 1),1e-9))*100),
            'htf_1d': bd, 'htf_1d': bd, 'htf_4h': b4, 'htf_1h': b1, 'hint': hint, 'frames': frames}


def _deep_one(item, interval, btc_context, entry_threshold=ENTRY_SCORE_MIN, strategy='normal'):
    symbol=item['symbol']; frames=item['frames']
    frames['5'] = klines(symbol, '5', 180)
    # Reuse the 15/60/240 frames already fetched in the technical stage.
    return score(frames, interval, micro=None, live_price=item['price'], btc_context=btc_context, entry_threshold=entry_threshold, strategy=strategy)


def _json_safe(value):
    # Convert numpy/pandas scalar values and non-finite floats before FastAPI serialisation.
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, 'item'):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def scan_market(interval='15', limit_symbols=TECH_CANDIDATES, entry_threshold=ENTRY_SCORE_MIN, strategy='normal'):
    if interval not in {'5', '15', '60'}:
        raise ValueError('interval must be 5, 15 or 60')
    universe, tm = _fast_market_universe()
    if not universe:
        return {'ok': True, 'mode':MODE, 'setup_interval':interval, 'universe_size':0, 'checked':0, 'technical_checked':0, 'deep_checked':0, 'micro_checked':0, 'results':[], 'failures':[]}

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
            try:
                preliminary.append(fut.result())
            except Exception as first_error:
                # A single candle request can fail transiently even when the public API is healthy.
                # Retry the symbol once outside the pool so one blip does not poison the whole scan.
                try:
                    preliminary.append(_technical_one(s, interval, tm))
                except Exception as second_error:
                    failures.append({'symbol':s,'stage':'technical','error':str(second_error),'retry_error':str(first_error)})
    preliminary.sort(key=lambda x:(x['hint'], x['turnover24h']), reverse=True)

    # Deep pass on only the strongest technical candidates.
    deep=preliminary[:DEEP_CANDIDATES]
    btc_item=next((x for x in preliminary if x['symbol']=='BTCUSDT'),None)
    if btc_item and all(x['symbol']!='BTCUSDT' for x in deep): deep[-1]=btc_item
    btc_context={'symbol':'BTCUSDT','bd':btc_item.get('htf_1d'),'b4':btc_item['htf_4h'],'b1':btc_item['htf_1h']} if btc_item else None
    scored=[]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures={ex.submit(_deep_one,item,interval,btc_context,entry_threshold,strategy):item for item in deep}
        for fut in as_completed(futures):
            item=futures[fut]
            try:
                a=fut.result(); a.update({'symbol':item['symbol'],'turnover24h':item['turnover24h'],'enriched':False}); scored.append(a)
            except Exception as e:
                failures.append({'symbol':item['symbol'],'stage':'deep','error':str(e)})

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
                a=score(item['frames'],interval,micro=micro_map[s],live_price=item['price'],btc_context=btc_context,entry_threshold=entry_threshold,strategy=strategy)
                a.update({'symbol':s,'turnover24h':x['turnover24h'],'enriched':True}); results.append(a)
                continue
            except Exception as e: failures.append({'symbol':s,'stage':'rescore','error':str(e)})
        x.pop('frames',None); results.append(x)
    results.sort(key=lambda x:x.get('score',x.get('hint',0)), reverse=True)
    return _json_safe({'ok':True,'mode':MODE,'setup_interval':interval,
            'universe_size':len(universe),'checked':len(top),'technical_checked':len(preliminary),'technical_target':len(top),'technical_skipped':max(0,len(top)-len(preliminary)),
            'deep_checked':len(deep),'deep_target':len(deep),'micro_checked':len(micro_map),'micro_target':len(micro_targets),
            'results':results,'failures':failures,
            'entry_threshold': int(entry_threshold), 'strategy': strategy, 'scan_policy':{'universe':'all active USDT linear perpetuals','technical_cap':TECH_CANDIDATES,
                           'deep_cap':DEEP_CANDIDATES,'micro_cap':MICRO_CANDIDATES,
                           'optimization':'persistent HTTP connections + bounded concurrency + retry/backoff + cached public data'}})

def _roll_day_locked():
    today = datetime.now(timezone.utc).date().isoformat()
    if _state['day'] != today:
        _state['day'] = today; _state['day_start_balance'] = _state['balance']; _state['loss_streak'] = 0; _state['last_loss_at'] = 0


def _paper_equity_locked(prices=None):
    prices = prices if prices is not None else _state.get('mark_prices', {})
    eq = _state['balance']; unreal = 0.0
    for t in _state['open']:
        p = prices.get(t['symbol'], t['entry'])
        gross = (p - t['entry']) * t['qty'] if t['side'] == 'LONG' else (t['entry'] - p) * t['qty']
        unreal += gross
    return eq + unreal, unreal


def set_paper_budget(amount):
    amount=float(amount)
    with _lock:
        if amount <= 0: raise ValueError('trading budget must be positive')
        if amount > _state['balance']: raise ValueError('trading budget cannot exceed current balance')
        reserved=sum(float(x.get('margin_required',0)) for x in _state['open'])
        if amount + 1e-9 < reserved: raise ValueError(f'budget cannot be below reserved margin ${reserved:.4f}')
        _state['trading_budget']=amount
        return paper_state()


_private_cache = {}
_private_cache_lock = threading.RLock()
_demo_last_good_state = None

def _private_cached(key, ttl):
    with _private_cache_lock:
        item = _private_cache.get(key)
        if item and time.time() - item[0] < ttl:
            return item[1]
    return None

def _private_put(key, value):
    with _private_cache_lock:
        _private_cache[key] = (time.time(), value)
    return value

def bybit_private_get(path, params, retries=PRIVATE_RETRY_COUNT):
    if MODE == 'demo':
        key, secret = DEMO_API_KEY, DEMO_API_SECRET
    elif MODE == 'live':
        key, secret = LIVE_API_KEY, LIVE_API_SECRET
    else:
        raise RuntimeError('Private Bybit API is disabled in paper mode')
    if not key or not secret: raise RuntimeError(f'{MODE.upper()} API key/secret are not configured')
    last_error = None
    for attempt in range(retries + 1):
        try:
            r = _http.get(BASE + path, params=params, headers=_auth_headers('GET', path, params))
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(0.5 * (2 ** attempt)); continue
            r.raise_for_status()
            try:
                j = r.json()
            except ValueError:
                raise RuntimeError(f'Bybit returned invalid JSON (HTTP {r.status_code})')
            if j.get('retCode') != 0:
                raise RuntimeError(f"Bybit {j.get('retCode')}: {j.get('retMsg', 'API error')}")
            return j.get('result', {})
        except httpx.HTTPStatusError as e:
            last_error = RuntimeError(f'Bybit HTTP {e.response.status_code}')
        except httpx.RequestError as e:
            last_error = RuntimeError(f'Bybit connection error: {e}')
        except RuntimeError as e:
            last_error = e
        if attempt < retries:
            time.sleep(0.5 * (2 ** attempt))
    raise last_error or RuntimeError('Bybit private request failed')

def _demo_wallet():
    key = ('wallet',)
    cached = _private_cached(key, DEMO_ACCOUNT_CACHE_TTL)
    return cached if cached is not None else _private_put(key, bybit_private_get('/v5/account/wallet-balance', {'accountType':'UNIFIED','coin':'USDT'}))

def _demo_positions():
    key = ('positions',)
    cached = _private_cached(key, DEMO_ACCOUNT_CACHE_TTL)
    if cached is not None: return cached
    return _private_put(key, bybit_private_get('/v5/position/list', {'category':'linear','settleCoin':'USDT'}).get('list', []))

def _demo_closed_pnl(limit=100):
    key = ('closed_pnl', min(int(limit), 100))
    cached = _private_cached(key, DEMO_CLOSED_PNL_CACHE_TTL)
    if cached is not None: return cached
    return _private_put(key, bybit_private_get('/v5/position/closed-pnl', {'category':'linear','limit':min(int(limit), 100)}).get('list', []))

def _invalidate_demo_account_cache():
    with _private_cache_lock:
        for key in list(_private_cache):
            if key and key[0] in {'wallet','positions','closed_pnl'}:
                _private_cache.pop(key, None)


def _demo_pnl_snapshot(wallet, positions, closed):
    acct = (wallet.get('list') or [{}])[0]
    now_ms = int(time.time() * 1000)
    day_start_ms = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    realized_7d = sum(float(x.get('closedPnl') or 0) for x in closed)
    realized_today = sum(float(x.get('closedPnl') or 0) for x in closed if int(x.get('updatedTime') or x.get('createdTime') or 0) >= day_start_ms)
    unrealized = sum(float(x.get('unrealisedPnl') or 0) for x in positions)
    # This is the account's current perp P&L view; it is intentionally separate from the bot's $100 budget.
    account_unrealized = float(acct.get('totalPerpUPL') or unrealized or 0)
    total_pnl_7d = realized_7d + account_unrealized
    daily_pnl = realized_today + account_unrealized
    reserved_margin = sum(abs(float(x.get('positionIM') or 0)) for x in positions)
    return {
        'realized_pnl_7d': round(realized_7d, 6),
        'realized_pnl_today': round(realized_today, 6),
        'unrealized_pnl': round(account_unrealized, 6),
        'total_pnl_7d': round(total_pnl_7d, 6),
        'daily_pnl': round(daily_pnl, 6),
        'daily_loss': round(max(0.0, -daily_pnl), 6),
        'reserved_margin': round(reserved_margin, 6),
        'day_start_ms': day_start_ms,
        'now_ms': now_ms,
    }


def _demo_available_usdt():
    result = _demo_wallet()
    lst = result.get('list', [])
    if not lst: raise RuntimeError('Demo wallet returned no UNIFIED account')
    acct = lst[0]
    available = acct.get('totalAvailableBalance')
    equity = acct.get('totalEquity')
    if available is None:
        for c in acct.get('coin', []):
            if c.get('coin') == 'USDT':
                available = c.get('availableToWithdraw') or c.get('walletBalance')
                equity = equity or c.get('equity')
                break
    return float(available or 0), float(equity or 0), result


def _demo_risk_qty(symbol, entry, sl):
    available, equity, _ = _demo_available_usdt()
    budget_limit = DEMO_TRADING_BUDGET if MODE == 'demo' else LIVE_TRADING_BUDGET
    risk_pct = DEMO_RISK_PCT if MODE == 'demo' else LIVE_RISK_PCT
    budget = min(budget_limit, available)
    risk_cash = min(equity, budget) * risk_pct / 100
    dist = abs(entry - sl)
    if dist <= 0: raise ValueError('invalid stop distance')
    qty = risk_cash / dist
    step, min_qty, max_qty = _symbol_rules(symbol)
    qty = _round_step(qty, step) if step else qty
    if max_qty > 0: qty = min(qty, max_qty)
    margin = entry * qty / LEVERAGE
    if margin > budget * MAX_MARGIN_FRACTION:
        qty = _round_step((budget * MAX_MARGIN_FRACTION * LEVERAGE) / entry, step) if step else (budget * MAX_MARGIN_FRACTION * LEVERAGE) / entry
        margin = entry * qty / LEVERAGE
    if qty <= 0 or (min_qty > 0 and qty < min_qty):
        raise ValueError(f'Demo position too small: available trading budget ${budget:.2f}')
    return qty, margin, available, equity



def _demo_guard_status(equity=None):
    # Use Bybit's actual closed PnL + current perp uPnL so the daily guard survives
    # Render restarts instead of depending on an in-memory start-equity snapshot.
    wallet = _demo_wallet()
    positions = [x for x in _demo_positions() if float(x.get('size') or 0) > 0]
    closed = _demo_closed_pnl(100)
    snap = _demo_pnl_snapshot(wallet, positions, closed)
    loss = snap['daily_loss']
    return loss, loss >= DEMO_MAX_DAILY_LOSS

def demo_open(d):
    if MODE not in ('demo', 'live'): raise ValueError('Exchange trading mode is not enabled')
    if MODE == 'live' and not LIVE_TRADING_ARMED: raise ValueError('LIVE trading is not armed')
    side = str(d['side']).upper(); symbol = str(d['symbol']).upper(); entry = float(d['entry']); sl = float(d['stop_loss']); tp = float(d['take_profit'])
    if side not in ('LONG','SHORT'): raise ValueError('side must be LONG or SHORT')
    if entry <= 0 or sl <= 0 or tp <= 0: raise ValueError('entry, stop_loss and take_profit must be positive')
    if side == 'LONG' and not (sl < entry < tp): raise ValueError('LONG requires stop_loss < entry < take_profit')
    if side == 'SHORT' and not (tp < entry < sl): raise ValueError('SHORT requires take_profit < entry < stop_loss')
    positions = [x for x in _demo_positions() if float(x.get('size') or 0) > 0]
    if len(positions) >= MAX_POSITIONS: raise ValueError(f'max {MAX_POSITIONS} demo positions')
    if any(x.get('symbol') == symbol for x in positions): raise ValueError('Position for this symbol already open')
    qty, margin, available, equity = _demo_risk_qty(symbol, entry, sl)
    daily_loss, locked = _demo_guard_status(equity)
    limit = DEMO_MAX_DAILY_LOSS if MODE == 'demo' else LIVE_MAX_DAILY_LOSS
    if locked: raise ValueError(f'Daily loss limit reached: ${daily_loss:.2f} / ${limit:.2f}')
    # Set leverage first. If account is already at the desired leverage, Bybit returns success.
    bybit_private_post('/v5/position/set-leverage', {'category':'linear','symbol':symbol,'buyLeverage':str(int(LEVERAGE)),'sellLeverage':str(int(LEVERAGE))})
    order = {
        'category':'linear','symbol':symbol,'side':'Buy' if side == 'LONG' else 'Sell','orderType':'Market','qty':str(qty),
        'positionIdx':0,'reduceOnly':False,'takeProfit':str(tp),'stopLoss':str(sl),
        'tpTriggerBy':'MarkPrice','slTriggerBy':'MarkPrice','orderLinkId':f'ai-{int(time.time()*1000)}'
    }
    result = bybit_private_post('/v5/order/create', order)
    _invalidate_demo_account_cache()
    return {'mode':MODE,'ok':True,'symbol':symbol,'side':side,'qty':qty,'margin_required':margin,'available_balance':available,'equity':equity,'order':result}


def exchange_positions():
    return [x for x in bybit_private_get('/v5/position/list', {'category':'linear','settleCoin':'USDT'}).get('list', []) if float(x.get('size') or 0) > 0]

def close_position(symbol):
    if MODE not in ('demo','live'):
        raise ValueError('Exchange position closing is unavailable in paper mode')
    symbol = str(symbol).upper()
    positions = [x for x in exchange_positions() if x.get('symbol') == symbol]
    if not positions:
        raise ValueError(f'No open position for {symbol}')
    p = positions[0]
    qty = p.get('size')
    side = 'Sell' if p.get('side') == 'Buy' else 'Buy'
    result = bybit_private_post('/v5/order/create', {
        'category':'linear','symbol':symbol,'side':side,'orderType':'Market','qty':str(qty),
        'positionIdx':int(p.get('positionIdx') or 0),'reduceOnly':True,'closeOnTrigger':True,
        'orderLinkId':f'ai-close-{int(time.time()*1000)}'
    })
    _invalidate_demo_account_cache()
    return {'mode':MODE,'ok':True,'symbol':symbol,'closed_qty':qty,'order':result}

def demo_state():
    global _demo_last_good_state
    if MODE not in ('demo','live'): return {'mode':'paper','configured':False}
    api_key, api_secret = (DEMO_API_KEY, DEMO_API_SECRET) if MODE == 'demo' else (LIVE_API_KEY, LIVE_API_SECRET)
    if not api_key or not api_secret:
        return {'mode':MODE,'configured':False,'error':f'{MODE.upper()} API key/secret missing'}
    warnings = []
    try:
        wallet = _demo_wallet()
    except Exception as e:
        # Do not replace a valid last-known account view with zeros during a transient
        # Bybit/network failure. Returning stale-but-explicit data keeps the UI trustworthy.
        if _demo_last_good_state is not None:
            stale = dict(_demo_last_good_state)
            stale['degraded'] = True
            stale['stale'] = True
            stale['warnings'] = list(stale.get('warnings') or []) + [f'Баланс временно не обновлён: {e}']
            return stale
        return {'mode':MODE,'configured':True,'degraded':True,'error':'wallet_sync_failed','error_detail':str(e),'warnings':['Не удалось получить баланс Demo. Повторная попытка будет выполнена автоматически.']}
    try:
        positions = [x for x in _demo_positions() if float(x.get('size') or 0) > 0]
    except Exception as e:
        positions = []
        warnings.append(f'Позиции временно недоступны: {e}')
    try:
        closed = _demo_closed_pnl(100)
    except Exception as e:
        closed = []
        warnings.append(f'История P&L временно недоступна: {e}')

    acct = (wallet.get('list') or [{}])[0]
    coin = next((x for x in acct.get('coin', []) if x.get('coin') == 'USDT'), {})
    equity = float(acct.get('totalEquity') or 0)
    usdt_wallet = float(coin.get('walletBalance') or 0)
    available_margin = float(acct.get('totalAvailableBalance') or 0)
    snap = _demo_pnl_snapshot(wallet, positions, closed)
    daily_loss = snap['daily_loss']
    locked = daily_loss >= (DEMO_MAX_DAILY_LOSS if MODE == 'demo' else LIVE_MAX_DAILY_LOSS)
    budget = max(0.0, DEMO_TRADING_BUDGET if MODE == 'demo' else LIVE_TRADING_BUDGET)
    bot_available = max(0.0, min(budget - snap['reserved_margin'], available_margin))
    state = {
        'mode':MODE,'configured':True,'degraded':bool(warnings),'stale':False,'warnings':warnings,
        'wallet':wallet,'positions':positions,'closed_pnl':closed[:20],
        'max_positions':MAX_POSITIONS,'trading_budget':budget,'bot_available_budget':round(bot_available, 6),
        'reserved_margin':snap['reserved_margin'],'daily_loss_limit':(DEMO_MAX_DAILY_LOSS if MODE == 'demo' else LIVE_MAX_DAILY_LOSS),'daily_loss':daily_loss,
        'daily_pnl':snap['daily_pnl'],'realized_pnl_today':snap['realized_pnl_today'],
        'realized_pnl_7d':snap['realized_pnl_7d'],'unrealized_pnl':snap['unrealized_pnl'],
        'total_pnl_7d':snap['total_pnl_7d'],'equity':equity,'usdt_wallet_balance':usdt_wallet,
        'available_margin':available_margin,'risk_pct':DEMO_RISK_PCT,'leverage':LEVERAGE,'risk_locked':locked,
    }
    _demo_last_good_state = dict(state)
    return state

def demo_account_state():
    return demo_state()

def paper_state():
    with _lock:
        _roll_day_locked()
        s = {k: (v.copy() if isinstance(v, list) else v) for k, v in _state.items()}
        realized_pnl = s['balance'] - s['initial_balance']
        _, unrealized_pnl = _paper_equity_locked()
        total_pnl = realized_pnl + unrealized_pnl
        s['realized_pnl'] = round(realized_pnl, 4)
        s['unrealized_pnl'] = round(unrealized_pnl, 4)
        s['pnl'] = round(total_pnl, 4)
        s['return_pct'] = round(total_pnl / s['initial_balance'] * 100, 3)
        daily_pnl = s['balance'] - s['day_start_balance']
        s['daily_pnl'] = round(daily_pnl, 4)
        s['daily_loss_pct'] = round(max(0.0, -daily_pnl) / max(s['day_start_balance'], 1e-9) * 100, 3)
        for t in s['open']:
            mark = s.get('mark_prices', {}).get(t['symbol'], t['entry'])
            gross = (mark - t['entry']) * t['qty'] if t['side'] == 'LONG' else (t['entry'] - mark) * t['qty']
            t['mark_price'] = round(mark, 10)
            t['unrealized_pnl'] = round(gross, 6)
            t['unrealized_pnl_pct'] = round(gross / max(t.get('margin_required', 1e-9), 1e-9) * 100, 3)
        s['risk_locked'] = s['daily_loss_pct'] >= DAILY_LOSS_LIMIT_PCT or s['loss_streak'] >= MAX_CONSECUTIVE_LOSSES
        wins = sum(1 for t in _state['trades'] if t.get('pnl', 0) > 0); losses = sum(1 for t in _state['trades'] if t.get('pnl', 0) < 0)
        s['win_rate'] = round(wins / max(1, wins + losses) * 100, 2)
        s['profit_factor'] = round(sum(max(0, t.get('pnl', 0)) for t in _state['trades']) / max(1e-9, sum(-min(0, t.get('pnl', 0)) for t in _state['trades'])), 2) if losses else None
        s['open_count'] = len(_state['open']); s['max_positions'] = MAX_POSITIONS; s['leverage'] = LEVERAGE; s['trading_budget'] = round(min(s.get('trading_budget', s['balance']), s['balance']), 6); s['reserved_margin'] = round(sum(float(x.get('margin_required',0)) for x in _state['open']), 6); s['available_margin_budget'] = round(max(0.0, s['trading_budget'] - s['reserved_margin']), 6); s['mode'] = MODE; s['risk_pct_default'] = RISK_PCT_DEFAULT; s['assumptions'] = {'fee_rate': PAPER_FEE_RATE, 'slippage_rate': PAPER_SLIPPAGE_RATE, 'leverage': LEVERAGE}
        return s


def _round_step(value, step):
    if not step or step <= 0: return value
    return math.floor(value / step) * step

def _symbol_rules(symbol):
    try:
        source = instruments()
    except Exception:
        return 0.0, 0.0, 0.0
    for x in source:
        if x.get('symbol') == symbol:
            lot = x.get('lotSizeFilter') or {}
            return float(lot.get('qtyStep') or 0), float(lot.get('minOrderQty') or 0), float(lot.get('maxOrderQty') or 0)
    return 0.0, 0.0, 0.0

def paper_open(d):
    side = str(d['side']).upper(); symbol = str(d['symbol']).upper(); entry = float(d['entry']); sl = float(d['stop_loss']); tp = float(d['take_profit']); risk_pct = float(d.get('risk_pct', RISK_PCT_DEFAULT))
    if side not in ('LONG', 'SHORT'): raise ValueError('side must be LONG or SHORT')
    if not symbol.endswith('USDT'): raise ValueError('symbol must be a USDT perpetual')
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
        if len(_state['open']) >= MAX_POSITIONS: raise ValueError(f'max {MAX_POSITIONS} open positions')
        if any(x['symbol'] == symbol for x in _state['open']): raise ValueError('position for this symbol already open')
        dist = abs(entry - sl); risk = _state['balance'] * risk_pct / 100
        qty = risk / dist
        step, min_qty, max_qty = _symbol_rules(symbol)
        qty = _round_step(qty, step) if step else qty
        if max_qty > 0: qty = min(qty, max_qty)
        budget = min(_state.get('trading_budget', _state['balance']), _state['balance'])
        reserved = sum(float(x.get('margin_required', 0)) for x in _state['open'])
        free_budget = max(0.0, budget - reserved)
        margin_required = entry * qty / LEVERAGE
        if margin_required > free_budget * MAX_MARGIN_FRACTION:
            allowed_margin = free_budget * MAX_MARGIN_FRACTION
            qty = _round_step((allowed_margin * LEVERAGE) / entry, step) if step else (allowed_margin * LEVERAGE) / entry
            margin_required = entry * qty / LEVERAGE
            risk = qty * dist
        if qty <= 0 or (min_qty > 0 and qty < min_qty): raise ValueError(f'position too small for available margin (${free_budget:.4f})')
        entry_exec = entry * (1 + PAPER_SLIPPAGE_RATE if side == 'LONG' else 1 - PAPER_SLIPPAGE_RATE)
        fee = entry_exec * qty * PAPER_FEE_RATE
        if fee > _state['balance'] * 0.05: raise ValueError('entry fee would be too large for current balance')
        _state['balance'] -= fee
        trade = {'id': len(_state['trades']) + len(_state['open']) + 1, 'symbol': symbol, 'side': side, 'entry': entry, 'entry_exec': entry_exec, 'stop_loss': sl, 'take_profit': tp, 'qty': qty, 'risk_usdt': risk, 'entry_fee': fee, 'leverage': LEVERAGE, 'notional': entry*qty, 'margin_required': margin_required, 'opened_at': int(time.time()*1000)}
        _state['open'].append(trade); _state['reserved_margin'] = sum(float(x.get('margin_required',0)) for x in _state['open'])
        return paper_state()


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
                _state['balance'] += pnl; _state['trades'].append(rec); _state['open'].remove(trade); _state['reserved_margin'] = sum(float(x.get('margin_required',0)) for x in _state['open'])
                if pnl < 0: _state['loss_streak'] += 1; _state['last_loss_at'] = time.time()
                else: _state['loss_streak'] = 0
                closed.append(rec)
        _state['mark_prices'] = mp
        equity, unreal = _paper_equity_locked(mp); _state['peak_equity'] = max(_state['peak_equity'], equity)
    out = paper_state(); out['equity'] = round(equity, 6); out['unrealized_pnl'] = round(unreal, 6); out['drawdown_pct'] = round(max(0, (_state['peak_equity'] - equity) / max(_state['peak_equity'], 1e-9) * 100), 3)
    return {'state': out, 'closed': closed}


def paper_reset():
    with _lock:
        _state['balance'] = START_BALANCE; _state['initial_balance'] = START_BALANCE; _state['trading_budget'] = START_BALANCE; _state['reserved_margin'] = 0.0; _state['trades'] = []; _state['open'] = []
        _state['day'] = datetime.now(timezone.utc).date().isoformat(); _state['day_start_balance'] = START_BALANCE
        _state['loss_streak'] = 0; _state['last_loss_at'] = 0; _state['peak_equity'] = START_BALANCE; _state['mark_prices'] = {}
        return paper_state()
