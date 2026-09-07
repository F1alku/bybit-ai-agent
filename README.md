# Bybit AI Agent — AUTO v5.2

Paper-only Bybit Testnet market scanner. No real orders.

## Current model
- Entire active USDT-settled linear perpetual universe is discovered dynamically from Bybit.
- One cheap market-wide ticker pass ranks the full universe.
- Technical pass: up to 24 liquid candidates, using 4H + 1H + setup timeframe.
- Deep pass: top 8 candidates, adding 5M and full entry scoring.
- Microstructure pass: top 4 candidates, adding OI, order-book imbalance, trade delta, funding and spread.
- BTC is always included as a market-regime filter when available.
- Expensive calls are not made for every contract.
- Instrument metadata is cached for 10 minutes; market data uses short caching.

## Paper account
- Starting balance: $10
- Minimum leverage: 10x (paper simulation)
- Default aggressive risk: 2% of current balance per trade
- Maximum 2 open paper positions
- Mandatory SL/TP
- No averaging down
- No martingale
- Fees and slippage are simulated assumptions, not claims about actual Bybit fees.

## Auto scanner
- Runs every 3 minutes on Render.
- It can inspect the entire market without the browser being open.
- It only opens a paper trade when the existing entry gate is confirmed and score >= 70.
- The 70 score is a current engineering threshold, not a guarantee; it can be recalibrated from paper-trade statistics.

## API
- GET `/`
- GET `/api/health`
- GET `/api/markets`
- POST `/api/scan`
- GET `/api/auto`
- POST `/api/auto/toggle`
- GET `/api/paper`
- POST `/api/paper/open`
- POST `/api/paper/update`
- POST `/api/paper/reset`

## Run
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port $PORT
```
