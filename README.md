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


## v5.4 changes
- Full active USDT-settled linear perpetual universe -> 24 technical -> 12 deep -> 6 micro.
- Up to 4 simultaneous paper positions when independent signals qualify.
- Dynamic margin allocation: if ideal position does not fit the trading budget, quantity is reduced to available margin instead of immediately rejecting the signal.
- Trading budget / reserved margin / available margin are exposed in paper state.
- Default paper budget remains $10; it can be changed through `/api/paper/budget` up to current balance.
- Demo-ready API layer uses `https://api-demo.bybit.com` when `BYBIT_MODE=demo`; API keys are read only from environment variables. No keys are stored in the repository.
- `/api/account` reports Demo wallet status when Demo mode is configured.
- Real Demo order execution is intentionally not enabled by default; PAPER remains the safe default.


## Demo mode
- `BYBIT_MODE=demo` uses Bybit Demo Trading at `https://api-demo.bybit.com`.
- Set `BYBIT_DEMO_API_KEY` and `BYBIT_DEMO_API_SECRET` only as Render environment secrets; never commit them.
- `AUTO_ENABLED=false` by default in Demo. Enable AUTO only after `/api/account` shows `configured=true`.
- Demo trading budget defaults to $100, daily loss limit $20, risk 2%, leverage 10x, max 4 positions.
- Demo orders are real orders inside Bybit Demo, with server-side TP/SL.
- Public market data remains from Bybit mainnet public streams/endpoints as specified by Bybit Demo documentation.
- Demo API keys are separate from Testnet keys.
