"""Portfolio capital/risk allocator for the autonomous trading loop.

The allocator never decides whether a trade is valid. It only distributes the
already-gated opportunities across a fixed bot capital and a fixed portfolio
risk budget. Risk is allocated by signal strength; capital allocation is
concentration-limited and leaves a reserve.
"""
from __future__ import annotations

import math
from typing import Iterable

TOTAL_RISK_PCT = 10.0
RESERVE_PCT = 10.0
MAX_SINGLE_CAPITAL_PCT = 80.0
MAX_NEW_ENTRIES = 3


def _score(x):
    try:
        return max(0.0, min(100.0, float(x.get("score", 0))))
    except Exception:
        return 0.0


def allocate(candidates: Iterable[dict], capital: float, total_risk_pct: float = TOTAL_RISK_PCT,
             max_new: int = MAX_NEW_ENTRIES, reserve_pct: float = RESERVE_PCT) -> list[dict]:
    """Return candidates with deterministic capital/risk budgets.

    The first three candidates are considered in a cycle. A single exceptional
    signal can receive up to 80% of working capital, but never 100%. With two or
    three signals the available 90% is split by score-weighted rank. The whole
    portfolio receives exactly the configured risk budget (10% by default),
    divided more heavily toward stronger signals.
    """
    rows = [dict(x) for x in candidates if _score(x) > 0]
    rows.sort(key=lambda x: (_score(x), float(x.get("expected_net_edge_pct") or 0)), reverse=True)
    rows = rows[:max(1, int(max_new))]
    if not rows or capital <= 0:
        return []

    n = len(rows)
    available_capital_pct = max(0.0, 100.0 - reserve_pct)
    if n == 1:
        capital_weights = [min(MAX_SINGLE_CAPITAL_PCT, available_capital_pct)]
    else:
        # Score-weighted capital distribution. The floor keeps lower-ranked
        # confirmed opportunities from being starved completely.
        raw = [max(0.20, (_score(x) / 100.0) ** 1.35) for x in rows]
        s = sum(raw)
        capital_weights = [available_capital_pct * w / s for w in raw]

    # Risk is more concentrated than capital. A strong setup gets more of the
    # fixed 10% portfolio loss budget, but the sum can never exceed it.
    raw_risk = [max(0.25, (_score(x) / 100.0) ** 2.0) for x in rows]
    sr = sum(raw_risk)
    risk_weights = [max(0.0, total_risk_pct) * w / sr for w in raw_risk]

    out = []
    for x, cap_pct, risk_pct in zip(rows, capital_weights, risk_weights):
        y = dict(x)
        y["allocation"] = {
            "capital_pct": round(cap_pct, 4),
            "capital_usdt": round(capital * cap_pct / 100.0, 6),
            "risk_pct_of_bot": round(risk_pct, 4),
            "risk_usdt": round(capital * risk_pct / 100.0, 6),
            "reserve_pct": round(reserve_pct, 4),
        }
        out.append(y)
    return out


def validate_allocations(items: Iterable[dict], total_risk_pct: float = TOTAL_RISK_PCT) -> dict:
    items = list(items)
    cap = sum(float(x.get("allocation", {}).get("capital_pct", 0)) for x in items)
    risk = sum(float(x.get("allocation", {}).get("risk_pct_of_bot", 0)) for x in items)
    return {
        "count": len(items),
        "capital_pct": round(cap, 6),
        "reserve_pct": round(max(0.0, 100.0 - cap), 6),
        "risk_pct": round(risk, 6),
        "risk_ok": risk <= float(total_risk_pct) + 1e-9,
        "concentration_ok": all(float(x.get("allocation", {}).get("capital_pct", 0)) <= MAX_SINGLE_CAPITAL_PCT + 1e-9 for x in items),
    }
