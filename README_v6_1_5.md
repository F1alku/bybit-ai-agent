# Bybit AI Agent v6.1.5 — Manual Risk & Capital Controls

## What changed
- Risk per trade is manually editable from the UI up to 50% (instead of being fixed at 5%).
- Daily loss limit in USDT is manually editable and is now actually used by the Demo AUTO lock. The previous bug compared daily loss against the hard-coded `$20` default in `demo_state()` even after the UI value was changed.
- Base bot capital, working capital, and available-capital cap are manually editable.
- Total open-risk %, max positions, leverage, AUTO interval, max margin fraction and profit-lock step are manually editable.
- Added manual controls for daily loss limit (%) and maximum consecutive losses for paper/risk logic.
- Manual NORMAL and SCALP score gates remain editable.
- Settings persist in the journal database and are reloaded on restart.
- AUTO diagnostics continue to show candidate/openable/opened/rejected counts and rejection reasons.

## Important
This build does not remove exchange-side restrictions. Bybit can still reject an order because of insufficient margin, symbol quantity/price limits, leverage limits, account/risk-limit restrictions, or other API rules. Bybit's Demo Trading simulates market conditions and supports API access; exchange-side constraints still apply.

## Verification
- `py_compile`: passed
- `pytest`: 41 passed
