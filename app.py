from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import market_snapshot, scan_market, paper_state, paper_open, paper_reset, paper_mark_to_market

app = FastAPI(title='Bybit AI Agent Web', version='2.0.0')
app.mount('/static', StaticFiles(directory='static'), name='static')

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
def index():
    return FileResponse('static/index.html')

@app.get('/api/health')
def health():
    return {'ok': True, 'service': 'bybit-ai-agent-web', 'version': '2.0.0', 'mode': 'paper-only'}

@app.get('/api/markets')
def markets():
    try:
        return {'ok': True, 'markets': market_snapshot(20)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.post('/api/scan')
def scan(req: ScanRequest):
    try:
        return scan_market(req.interval, req.limit_symbols)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get('/api/paper')
def paper():
    return paper_state()

@app.post('/api/paper/open')
def open_paper(req: PaperOpenRequest):
    try:
        return paper_open(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post('/api/paper/update')
def update_paper():
    return paper_mark_to_market()

@app.post('/api/paper/reset')
def reset_paper():
    return paper_reset()
