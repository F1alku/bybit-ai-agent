# Bybit AI Agent v6.1.26 — Performance + Learning

Based on v6.1.25. Demo-first autonomous Bybit USDT Perpetual agent.

## v6.1.26 changes
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
