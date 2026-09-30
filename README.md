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
