from fastapi import FastAPI, HTTPException
import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
import os
import time
from contextlib import asynccontextmanager
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import market_snapshot, scan_market, paper_state, paper_open, paper_reset, paper_mark_to_market, set_paper_budget, MODE, demo_state, demo_open

AUTO_INTERVAL_SEC = 180
SCAN_CANDIDATES = 24
auto_lock = threading.RLock()
scan_lock = threading.Lock()
scan_jobs = {}
scan_jobs_lock = threading.Lock()
scan_executor = ThreadPoolExecutor(max_workers=1)
auto_state = {'enabled': os.getenv('AUTO_ENABLED','false' if MODE=='demo' else 'true').lower() == 'true', 'last_run': 0.0, 'last_scan': None, 'last_action': 'starting', 'error': None, 'last_success': 0.0}

@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(_auto_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title='Bybit AI Agent Web', version='5.6.3', lifespan=lifespan)
app.mount('/static', StaticFiles(directory='static'), name='static')

def _auto_iteration():
    with auto_lock:
        if not auto_state['enabled']:
            return
    try:
        if MODE == 'demo':
            state = demo_state()
            if not state.get('configured'):
                action = 'Demo API not configured'
                result = None
            elif state.get('error'):
                action = 'Demo sync error'
                result = None
            elif len(state.get('positions', [])) >= 4:
                action = 'max 4 demo positions — monitoring'
                result = None
            else:
                result = None
                if not scan_lock.acquire(blocking=False):
                    with auto_lock:
                        auto_state['last_run'] = time.time()
                        auto_state['last_action'] = 'scan already running — skipped this cycle'
                    return
                try:
                    result = scan_market('15', SCAN_CANDIDATES)
                finally:
                    scan_lock.release()
                candidates = [x for x in result.get('results', [])
                              if x.get('direction') in ('LONG', 'SHORT') and float(x.get('score', 0)) >= 70
                              and x.get('stop_loss') and x.get('take_profit')]
                candidates.sort(key=lambda x: float(x.get('score', 0)), reverse=True)
                opened=[]; rejected=[]
                for x in candidates[:4]:
                    try:
                        demo_open({'symbol': x['symbol'], 'side': x['direction'], 'entry': x['price'],
                                   'stop_loss': x['stop_loss'], 'take_profit': x['take_profit'], 'risk_pct': 2.0})
                        opened.append(f"{x['direction']} {x['symbol']} {x['score']}/100")
                    except (ValueError, RuntimeError) as e:
                        rejected.append(f"{x['symbol']}: {e}")
                action = ('AUTO DEMO: ' + ', '.join(opened)) if opened else ('scan complete — no demo entry' + (f" • {rejected[0]}" if rejected else ''))
        else:
            paper_mark_to_market()
            state = paper_state()
            if state.get('risk_locked'):
                action = 'risk lock active — no new paper entry'
                result = None
            elif len(state.get('open', [])) >= 4:
                action = 'max 4 paper positions — monitoring'
                result = None
            else:
                if not scan_lock.acquire(blocking=False):
                    with auto_lock:
                        auto_state['last_run'] = time.time()
                        auto_state['last_action'] = 'scan already running — skipped this cycle'
                    return
                try:
                    result = scan_market('15', SCAN_CANDIDATES)
                finally:
                    scan_lock.release()
                candidates = [x for x in result.get('results', [])
                              if x.get('direction') in ('LONG', 'SHORT') and float(x.get('score', 0)) >= 70
                              and x.get('stop_loss') and x.get('take_profit')]
                candidates.sort(key=lambda x: float(x.get('score', 0)), reverse=True)
                opened=[]; rejected=[]
                for x in candidates[:4]:
                    try:
                        paper_open({'symbol': x['symbol'], 'side': x['direction'], 'entry': x['price'],
                                    'stop_loss': x['stop_loss'], 'take_profit': x['take_profit'], 'risk_pct': 2.0})
                        opened.append(f"{x['direction']} {x['symbol']} {x['score']}/100")
                    except ValueError as e:
                        rejected.append(f"{x['symbol']}: {e}")
                action = ('AUTO PAPER: ' + ', '.join(opened)) if opened else ('scan complete — no entry' + (f" • {rejected[0]}" if rejected else ''))
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
    limit_symbols: int = Field(SCAN_CANDIDATES, ge=3, le=SCAN_CANDIDATES)

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
def health(): return {'ok': True, 'service': 'bybit-ai-agent-web', 'version': '5.6.3', 'mode': ('demo' if __import__('engine').MODE == 'demo' else 'paper-only'), 'auto_scanner': auto_state['enabled']}

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

@app.get('/api/markets')
def markets():
    try: return {'ok': True, 'markets': market_snapshot(20)}
    except Exception as e: raise HTTPException(status_code=502, detail=str(e))

def _run_scan_job(job_id, interval, limit_symbols):
    try:
        result = scan_market(interval, limit_symbols)
        with scan_jobs_lock:
            scan_jobs[job_id] = {'status': 'done', 'result': result}
    except Exception as e:
        with scan_jobs_lock:
            scan_jobs[job_id] = {'status': 'error', 'error': str(e)}
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
        scan_executor.submit(_run_scan_job, job_id, req.interval, req.limit_symbols)
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
        raise HTTPException(status_code=404, detail='Результат скана не найден.')
    if job['status'] == 'error':
        raise HTTPException(status_code=502, detail=job.get('error', 'scan failed'))
    if job['status'] == 'running':
        return {'ok': True, 'job_id': job_id, 'status': 'running'}
    return {'ok': True, 'job_id': job_id, 'status': 'done', **job['result']}

@app.get('/api/paper')
def paper():
    return demo_state() if MODE == 'demo' else paper_state()

@app.get('/api/account')
def account():
    from engine import demo_account_state
    return demo_account_state()

@app.post('/api/trade/open')
def trade_open(req: PaperOpenRequest):
    try:
        if MODE == 'demo':
            return demo_open(req.model_dump())
        return paper_open(req.model_dump())
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post('/api/paper/open')
def open_paper(req: PaperOpenRequest):
    try: return paper_open(req.model_dump())
    except ValueError as e: raise HTTPException(status_code=400, detail=str(e))

@app.post('/api/paper/update')
def update_paper(): return demo_state() if MODE == 'demo' else paper_mark_to_market()

@app.post('/api/paper/reset')
def reset_paper(): return paper_reset()

class BudgetRequest(BaseModel):
    amount: float = Field(gt=0)

@app.post('/api/paper/budget')
def budget(req: BudgetRequest):
    try: return set_paper_budget(req.amount)
    except ValueError as e: raise HTTPException(status_code=400, detail=str(e))
