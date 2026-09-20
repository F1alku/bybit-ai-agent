import os, time
from engine import MODE, MAX_POSITIONS, scan_market, demo_state, demo_open, paper_state, paper_open, paper_mark_to_market

STRATEGY_DEFAULT = os.getenv('STRATEGY_MODE','both').lower() if os.getenv('STRATEGY_MODE','both').lower() in ('normal','scalp','both') else 'both'

def _strategy_config():
    # Read strategy mode from the web app when available and gates from the
    # persistent settings store. Keep independent fallbacks so one failure
    # cannot leave a gate variable uninitialised (the v5.8.1 bug).
    mode = STRATEGY_DEFAULT
    try:
        from app import strategy_state
        mode = strategy_state.get('mode', mode)
    except Exception:
        pass

    try:
        from journal import get_setting
        normal_gate = int(get_setting('normal_gate', os.getenv('NORMAL_SCORE_GATE', '70')))
        scalp_gate = int(get_setting('scalp_gate', os.getenv('SCALP_SCORE_GATE', '60')))
    except Exception:
        normal_gate = int(os.getenv('NORMAL_SCORE_GATE', '70'))
        scalp_gate = int(os.getenv('SCALP_SCORE_GATE', '60'))

    return mode, ('5' if mode == 'scalp' else '15'), (scalp_gate if mode == 'scalp' else normal_gate)


SCAN_CANDIDATES = 0

def run_auto_cycle():
    """Single trading cycle. NORMAL and SCALP may scan concurrently in the same cycle.
    A symbol can only have one one-way position, so duplicate symbols are resolved by score.
    """
    strategy, setup_interval, score_gate = _strategy_config()
    modes = ['normal','scalp'] if strategy == 'both' else [strategy]
    def scan_mode(mode):
        interval = '15' if mode == 'normal' else '5'
        gate = int(os.getenv('NORMAL_SCORE_GATE','70')) if mode == 'normal' else int(os.getenv('SCALP_SCORE_GATE','60'))
        try:
            from journal import get_setting
            gate = int(get_setting('normal_gate' if mode == 'normal' else 'scalp_gate', gate))
        except Exception:
            pass
        return scan_market(interval, SCAN_CANDIDATES, entry_threshold=gate, strategy=mode, full_market=True)

    if MODE in ('demo', 'live'):
        state = demo_state()
        if not state.get('configured'): return None, 'Exchange API not configured', None
        if state.get('error'): return None, 'Exchange sync error', None
        if state.get('risk_locked'): return None, 'risk lock active — no new exchange entry', None
        positions = state.get('positions', [])
        if len(positions) >= MAX_POSITIONS: return None, f'max {MAX_POSITIONS} exchange positions — monitoring', None
        scans = {m: scan_mode(m) for m in modes}
        candidates=[]
        for m,r in scans.items():
            gate = int(r.get('entry_threshold', 70 if m=='normal' else 60))
            for x in r.get('results', []):
                if x.get('direction') in ('LONG','SHORT') and float(x.get('score',0)) >= gate and x.get('stop_loss') and x.get('take_profit') and x.get('decision') == f"OPEN {x.get('direction')}":
                    y=dict(x); y['strategy']=m; candidates.append(y)
        # Same symbol is one position in one-way mode; keep the stronger score.
        best={}
        for x in candidates:
            key=x['symbol'];
            if key not in best or float(x.get('score',0)) > float(best[key].get('score',0)): best[key]=x
        candidates=sorted(best.values(), key=lambda x: float(x.get('score',0)), reverse=True)
        opened=[]; rejected=[]; used=len(positions)
        for x in candidates:
            if used >= MAX_POSITIONS: break
            try:
                demo_open({'symbol':x['symbol'],'side':x['direction'],'entry':x['price'],'stop_loss':x['stop_loss'],'take_profit':x['take_profit'],'risk_pct':2.0,'strategy':x.get('strategy'),'score':x.get('score'),'atr_pct':x.get('atr_pct'),'market_regime':x.get('market_regime'),'entry_timing':x.get('entry_timing'),'news_impact':x.get('news_impact')})
                opened.append(f"{x['strategy'].upper()} {x['direction']} {x['symbol']} {x['score']}/100"); used += 1
            except (ValueError, RuntimeError) as e: rejected.append(f"{x['symbol']}: {e}")
        result = scans[modes[0]] if len(modes)==1 else {'ok':True,'mode':MODE,'strategy':'both','scans':scans,'results':sum([r.get('results',[]) for r in scans.values()],[])}
        prefix='AUTO LIVE' if MODE=='live' else 'AUTO DEMO'
        action=f"{prefix}: {', '.join(opened)}" if opened else 'scan complete — no exchange entry'
        if rejected and not opened: action += f" • {rejected[0]}"
        return result, action, None

    paper_mark_to_market()
    state=paper_state()
    if state.get('risk_locked'): return None,'risk lock active — no paper entry',None
    if len(state.get('open',[])) >= MAX_POSITIONS: return None,f'max {MAX_POSITIONS} paper positions — monitoring',None
    scans={m:scan_mode(m) for m in modes}
    candidates=[]
    for m,r in scans.items():
        gate=int(r.get('entry_threshold',70 if m=='normal' else 60))
        for x in r.get('results',[]):
            if x.get('direction') in ('LONG','SHORT') and float(x.get('score',0)) >= gate and x.get('stop_loss') and x.get('take_profit') and x.get('decision')==f"OPEN {x.get('direction')}":
                y=dict(x); y['strategy']=m; candidates.append(y)
    best={}
    for x in candidates:
        if x['symbol'] not in best or float(x.get('score',0))>float(best[x['symbol']].get('score',0)): best[x['symbol']]=x
    opened=[]; rejected=[]; used=len(state.get('open',[]))
    for x in sorted(best.values(),key=lambda x:float(x.get('score',0)),reverse=True):
        if used>=MAX_POSITIONS: break
        try:
            paper_open({'symbol':x['symbol'],'side':x['direction'],'entry':x['price'],'stop_loss':x['stop_loss'],'take_profit':x['take_profit'],'risk_pct':2.0,'strategy':x.get('strategy'),'score':x.get('score'),'atr_pct':x.get('atr_pct'),'market_regime':x.get('market_regime'),'entry_timing':x.get('entry_timing'),'news_impact':x.get('news_impact')})
            opened.append(f"{x['strategy'].upper()} {x['direction']} {x['symbol']} {x['score']}/100"); used+=1
        except ValueError as e: rejected.append(f"{x['symbol']}: {e}")
    result=scans[modes[0]] if len(modes)==1 else {'ok':True,'mode':MODE,'strategy':'both','scans':scans,'results':sum([r.get('results',[]) for r in scans.values()],[])}
    action='AUTO PAPER: '+', '.join(opened) if opened else 'scan complete — no paper entry'
    if rejected and not opened: action += f" • {rejected[0]}"
    return result,action,None


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
