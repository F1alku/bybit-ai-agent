from fastapi import FastAPI, HTTPException
import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
import os
import time
from contextlib import asynccontextmanager
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import market_snapshot, scan_market, paper_state, paper_open, paper_reset, paper_mark_to_market, set_paper_budget, MODE, demo_state, demo_open, close_position, trade_monitor_snapshot
from journal import init_db, recent as journal_recent, sync_closed_pnl, get_setting, set_setting
from learning_engine import build_learning_report, init_learning_db
from news_engine import snapshot as news_snapshot
from trader import run_auto_cycle

AUTO_INTERVAL_SEC = 180
SCAN_CANDIDATES = 0  # 0 = full active Bybit USDT perpetual market
auto_lock = threading.RLock()
scan_lock = threading.Lock()
scan_jobs = {}
scan_jobs_lock = threading.Lock()
scan_executor = ThreadPoolExecutor(max_workers=1)
strategy_state = {'mode': os.getenv('STRATEGY_MODE','both').lower() if os.getenv('STRATEGY_MODE','both').lower() in ('normal','scalp','both') else 'both', 'normal_gate': int(get_setting('normal_gate', os.getenv('NORMAL_SCORE_GATE','70'))), 'scalp_gate': int(get_setting('scalp_gate', os.getenv('SCALP_SCORE_GATE','60')))}
auto_state = {'enabled': os.getenv('AUTO_ENABLED','false').lower() == 'true', 'last_run': 0.0, 'last_scan': None, 'last_action': 'starting', 'error': None, 'last_success': 0.0}

@asynccontextmanager
async def lifespan(_app):
    init_db()
    init_learning_db()
    task = asyncio.create_task(_auto_loop()) if os.getenv('RUN_TRADER_IN_WEB','true').lower() == 'true' else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

app = FastAPI(title='Bybit AI Agent Web', version='5.11.1', lifespan=lifespan)
app.mount('/static', StaticFiles(directory='static'), name='static')

@app.middleware('http')
async def json_error_middleware(request, call_next):
    # Never let an unexpected exception turn an API response into Render's HTML 500 page.
    # The frontend can then always parse a JSON diagnostic and display the actual endpoint/error.
    try:
        return await call_next(request)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            'ok': False,
            'error': str(e) or e.__class__.__name__,
            'error_type': e.__class__.__name__,
            'path': request.url.path,
        })


def _auto_iteration():
    with auto_lock:
        if not auto_state['enabled']:
            return
    try:
        result, action, _ = run_auto_cycle()
        with auto_lock:
            auto_state['last_run'] = time.time()
            auto_state['last_scan'] = result
            auto_state['last_action'] = action
            auto_state['error'] = None
            if result is not None:
                auto_state['last_success'] = time.time()
    except Exception as e:
        with auto_lock:
            auto_state['last_run'] = time.time()
            auto_state['last_action'] = 'scan error'
            auto_state['error'] = str(e)


async def _auto_loop():
    while True:
        try:
            with auto_lock:
                enabled = auto_state['enabled']
            if enabled:
                await asyncio.to_thread(_auto_iteration)
        except Exception as e:
            with auto_lock:
                auto_state['error'] = str(e)
        await asyncio.sleep(AUTO_INTERVAL_SEC)


class ScanRequest(BaseModel):
    interval: str = Field('15', pattern=r'^(5|15|60)$')
    limit_symbols: int = Field(0, ge=0, le=2000)
    full_market: bool = True

class PaperOpenRequest(BaseModel):
    symbol: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    risk_pct: float = Field(2.0, gt=0, le=5)

@app.get('/')
def index(): return FileResponse('static/index.html')

@app.get('/api/health')
def health():
    import engine
    return {'ok': True, 'service': 'bybit-ai-agent-web', 'version': '5.10.1', 'mode': engine.MODE, 'live_armed': bool(getattr(engine, 'LIVE_TRADING_ARMED', False)), 'auto_scanner': auto_state['enabled'], 'strategy': strategy_state['mode']}

@app.get('/api/strategy')
def strategy_status():
    label = {'normal':'NORMAL','scalp':'SCALP','both':'NORMAL + SCALP'}.get(strategy_state['mode'], strategy_state['mode'].upper())
    return {'ok': True, 'strategy': strategy_state['mode'], 'label': label, 'normal_gate': strategy_state['normal_gate'], 'scalp_gate': strategy_state['scalp_gate']}

class StrategyGateRequest(BaseModel):
    mode: str = Field(..., pattern=r'^(normal|scalp)$')
    score_gate: int = Field(..., ge=0, le=100)

@app.post('/api/strategy/gate')
def strategy_gate(req: StrategyGateRequest):
    with auto_lock:
        strategy_state['normal_gate' if req.mode == 'normal' else 'scalp_gate'] = req.score_gate
        set_setting('normal_gate' if req.mode == 'normal' else 'scalp_gate', req.score_gate)
    return strategy_status()

@app.post('/api/strategy/toggle')
def strategy_toggle():
    with auto_lock:
        cycle = {'normal':'scalp','scalp':'both','both':'normal'}
        strategy_state['mode'] = cycle.get(strategy_state['mode'], 'both')
        auto_state['last_action'] = f"strategy mode: {strategy_state['mode'].upper()}"
    return strategy_status()

@app.get('/api/auto')
def auto_status():
    with auto_lock:
        s = dict(auto_state)
    s['enabled'] = bool(s['enabled'])
    s['interval_sec'] = AUTO_INTERVAL_SEC
    s['next_run_in_sec'] = max(0, int(AUTO_INTERVAL_SEC - (time.time() - s['last_run']))) if s['last_run'] else 0
    return s

@app.post('/api/auto/toggle')
def auto_toggle():
    with auto_lock:
        auto_state['enabled'] = not auto_state['enabled']
        auto_state['last_action'] = 'enabled by user' if auto_state['enabled'] else 'disabled by user'
    return auto_status()

@app.get('/api/news')
def news():
    try:
        return news_snapshot(force=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get('/api/trading-config')
def trading_config():
    import engine
    return {'ok': True, 'max_positions': engine.MAX_POSITIONS, 'default_leverage': engine.LEVERAGE, 'leverage_mode': engine.LEVERAGE_MODE, 'risk_pct': engine.RISK_PCT_DEFAULT, 'total_open_risk_pct': engine.TOTAL_OPEN_RISK_PCT, 'full_market': engine.FULL_MARKET_DEFAULT}

@app.get('/api/markets')
def markets():
    try: return {'ok': True, 'markets': market_snapshot(20)}
    except Exception as e: raise HTTPException(status_code=502, detail=str(e))

def _run_scan_job(job_id, interval, limit_symbols, entry_threshold, strategy):
    try:
        if strategy == 'both':
            normal = scan_market('15', limit_symbols, entry_threshold=entry_threshold, strategy='normal', full_market=True)
            scalp = scan_market('5', limit_symbols, entry_threshold=int(strategy_state['scalp_gate']), strategy='scalp', full_market=True)
            merged = {}
            for x in normal.get('results', []):
                y = dict(x); y['strategy'] = 'normal'; merged[(y.get('symbol'), 'normal')] = y
            for x in scalp.get('results', []):
                y = dict(x); y['strategy'] = 'scalp'; merged[(y.get('symbol'), 'scalp')] = y
            result = dict(normal)
            result['results'] = list(merged.values())
            result['results'].sort(key=lambda x: float(x.get('score', 0)), reverse=True)
            result['strategy'] = 'both'
            result['dual_scan'] = {'normal': normal, 'scalp': scalp}
            result['failures'] = normal.get('failures', []) + scalp.get('failures', [])
        else:
            result = scan_market(interval, limit_symbols, entry_threshold=entry_threshold, strategy=strategy, full_market=True)
        with scan_jobs_lock:
            scan_jobs[job_id] = {'status': 'done', 'result': result}
    except Exception as e:
        with scan_jobs_lock:
            scan_jobs[job_id] = {'status': 'error', 'error': str(e), 'error_type': e.__class__.__name__}
    finally:
        scan_lock.release()

@app.post('/api/scan')
def scan(req: ScanRequest):
    if not scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='Скан уже выполняется. Подожди завершения текущего цикла.')
    job_id = uuid.uuid4().hex[:12]
    with scan_jobs_lock:
        scan_jobs[job_id] = {'status': 'running', 'interval': req.interval, 'limit_symbols': req.limit_symbols}
        # Keep only the newest 20 job records so a long-lived Render instance cannot grow memory forever.
        if len(scan_jobs) > 20:
            for old_id in list(scan_jobs)[:-20]:
                scan_jobs.pop(old_id, None)
    try:
        scan_executor.submit(_run_scan_job, job_id, req.interval, req.limit_symbols, strategy_state['scalp_gate'] if strategy_state['mode']=='scalp' else strategy_state['normal_gate'], strategy_state['mode'])
    except Exception as e:
        scan_lock.release()
        with scan_jobs_lock:
            scan_jobs.pop(job_id, None)
        raise HTTPException(status_code=503, detail=f'Не удалось запустить скан: {e}')
    return {'ok': True, 'job_id': job_id, 'status': 'running'}

@app.get('/api/scan/{job_id}')
def scan_status(job_id: str):
    with scan_jobs_lock:
        job = scan_jobs.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={'ok': False, 'job_id': job_id, 'status': 'not_found', 'error': 'Результат скана не найден.'})
    if job['status'] == 'error':
        # Keep polling JSON-safe: do not raise HTTPException here because some proxies/render
        # error paths can replace it with an HTML 500 page.
        return {'ok': False, 'job_id': job_id, 'status': 'error', 'error': job.get('error', 'scan failed'), 'error_type': job.get('error_type', 'RuntimeError')}
    if job['status'] == 'running':
        return {'ok': True, 'job_id': job_id, 'status': 'running'}

    result = job.get('result') or {}
    if not isinstance(result, dict):
        return {'ok': False, 'job_id': job_id, 'status': 'error', 'error': 'Скан вернул некорректный результат.', 'error_type': 'InvalidScanResult'}
    # Keep a stable response contract for the browser even if a future engine version omits a field.
    result.setdefault('failures', [])
    result.setdefault('results', [])
    result.setdefault('universe_size', 0)
    result.setdefault('technical_checked', 0)
    result.setdefault('technical_target', result.get('technical_checked', 0))
    result.setdefault('technical_skipped', max(0, result.get('technical_target', 0) - result.get('technical_checked', 0)))
    result.setdefault('deep_checked', 0)
    result.setdefault('micro_checked', 0)
    result.setdefault('micro_target', result.get('micro_checked', 0))
    return {'ok': True, 'job_id': job_id, 'status': 'done', **result}

@app.get('/api/paper')
def paper():
    return demo_state() if MODE == 'demo' else paper_state()

@app.get('/api/account')
def account():
    from engine import demo_account_state
    return demo_account_state()

@app.get('/api/learning')
def learning():
    try:
        if MODE in ('demo','live'):
            sync_closed_pnl(MODE, __import__('engine')._demo_closed_pnl(100))
        return {'ok': True, **build_learning_report(1000)}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'learning_enabled': True}

@app.get('/api/journal')
def journal():
    try:
        if MODE in ('demo','live'):
            sync_closed_pnl(MODE, __import__('engine')._demo_closed_pnl(100))
        return {'ok': True, 'mode': MODE, 'trades': journal_recent(100)}
    except Exception as e:
        return {'ok': False, 'mode': MODE, 'trades': [], 'error': str(e)}

@app.post('/api/trade/open')
def trade_open(req: PaperOpenRequest):
    try:
        if MODE == 'demo':
            return demo_open(req.model_dump())
        return paper_open(req.model_dump())
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get('/api/trades/monitor')
def trades_monitor():
    try:
        return trade_monitor_snapshot()
    except Exception as e:
        return {'ok': False, 'error': str(e), 'positions': []}

@app.post('/api/trade/close')
def trade_close(req: dict):
    try:
        if MODE in ('demo','live'):
            return close_position(req.get('symbol'))
        raise ValueError('Exchange position closing is unavailable in paper mode')
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post('/api/paper/open')
def open_paper(req: PaperOpenRequest):
    try: return paper_open(req.model_dump())
    except ValueError as e: raise HTTPException(status_code=400, detail=str(e))

@app.post('/api/paper/update')
def update_paper(): return demo_state() if MODE == 'demo' else paper_mark_to_market()

@app.post('/api/paper/reset')
def reset_paper():
    if MODE == 'demo':
        raise HTTPException(status_code=409, detail='Сброс PAPER недоступен в DEMO режиме')
    return paper_reset()

class BudgetRequest(BaseModel):
    amount: float = Field(gt=0)

@app.post('/api/paper/budget')
def budget(req: BudgetRequest):
    try: return set_paper_budget(req.amount)
    except ValueError as e: raise HTTPException(status_code=400, detail=str(e))
