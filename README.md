# v6.1.25 — Bybit Cursor Signing + Full History + Learning Backfill

- Fixed staged profit-taking for open positions only.
- Thresholds: +10/+20/+30/+40/+50% P&L relative to occupied margin.
- Each stage realizes 10% of the original position size.
- Exchange hard SL is progressively moved: breakeven, then approximately +10/+20/+30/+40% margin-P&L equivalent.
- Adaptive Exit and Liquidity Intelligence remain responsible for the remaining runner.
- No automatic re-entry is forced after a partial take-profit; a new entry requires a fresh setup.

# v6.1.21 — Learning Sync Fix

Fixes learning from Bybit Closed PnL: execId/orderId mismatches now use symbol/side/entry-price fallback. Closed trades without saved entry context still create outcome lessons instead of disappearing.

History remains PostgreSQL-backed and Liquidity Intelligence remains restricted to open positions only.



## v6.1.20 Unified History + Liquidity Intelligence — Open Positions Only
- Optional Hyblock liquidation levels/heatmap context via HYBLOCK_API_KEY.
- Uses predicted liquidation levels; it does not claim access to individual users' private stop orders.
- Config: HYBLOCK_API_KEY, HYBLOCK_EXCHANGE (default binance_perp_stable), LIQUIDITY_ZONE_PCT, LIQUIDITY_MIN_ZONE_USD.
- External liquidation/liquidity intelligence is used ONLY for already-open positions; it never gates new entries and never increases position size.
- Status: GET /api/liquidity/status.

- Liquidity Intelligence is NOT used by the market scanner or entry gate.
- It is queried only for positions already open on Bybit and is used by Trade Monitor/Adaptive Exit.
- It never increases position size.

- v6.1.20 fixes PostgreSQL psycopg row conversion in journal history and separates history-sync errors from learning errors.
- UI version labels are unified to 6.1.20.


## v6.1.25 changes

- Fixed Bybit private GET signing for paginated `cursor` values: cursor is fully normalized from repeated percent-encoding, encoded exactly once, and the exact signed query string is sent on the wire. This fixes Bybit 10004 `Error sign` caused by `%253A` / `%252C` double-encoding.
- Added regression tests for signed cursor pagination.
- 57 tests pass.
- Full Bybit Closed PnL pagination for reconciliation (up to 500 rows per sync).
- Existing closed trades are backfilled into `learning_lessons`; missing entry context never blocks a lesson.
- Learning reconciliation reports closed rows, matched context, unmatched context and match method.
- Order ID / orderLinkId aliases are resolved in both directions.
- History UI shows exchange count, DB count, learning count and context coverage.
- Keeps the v6.1.22 fixed profit ladder and hard-stop protection.
