import os, time
from engine import MODE, MAX_POSITIONS, scan_market, demo_state, demo_open, paper_state, paper_open, paper_mark_to_market

STRATEGY_DEFAULT = os.getenv('STRATEGY_MODE','normal').lower() if os.getenv('STRATEGY_MODE','normal').lower() in ('normal','scalp') else 'normal'

def _strategy_config():
    # NORMAL keeps the current multi-timeframe setup. SCALP uses a faster 5M setup
    # and a stricter score gate; exit management remains under the same risk engine.
    mode = STRATEGY_DEFAULT
    try:
        from app import strategy_state
        mode = strategy_state.get('mode', mode)
    except Exception:
        pass
    
        from journal import get_setting
        normal_gate = int(get_setting('normal_gate', os.getenv('NORMAL_SCORE_GATE','70')))
        scalp_gate = int(get_setting('scalp_gate', os.getenv('SCALP_SCORE_GATE','60')))
    except Exception:
        normal_gate = int(os.getenv('NORMAL_SCORE_GATE','70'))
        scalp_gate = int(os.getenv('SCALP_SCORE_GATE','60'))
    return mode, ('5' if mode == 'scalp' else '15'), (scalp_gate if mode == 'scalp' else normal_gate)


SCAN_CANDIDATES = 24

def run_auto_cycle():
    """Single idempotency-aware trading cycle. Web UI and background worker share this logic."""
    strategy, setup_interval, score_gate = _strategy_config()
    if MODE in ('demo', 'live'):
        state = demo_state()
        if not state.get('configured'):
            return None, 'Exchange API not configured', None
        if state.get('error'):
            return None, 'Exchange sync error', None
        if state.get('risk_locked'):
            return None, 'risk lock active — no new exchange entry', None
        if len(state.get('positions', [])) >= MAX_POSITIONS:
            return None, f'max {MAX_POSITIONS} exchange positions — monitoring', None
        result = scan_market(setup_interval, SCAN_CANDIDATES, entry_threshold=score_gate, strategy=strategy)
        candidates = [x for x in result.get('results', [])
                      if x.get('direction') in ('LONG','SHORT')
                      and float(x.get('score',0)) >= score_gate
                      and x.get('stop_loss') and x.get('take_profit')
                      and x.get('decision') in (f'OPEN {x.get("direction")}',)]
        candidates.sort(key=lambda x: float(x.get('score',0)), reverse=True)
        opened, rejected = [], []
        for x in candidates[:MAX_POSITIONS]:
            try:
                demo_open({'symbol':x['symbol'],'side':x['direction'],'entry':x['price'],
                           'stop_loss':x['stop_loss'],'take_profit':x['take_profit'],'risk_pct':2.0})
                opened.append(f"{strategy.upper()} {x['direction']} {x['symbol']} {x['score']}/100")
            except (ValueError, RuntimeError) as e:
                rejected.append(f"{x['symbol']}: {e}")
        prefix = 'AUTO LIVE' if MODE == 'live' else 'AUTO DEMO'
        action = f"{prefix}: {', '.join(opened)}" if opened else 'scan complete — no exchange entry'
        if rejected and not opened:
            action += f" • {rejected[0]}"
        return result, action, None
    paper_mark_to_market()
    state = paper_state()
    if state.get('risk_locked'):
        return None, 'risk lock active — no paper entry', None
    if len(state.get('open', [])) >= MAX_POSITIONS:
        return None, f'max {MAX_POSITIONS} paper positions — monitoring', None
    result = scan_market(setup_interval, SCAN_CANDIDATES, entry_threshold=score_gate, strategy=strategy)
    candidates = [x for x in result.get('results', [])
                  if x.get('direction') in ('LONG','SHORT') and float(x.get('score',0)) >= score_gate
                  and x.get('stop_loss') and x.get('take_profit')
                      and x.get('decision') in (f'OPEN {x.get("direction")}',)]
    candidates.sort(key=lambda x: float(x.get('score',0)), reverse=True)
    opened=[]; rejected=[]
    for x in candidates[:MAX_POSITIONS]:
        try:
            paper_open({'symbol':x['symbol'],'side':x['direction'],'entry':x['price'],
                        'stop_loss':x['stop_loss'],'take_profit':x['take_profit'],'risk_pct':2.0})
            opened.append(f"{strategy.upper()} {x['direction']} {x['symbol']} {x['score']}/100")
        except ValueError as e:
            rejected.append(f"{x['symbol']}: {e}")
    action = 'AUTO PAPER: ' + ', '.join(opened) if opened else 'scan complete — no paper entry'
    if rejected and not opened:
        action += f" • {rejected[0]}"
    return result, action, None


def worker_loop(interval=180):
    from journal import init_db
    init_db()
    enabled = os.getenv('AUTO_ENABLED','false').lower() == 'true'
    if not enabled:
        while True:
            time.sleep(30)
    while True:
        started = time.time()
        try:
            run_auto_cycle()
        except Exception:
            pass
        time.sleep(max(1, interval - (time.time() - started)))
