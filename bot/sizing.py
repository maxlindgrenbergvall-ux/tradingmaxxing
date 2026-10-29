"""ATR-baserad positionsstorlek (samma formel som skillen position-sizer):
antal = konto * risk% / stoppavstånd, begränsat av max positionsstorlek och köpkraft."""

from __future__ import annotations

import math


def position_size(
    *,
    equity: float,
    buying_power: float,
    entry: float,
    stop: float,
    risk_pct: float,
    max_position_pct: float,
    fractional: bool,
) -> float:
    risk_per_unit = entry - stop
    if entry <= 0 or risk_per_unit <= 0 or equity <= 0:
        return 0.0
    qty = equity * risk_pct / 100 / risk_per_unit
    qty = min(qty, equity * max_position_pct / 100 / entry, max(buying_power, 0) * 0.98 / entry)
    if fractional:
        return math.floor(qty * 1e6) / 1e6
    return float(math.floor(qty))
