

## v6.1.19 Liquidity Intelligence — Open Positions Only
- Optional Hyblock liquidation levels/heatmap context via HYBLOCK_API_KEY.
- Uses predicted liquidation levels; it does not claim access to individual users' private stop orders.
- Config: HYBLOCK_API_KEY, HYBLOCK_EXCHANGE (default binance_perp_stable), LIQUIDITY_ZONE_PCT, LIQUIDITY_MIN_ZONE_USD.
- Strong candidates are checked for nearby long/short liquidation clusters; entry can wait for a liquidity sweep confirmation.
- Status: GET /api/liquidity/status.

- Liquidity Intelligence is NOT used by the market scanner or entry gate.
- It is queried only for positions already open on Bybit and is used by Trade Monitor/Adaptive Exit.
- It never increases position size.
