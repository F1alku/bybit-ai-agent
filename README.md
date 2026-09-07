# Bybit AI Agent Web v2.0

Mobile-first FastAPI app for Bybit Testnet market analysis and paper trading.

- Public Testnet market data through backend proxy
- Multi-timeframe analysis: 4H / 1H / selected setup / 5M
- Liquidity sweep, premium/discount, VWAP, volume and trend scoring
- LONG / SHORT / WAIT
- Paper balance $1,000
- Max 2 paper positions
- No real order endpoint and no API keys required for public market analysis
- `/api/markets` compatibility endpoint for simple clients

Render build: `pip install -r requirements.txt`
Render start: `gunicorn -k uvicorn.workers.UvicornWorker app:app`
