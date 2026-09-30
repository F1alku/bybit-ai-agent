# Bybit AI Agent v6.2.0 — Final Portfolio Brain + Adaptive Learning

Based on v6.1.25. Demo-first autonomous Bybit USDT Perpetual agent.

## v6.2.0 changes
- History API is now DB-first: `/api/journal` never calls Bybit.
- Learning lessons are read/backfilled from the durable journal without exchange calls from page requests.
- Background Closed PnL reconciliation remains the only exchange-to-history sync path.
- Learning reports expose total persisted lesson count (`lesson_total`) so UI does not show 0 after backfill has already completed.
- First page load no longer starts the expensive position monitor before positions are rendered.
- Position monitor is single-flight and only runs when open-position cards exist.
- Removed duplicate 30-second monitor request; `paper()` triggers monitor only when positions exist.
- UI bundles learning lessons into the journal response, removing a second learning HTTP request.
- The durable PostgreSQL journal remains available even when Bybit sync is temporarily unavailable.
- Trading logic, NORMAL/SCALP, AUTO, risk limits, hard SL, and Profit Ladder are unchanged.

## Verification
- Python compile check: passed.
- JavaScript syntax check with Node: passed.
- Full test suite: **58 passed**.


## v6.2.0 Learning reliability
- Learning backfill is background-only; page/API reads never trigger DB-backfill.
- Each historical row is isolated with a savepoint, so one malformed row cannot stop the remaining lessons.
- `/api/journal` reports durable `lesson_total`, `pending`, and Learning errors.
- `/api/learning` reads persisted lessons/counts and does not rebuild them during page load.


## v6.2.0 final portfolio policy
- Bot trading capital default/migration: $100.
- Portfolio stop-risk budget: 10% ($10 on $100), shared across all open positions.
- Capital allocator selects up to 3 new entries per cycle and distributes capital by signal strength; single-position concentration is capped at 80%, with a 10% reserve.
- Target leverage: 10x; exchange-specific limits may clamp it lower.
- Open positions are re-analysed continuously using multi-timeframe structure, EMA, RSI, momentum, volume, OI, order-book imbalance, trade delta, funding, spread, BTC context, news and optional liquidity intelligence.
- Closed trades feed a bounded adaptive learning adjustment (maximum +/-5 score points) after sufficient samples; risk limits and hard gates are never changed by learning.
- Profit Ladder, hard SL, adaptive exits, persistent history and background learning remain enabled.
