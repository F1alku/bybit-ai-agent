# Bybit AI Agent — v5.6.5 DEMO

Web-based Bybit market scanner/trader for Paper and Bybit Demo Trading.

## Scanner
- Entire active USDT-settled linear perpetual universe is discovered dynamically.
- Fast market-wide pass -> up to 24 technical candidates -> 12 deep -> 6 micro.
- Multi-timeframe: 4H + 1H + setup timeframe + 5M microstructure.
- OI, order-book imbalance, trade delta, funding, spread and BTC regime are included where available.
- Entry gate currently requires LONG/SHORT and score >= 70 plus SL/TP.
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
