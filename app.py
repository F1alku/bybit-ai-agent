from fastapi import FastAPI, HTTPException
import asyncio
import threading
import time
from contextlib import asynccontextmanager
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import market_snapshot, scan_market, paper_state, paper_open, paper_reset, paper_mark_to_market

AUTO_INTERVAL_SEC = 180
auto_lock = threading.RLock()
auto_state = {'enabled': True, 'last_run': 0.0, 'last_scan': None, 'last_action': 'starting', 'error': None}

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

app = FastAPI(title='Bybit AI Agent Web', version='5.1.0', lifespan=lifespan)
app.mount('/static', StaticFiles(directory='static'), name='static')

def _auto_iteration():
    with auto_lock:
        if not auto_state['enabled']:
            return
    try:
        mark = paper_mark_to_market()
        state = paper_state()
        if state.get('risk_locked'):
            action = 'risk lock active — no new paper entry'
            result = None
        elif len(state.get('open', [])) >= 2:
            action = 'max 2 paper positions — monitoring'
            result = None
        else:
            result = scan_market('15', 8)
            candidates = [x for x in result.get('results', [])
                          if x.get('direction') in ('LONG', 'SHORT') and float(x.get('score', 0)) >= 70
                          and x.get('stop_loss') and x.get('take_profit')]
            candidates.sort(key=lambda x: float(x.get('score', 0)), reverse=True)
            action = 'scan complete — no entry'
            if candidates:
                x = candidates[0]
                try:
                    paper_open({'symbol': x['symbol'], 'side': x['direction'], 'entry': x['price'],
                                'stop_loss': x['stop_loss'], 'take_profit': x['take_profit'], 'risk_pct': 0.5})
                    action = f"AUTO PAPER {x['direction']} {x['symbol']} {x['score']}/100"
                except ValueError as e:
                    action = f"candidate rejected: {e}"
        with auto_lock:
            auto_state['last_run'] = time.time()
            auto_state['last_scan'] = result
            auto_state['last_action'] = action
            auto_state['error'] = None
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
    limit_symbols: int = Field(8, ge=3, le=12)

class PaperOpenRequest(BaseModel):
    symbol: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    risk_pct: float = Field(0.5, gt=0, le=5)

@app.get('/')
def index(): return FileResponse('static/index.html')

@app.get('/api/health')
def health(): return {'ok': True, 'service': 'bybit-ai-agent-web', 'version': '5.1.0', 'mode': 'paper-only', 'auto_scanner': auto_state['enabled']}

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

@app.post('/api/scan')
def scan(req: ScanRequest):
    try: return scan_market(req.interval, req.limit_symbols)
    except Exception as e: raise HTTPException(status_code=502, detail=str(e))

@app.get('/api/paper')
def paper(): return paper_state()

@app.post('/api/paper/open')
def open_paper(req: PaperOpenRequest):
    try: return paper_open(req.model_dump())
    except ValueError as e: raise HTTPException(status_code=400, detail=str(e))

@app.post('/api/paper/update')
def update_paper(): return paper_mark_to_market()

@app.post('/api/paper/reset')
def reset_paper(): return paper_reset()
