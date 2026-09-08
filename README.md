# Bybit AI Agent — v5.7.2 DEMO

Web-based Bybit market scanner/trader for Paper and Bybit Demo Trading.

## Scanner
- Entire active USDT-settled linear perpetual universe is discovered dynamically.
- Fast market-wide pass -> up to 24 technical candidates -> 12 deep -> 6 micro.
- Multi-timeframe: 4H + 1H + setup timeframe + 5M microstructure.
- OI, order-book imbalance, trade delta, funding, spread and BTC regime are included where available.
- Entry logic v5.8: separate NORMAL/SCALP thresholds, 1D/4H/1H regime, structure/VWAP/momentum/flow gates, R:R >= 1.5, estimated net edge after fee/spread/slippage, exact blocker reasons, and signal quality A/A+. SCALP uses 5M setup and a configurable time-stop hint.
- No averaging down and no martingale.

## Demo risk model
- Bybit Demo base: `https://api-demo.bybit.com`.
- Default Demo trading budget: **$100**.
- Risk per trade: **2%**.
- Leverage: **10x**.
- Maximum simultaneous positions: **4**.
- Daily loss limit: **$20**.
- AUTO is **OFF by default** in Demo.
- Orders use server-side TP/SL.

## Demo account display
The UI deliberately separates the Bybit account totals from the bot's budget:
- USDT wallet balance
- Bybit total equity
- Bybit available margin
- Bot trading budget ($100 by default)
- Budget currently available to the bot
- Margin reserved by open positions
- Open/unrealized P&L
- Realized P&L today and over the latest 7-day Bybit closed-PnL window
- Daily P&L and daily loss limit

`totalEquity` is an account-wide USD value across assets, so it must not be confused with the bot's $100 trading budget.

## Demo API keys
Set these only as Render environment variables:
- `BYBIT_MODE=demo`
- `BYBIT_DEMO_API_KEY`
- `BYBIT_DEMO_API_SECRET`
- `AUTO_ENABLED=false`
- `DEMO_TRADING_BUDGET=100`
- `DEMO_MAX_DAILY_LOSS=20`
- `DEMO_RISK_PCT=2`

No API secrets belong in the repository.

## API
- `GET /`
- `GET /api/health`
- `GET /api/markets`
- `POST /api/scan`
- `GET /api/auto`
- `POST /api/auto/toggle`
- `GET /api/paper`
- `GET /api/account`
- `POST /api/trade/open`
- `POST /api/paper/update`
- `POST /api/paper/reset`
- `POST /api/paper/budget`

## Safety sequence
1. Deploy with Demo mode and AUTO OFF.
2. Verify the account page and the separated $100 bot budget.
3. Run a controlled manual Demo trade with valid SL/TP.
4. Confirm position, uPnL and closed-PnL reporting.
5. Only then consider enabling AUTO.


## Production-ready modes
- `BYBIT_MODE=paper`: no exchange trading.
- `BYBIT_MODE=demo`: Bybit Demo Trading.
- `BYBIT_MODE=live`: Bybit mainnet API, but order placement is blocked unless `LIVE_TRADING_ARMED=true`.
- Live credentials use `BYBIT_LIVE_API_KEY` / `BYBIT_LIVE_API_SECRET`; Demo credentials remain separate.
- Manual Market close: `POST /api/trade/close` with `{"symbol":"BTCUSDT"}`.
- Live mode should be enabled only after Demo validation and an explicit production checklist.

## Persistent trading process and journal — v5.7.2
- Trading execution is separated from the web UI into `worker.py` / Render Background Worker.
- The web service can run with `RUN_TRADER_IN_WEB=false`, preventing duplicate trading loops.
- The worker is intended to remain running continuously; do not rely on a Free web service for 24/7 trading because Render Free web services spin down after 15 minutes without inbound traffic. See Render docs.
- `journal.py` stores authoritative Bybit Closed PnL records in Postgres when `DATABASE_URL` is configured. It falls back to SQLite for local testing.
- On startup the worker synchronizes recent Bybit Closed PnL before starting AUTO.
- `/api/journal` exposes the synchronized trade history to the UI.
- For production, use a Render Postgres database (or another durable external database). Do not rely on local SQLite on Render Free because its filesystem is ephemeral.
- Production topology: Web Service (UI/API) + Background Worker (AUTO trader) + durable Postgres database.

## Render database requirement
For persistent trade history, set DATABASE_URL to a Render Postgres database. If DATABASE_URL is absent, the app uses /tmp/bybit_agent.db only as a non-persistent fallback so the web service can still start; it is not suitable for production history.


## v5.8.2

Entry gate relaxed: 4H/1H opposite trend, structure confirmation, spread/volatility, BTC regime, R:R/net edge and configured score remain hard blockers. VWAP, momentum, delta, OI and premium/discount are score evidence rather than mandatory binary gates.
