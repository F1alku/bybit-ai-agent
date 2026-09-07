# Bybit AI Agent — Final Paper Quant

Mobile-first FastAPI scanner for Bybit Testnet USDT perpetuals.

## Safety
- Paper-only. No private API keys and no real order endpoint.
- Default risk: 0.5% per paper trade.
- Max 2 open paper positions.
- Daily loss lock: 2%.
- 3 consecutive losses lock entries.
- 30-minute cooldown after a loss.
- Paper fees/slippage are simulated assumptions, not Bybit fee guarantees.

## Strategy
4H/1H context → setup timeframe → liquidity sweep/structure → discount/premium → VWAP/volume → 5M momentum → RSI → OI → order-book imbalance → taker trade delta → funding → spread/volatility → BTC regime → risk gate.

The agent can return WAIT even when the score is high if the hard entry gate is not satisfied.

## Run
`pip install -r requirements.txt`
`uvicorn app:app --host 0.0.0.0 --port 8000`

## Render
Build: `pip install -r requirements.txt`
Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`

## Important
This is a rule-based quantitative scanner with microstructure inputs, not a trained ML model. Historical backtesting of the full microstructure layer requires archived OI/order-book/trade data; the current paper engine is the validation layer for live Testnet market data.


## Auto Scanner 5.1
- Автосканер включён по умолчанию в paper-only режиме.
- Каждые 3 минуты обновляет paper-позиции, запускает Deep Scan 15M и при сигнале LONG/SHORT >= 70/100 автоматически открывает лучшую допустимую paper-сделку.
- Максимум 2 одновременные позиции, риск 0.5%, действуют все защитные лимиты из paper engine.
- Автосканер можно выключить/включить кнопкой в веб-интерфейсе.
- Реальные ордера не используются.
- Состояние paper хранится в памяти процесса и может сброситься при перезапуске Render.
