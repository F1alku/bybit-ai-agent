import os, time
from engine import MODE, MAX_POSITIONS, scan_market, demo_state, demo_open, paper_state, paper_open, paper_mark_to_market, manage_open_positions
from capital_allocator import allocate, validate_allocations, TOTAL_RISK_PCT, MAX_NEW_ENTRIES

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

def _trading_settings():
    import engine
    try:
        from journal import get_setting
        risk_pct = float(get_setting('risk_pct', engine.RISK_PCT_DEFAULT))
        leverage = float(get_setting('leverage', engine.LEVERAGE))
        max_positions = int(get_setting('max_positions', engine.MAX_POSITIONS))
    except Exception:
        risk_pct, leverage, max_positions = engine.RISK_PCT_DEFAULT, engine.LEVERAGE, engine.MAX_POSITIONS
    return risk_pct, leverage, max_positions

def run_auto_cycle():
    """Single trading cycle. NORMAL and SCALP may scan concurrently in the same cycle.
    A symbol can only have one one-way position, so duplicate symbols are resolved by score.
    """
    strategy, setup_interval, score_gate = _strategy_config()
    risk_pct, leverage, max_positions = _trading_settings()
    position_management = manage_open_positions() if MODE in ('demo','live') else {'ok': True, 'actions': []}
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
        if not state.get('configured'):
            return None, 'AUTO BLOCKED: Exchange API not configured', {'stage':'precheck','reason':'api_not_configured'}
        if state.get('error'):
            return None, f"AUTO BLOCKED: Exchange sync error — {state.get('error')}", {'stage':'precheck','reason':'exchange_sync'}
        if state.get('risk_locked'):
            return None, 'AUTO BLOCKED: daily risk lock active — no new exchange entry', {'stage':'precheck','reason':'daily_risk_lock'}
        positions = state.get('positions', [])
        if len(positions) >= max_positions:
            return None, f'AUTO BLOCKED: max {max_positions} exchange positions — monitoring', {'stage':'precheck','reason':'max_positions','open_positions':len(positions),'max_positions':max_positions}
        scans = {m: scan_mode(m) for m in modes}
        candidates=[]
        candidate_stats={m:{'signals':0,'openable':0} for m in modes}
        for m,r in scans.items():
            gate = int(r.get('entry_threshold', 70 if m=='normal' else 60))
            for x in r.get('results', []):
                if x.get('direction') in ('LONG','SHORT') and float(x.get('score',0)) >= gate:
                    candidate_stats[m]['signals'] += 1
                if x.get('direction') in ('LONG','SHORT') and float(x.get('score',0)) >= gate and x.get('stop_loss') and x.get('take_profit') and x.get('decision') == f"OPEN {x.get('direction')}":
                    candidate_stats[m]['openable'] += 1
                    y=dict(x); y['strategy']=m; candidates.append(y)
        # Same symbol is one position in one-way mode; keep the stronger score.
        best={}
        for x in candidates:
            key=x['symbol'];
            if key not in best or float(x.get('score',0)) > float(best[key].get('score',0)): best[key]=x
        candidates=sorted(best.values(), key=lambda x: float(x.get('score',0)), reverse=True)
        free_slots=max(0, max_positions-len(positions))
        try:
            cap_view=float((state.get('bot_capital') or {}).get('bot_available_capital') or state.get('bot_available_capital') or 100.0)
        except Exception:
            cap_view=100.0
        allocated=allocate(candidates, cap_view, total_risk_pct=TOTAL_RISK_PCT, max_new=min(MAX_NEW_ENTRIES, free_slots))
        allocation_check=validate_allocations(allocated, TOTAL_RISK_PCT)
        opened=[]; rejected=[]; used=len(positions)
        for x in allocated:
            if used >= max_positions: break
            a=x.get('allocation') or {}
            try:
                demo_open({'symbol':x['symbol'],'side':x['direction'],'entry':x['price'],'stop_loss':x['stop_loss'],'take_profit':x['take_profit'],'risk_pct':min(10.0,float(a.get('risk_pct_of_bot') or risk_pct)),'leverage':10.0,'capital_allocation_pct':a.get('capital_pct'),'strategy':x.get('strategy'),'score':x.get('score'),'atr_pct':x.get('atr_pct'),'market_regime':x.get('market_regime'),'entry_timing':x.get('entry_timing'),'news_impact':x.get('news_impact')})
                opened.append(f"{x['strategy'].upper()} {x['direction']} {x['symbol']} {x['score']}/100 • cap {a.get('capital_pct',0):.1f}% • risk {a.get('risk_pct_of_bot',0):.2f}%"); used += 1
            except (ValueError, RuntimeError) as e: rejected.append(f"{x.get('strategy','?').upper()} {x['symbol']}: {e}")
            except Exception as e: rejected.append(f"{x.get('strategy','?').upper()} {x['symbol']}: unexpected execution error: {e}")
        result = scans[modes[0]] if len(modes)==1 else {'ok':True,'mode':MODE,'strategy':'both','scans':scans,'results':sum([r.get('results',[]) for r in scans.values()],[])}
        prefix='AUTO LIVE' if MODE=='live' else 'AUTO DEMO'
        action=f"{prefix}: {', '.join(opened)}" if opened else 'scan complete — no exchange entry'
        if rejected: action += f" • rejected: {rejected[0]}"
        diagnostics={
            'stage':'execution',
            'position_management': position_management,
            'candidate_stats':candidate_stats,
            'candidates':len(candidates),
            'opened':len(opened),
            'rejected':rejected[:20],
            'rejection_count':len(rejected),
            'open_positions_before':len(positions),
            'max_positions':max_positions,
            'risk_policy_pct':TOTAL_RISK_PCT,
            'leverage_target':10.0,
            'allocation':allocated,
            'allocation_check':allocation_check,
        }
        return result, action, diagnostics

    paper_mark_to_market()
    state=paper_state()
    if state.get('risk_locked'):
        return None,'AUTO BLOCKED: paper daily risk lock active — no new entry',{'stage':'precheck','reason':'daily_risk_lock'}
    if len(state.get('open',[])) >= max_positions:
        return None,f'AUTO BLOCKED: max {max_positions} paper positions — monitoring',{'stage':'precheck','reason':'max_positions','open_positions':len(state.get('open',[])),'max_positions':max_positions}
    scans={m:scan_mode(m) for m in modes}
    candidates=[]
    candidate_stats={m:{'signals':0,'openable':0} for m in modes}
    for m,r in scans.items():
        gate=int(r.get('entry_threshold',70 if m=='normal' else 60))
        for x in r.get('results',[]):
            if x.get('direction') in ('LONG','SHORT') and float(x.get('score',0)) >= gate:
                candidate_stats[m]['signals'] += 1
            if x.get('direction') in ('LONG','SHORT') and float(x.get('score',0)) >= gate and x.get('stop_loss') and x.get('take_profit') and x.get('decision')==f"OPEN {x.get('direction')}":
                y=dict(x); y['strategy']=m; candidates.append(y)
    best={}
    for x in candidates:
        if x['symbol'] not in best or float(x.get('score',0))>float(best[x['symbol']].get('score',0)): best[x['symbol']]=x
    candidates=sorted(best.values(),key=lambda x:float(x.get('score',0)),reverse=True)
    free_slots=max(0, max_positions-len(state.get('open',[])))
    cap_view=float(state.get('available_margin_budget') or state.get('trading_budget') or 100.0)
    allocated=allocate(candidates, cap_view, total_risk_pct=TOTAL_RISK_PCT, max_new=min(MAX_NEW_ENTRIES, free_slots))
    allocation_check=validate_allocations(allocated, TOTAL_RISK_PCT)
    opened=[]; rejected=[]; used=len(state.get('open',[]))
    for x in allocated:
        if used>=max_positions: break
        a=x.get('allocation') or {}
        try:
            paper_open({'symbol':x['symbol'],'side':x['direction'],'entry':x['price'],'stop_loss':x['stop_loss'],'take_profit':x['take_profit'],'risk_pct':min(10.0,float(a.get('risk_pct_of_bot') or risk_pct)),'capital_allocation_pct':a.get('capital_pct'),'strategy':x.get('strategy'),'score':x.get('score'),'atr_pct':x.get('atr_pct'),'market_regime':x.get('market_regime'),'entry_timing':x.get('entry_timing'),'news_impact':x.get('news_impact')})
            opened.append(f"{x['strategy'].upper()} {x['direction']} {x['symbol']} {x['score']}/100 • cap {a.get('capital_pct',0):.1f}% • risk {a.get('risk_pct_of_bot',0):.2f}%"); used+=1
        except ValueError as e: rejected.append(f"{x['symbol']}: {e}")
    result=scans[modes[0]] if len(modes)==1 else {'ok':True,'mode':MODE,'strategy':'both','scans':scans,'results':sum([r.get('results',[]) for r in scans.values()],[])}
    action='AUTO PAPER: '+', '.join(opened) if opened else 'scan complete — no paper entry'
    if rejected: action += f" • rejected: {rejected[0]}"
    diagnostics={
        'stage':'execution',
        'candidate_stats':candidate_stats,
        'candidates':len(best),
        'opened':len(opened),
        'rejected':rejected[:20],
        'rejection_count':len(rejected),
        'open_positions_before':len(state.get('open',[])),
        'max_positions':max_positions,
        'risk_policy_pct':TOTAL_RISK_PCT,
        'leverage_target':10.0,
        'allocation':allocated,
        'allocation_check':allocation_check,
    }
    return result,action,diagnostics


def worker_loop(interval=60):
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
            # Worker must stay alive; details are surfaced by the web/API worker.
            pass
        time.sleep(max(1, interval - (time.time() - started)))
