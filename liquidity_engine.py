"""Liquidity / liquidation intelligence.

Uses optional Hyblock Capital predicted liquidation levels/heatmap. These are
estimates, not a feed of individual traders' private stop orders. When the
external feed is unavailable, the module returns a neutral result so the core
Bybit scanner continues to work.
"""
import os, time, threading
import httpx

BASE = os.getenv('HYBLOCK_BASE_URL', 'https://api.hyblockcapital.com/v2').rstrip('/')
API_KEY = os.getenv('HYBLOCK_API_KEY', '').strip()
EXCHANGE = os.getenv('HYBLOCK_EXCHANGE', 'binance_perp_stable').strip()
TIMEOUT = float(os.getenv('HYBLOCK_TIMEOUT', '6'))
TTL = float(os.getenv('HYBLOCK_CACHE_TTL', '30'))
DIST_PCT = float(os.getenv('LIQUIDITY_ZONE_PCT', '2.0'))
MIN_ZONE_USD = float(os.getenv('LIQUIDITY_MIN_ZONE_USD', '0'))
_client = httpx.Client(timeout=TIMEOUT, headers={'User-Agent':'BybitAI-Agent-Liquidity/1.0'})
_lock = threading.RLock()
_cache = {}


def configured():
    return bool(API_KEY)


def status():
    return {'configured': configured(), 'exchange': EXCHANGE, 'base_url': BASE, 'source': 'hyblock' if configured() else 'native-fallback'}


def _get(path, params):
    key = (path, tuple(sorted((str(k), str(v)) for k,v in params.items())))
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    if not API_KEY:
        raise RuntimeError('HYBLOCK_API_KEY is not configured')
    r = _client.get(BASE + path, params=params, headers={'x-api-key': API_KEY})
    r.raise_for_status()
    j = r.json()
    if isinstance(j, dict) and j.get('code') not in (None, 0, '0'):
        raise RuntimeError(f"Hyblock {j.get('code')}: {j.get('msg','API error')}")
    data = j.get('data', []) if isinstance(j, dict) else []
    with _lock:
        _cache[key] = (time.time(), data)
    return data


def _coin(symbol):
    s = str(symbol).upper()
    return s[:-4] if s.endswith('USDT') else s.replace('-', '')


def analyze(symbol, price):
    """Return actionable liquidity context around current price."""
    base = {'enabled': configured(), 'source': 'hyblock' if configured() else 'native-fallback',
            'symbol': symbol, 'price': float(price or 0), 'error': None,
            'long_liq_below_usd': 0.0, 'short_liq_above_usd': 0.0,
            'nearest_long_liq_pct': None, 'nearest_short_liq_pct': None,
            'liquidity_bias': 'NEUTRAL', 'sweep_risk': 'LOW', 'sweep_direction': None,
            'decision': 'NEUTRAL', 'score_adjustment': 0.0, 'reasons': []}
    if not configured() or not price or price <= 0:
        base['reasons'].append('external liquidation feed unavailable; native market signals remain authoritative')
        return base
    try:
        rows = _get('/liquidationLevels', {
            'coin': _coin(symbol), 'exchange': EXCHANGE, 'leverage': 'all', 'position': 'all',
            'timestamp': int(time.time())
        })
        levels = []
        for x in rows or []:
            try:
                p = float(x.get('price')); size = float(x.get('size') or 0); side = str(x.get('side') or '').lower()
            except Exception:
                continue
            if p <= 0 or side not in ('long','short'):
                continue
            notional = abs(size)
            dist = abs(p-price)/price*100
            if dist <= DIST_PCT and notional >= MIN_ZONE_USD:
                levels.append((p, side, notional, dist))
        longs = [x for x in levels if x[1]=='long' and x[0] < price]
        shorts = [x for x in levels if x[1]=='short' and x[0] > price]
        base['long_liq_below_usd'] = sum(x[2] for x in longs)
        base['short_liq_above_usd'] = sum(x[2] for x in shorts)
        if longs:
            base['nearest_long_liq_pct'] = round(min(x[3] for x in longs), 4)
        if shorts:
            base['nearest_short_liq_pct'] = round(min(x[3] for x in shorts), 4)
        down, up = base['long_liq_below_usd'], base['short_liq_above_usd']
        total = down + up
        if total > 0:
            ratio = (up-down)/total
            if ratio > 0.25: base['liquidity_bias'] = 'SHORT_SQUEEZE_UP'
            elif ratio < -0.25: base['liquidity_bias'] = 'LONG_LIQUIDATION_DOWN'
            else: base['liquidity_bias'] = 'BALANCED'
        # A close liquidation cluster is a reason to wait for a sweep, not a blind entry.
        near_down = base['nearest_long_liq_pct'] is not None and base['nearest_long_liq_pct'] <= 0.6
        near_up = base['nearest_short_liq_pct'] is not None and base['nearest_short_liq_pct'] <= 0.6
        if near_down or near_up:
            base['sweep_risk'] = 'HIGH'
            base['sweep_direction'] = 'DOWN' if near_down and (not near_up or base['nearest_long_liq_pct'] <= base['nearest_short_liq_pct']) else 'UP'
            base['decision'] = 'WAIT_SWEEP'
            base['score_adjustment'] = -4.0
            base['reasons'].append(f"near liquidation cluster {base['sweep_direction']}")
        elif total > 0:
            base['sweep_risk'] = 'MEDIUM'
            base['decision'] = 'CONTEXT'
        return base
    except Exception as e:
        base['error'] = str(e)
        base['reasons'].append('Hyblock request failed; ignored for safety')
        return base


def apply_to_signal(signal, liquidity):
    """Apply liquidity context without allowing it to override hard structure gates."""
    if not liquidity:
        return signal
    signal['liquidity'] = liquidity
    adj = float(liquidity.get('score_adjustment') or 0)
    if adj:
        signal['score'] = round(max(0.0, min(100.0, float(signal.get('score',0)) + adj)), 1)
        signal['entry_score'] = signal['score']
    signal.setdefault('reasons', [])
    for r in liquidity.get('reasons', []):
        if r not in signal['reasons']:
            signal['reasons'].append(r)
    if liquidity.get('decision') == 'WAIT_SWEEP' and signal.get('direction') in ('LONG','SHORT'):
        # Do not kill an existing position here; this is an entry gate only.
        direction = signal.get('direction')
        sweep = liquidity.get('sweep_direction')
        if (direction == 'LONG' and sweep == 'DOWN') or (direction == 'SHORT' and sweep == 'UP'):
            signal['decision'] = 'WAIT LIQUIDITY SWEEP'
            signal['entry_timing'] = 'WAIT_LIQUIDITY_SWEEP'
            signal['blockers'] = list(signal.get('blockers') or []) + ['liquidity sweep not confirmed']
    return signal
