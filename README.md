# Bybit AI Agent v6.2.2 — Final Portfolio Brain + Adaptive Learning

Based on v6.2.0 Portfolio Brain. Demo-first autonomous Bybit USDT Perpetual agent.

## v6.2.2 changes
- History API is now DB-first: `/api/journal` never calls Bybit.
- Learning lessons are read/backfilled from the durable journal without exchange calls from page requests.
- Background Closed PnL reconciliation remains the only exchange-to-history sync path.
- Learning reports expose total persisted lesson count (`lesson_total`) so UI does not show 0 after backfill has already completed.
- First page load no longer starts the expensive position monitor before positions are rendered.
- Position monitor is single-flight and only runs when open-position cards exist.
- Removed duplicate 30-second monitor request; `paper()` triggers monitor only when positions exist.
- UI bundles learning lessons into the journal response, removing a second learning HTTP request.
- The durable PostgreSQL journal remains available even when Bybit sync is temporarily unavailable.
- NORMAL/SCALP, AUTO, hard SL and the existing Profit Ladder remain enabled; portfolio allocation, per-trade risk and dynamic profit protection are extended.

## Verification
- Python compile check: passed.
- JavaScript syntax check with Node: passed.
- Targeted regression tests for History/Learning/Allocator/Profit Protection: **15 passed**. The full suite reached **66/66 PASSED**; the test process itself did not exit cleanly because the pre-existing scan ThreadPoolExecutor keeps a worker alive after the scan API test.


## v6.2.2 Learning reliability
- Learning backfill is background-only; page/API reads never trigger DB-backfill.
- Each historical row is isolated with a savepoint, so one malformed row cannot stop the remaining lessons.
- `/api/journal` reports durable `lesson_total`, `pending`, and Learning errors.
- `/api/learning` reads persisted lessons/counts and does not rebuild them during page load.


## v6.2.2 final portfolio policy
- Bot trading capital default/migration: $100.
- Per-trade risk: 10% of the $100 trading capital ($10 max planned loss per trade). Risk is NOT pooled across the portfolio; five independent trades may represent up to 50% aggregate stop-risk.
- Capital allocator selects up to 3 new entries per cycle and distributes capital by signal strength; single-position concentration is capped at 80%, with a 10% reserve.
- Target leverage: 10x; exchange-specific limits may clamp it lower.
- Open positions are re-analysed continuously using multi-timeframe structure, EMA, RSI, momentum, volume, OI, order-book imbalance, trade delta, funding, spread, BTC context, news and optional liquidity intelligence.
- Closed trades feed a bounded adaptive learning adjustment (maximum +/-5 score points) after sufficient samples; risk limits and hard gates are never changed by learning.
- Profit Ladder, hard SL, adaptive exits, persistent history and background learning remain enabled. After +50% P&L, peak-profit tracking protects the runner and can close it after a 15-point retracement from the peak.


## v6.2.2 changes
- Fixed PostgreSQL learning schema migration: `learning_trade_meta` is created reliably and legacy columns are migrated with isolated savepoints.
- History Sync is independent from Learning; a learning failure cannot erase or hide persisted Closed PnL.
- `/api/journal` always serves durable history even when Learning is unavailable, and exposes `learning_error`.
- Risk policy corrected: 10% is per trade, not a portfolio-wide 10% cap.
- Capital allocator distributes working capital across up to 3 new candidates by signal strength while leaving reserve; each selected trade can carry up to 10% risk.
- Added profit protection after +50%: track peak P&L and protect a trailing portion of realized gains so a +70% trade cannot freely collapse back to zero.


## 6.2.2 execution hardening
- Canonical Decimal quantity formatting for Bybit market orders.
- `qtyStep`/`minOrderQty`/`maxMktOrderQty` validation and market-order quantity cap.
- One safe retry after Bybit `10001 Qty invalid` with refreshed instrument metadata.
- Bybit `110126 agreement required` contracts are temporarily excluded from AUTO for 6 hours and do not block the next scan cycle.
- AUTO diagnostics expose temporarily unavailable symbols.
