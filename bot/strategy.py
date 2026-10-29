"""Intradag-momentum-burst.

Samma idé som skillen stockbee-momentum-burst-screener (prisrörelse +
volymexpansion + range-expansion + stängning nära toppen + filter mot att
jaga redan utsträckta rörelser), men på minutbarer i stället för dagsbarer.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any


@dataclass(frozen=True)
class Bar:
    t: str
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    entry: float
    stop: float
    target: float
    atr: float
    score: float
    reasons: tuple[str, ...]


BASE_BARS = 20


def atr(bars: list[Bar], period: int) -> float:
    trs = []
    for prev, cur in zip(bars[-period - 1 : -1], bars[-period:]):
        trs.append(max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c)))
    return mean(trs) if trs else 0.0


def evaluate(symbol: str, bars: list[Bar], s: dict[str, Any]) -> Signal | None:
    burst = int(s.get("burst_bars", 5))
    period = int(s.get("atr_period", 14))
    if len(bars) < max(period + 1, burst + BASE_BARS + 1):
        return None

    last = bars[-1]
    window = bars[-burst:]
    base = bars[-burst - BASE_BARS : -burst]
    ref_close = bars[-burst - 1].c
    if ref_close <= 0 or last.c <= 0:
        return None

    burst_pct = (last.c / ref_close - 1) * 100
    base_vol = mean(b.v for b in base)
    vol_ratio = mean(b.v for b in window) / base_vol if base_vol > 0 else 0.0
    base_range = mean(b.h - b.l for b in base)
    bar_range = last.h - last.l
    range_ratio = bar_range / base_range if base_range > 0 else 0.0
    close_loc = (last.c - last.l) / bar_range if bar_range > 0 else 0.0
    a = atr(bars, period)
    atr_pct = a / last.c * 100

    checks = {
        "burst": burst_pct >= s.get("min_burst_pct", 0.6),
        "not_overextended": burst_pct <= s.get("max_burst_pct", 3.0),
        "volume": vol_ratio >= s.get("volume_mult", 1.8),
        "range_expansion": range_ratio >= s.get("range_expansion_mult", 1.3),
        "close_near_high": close_loc >= s.get("min_close_location", 0.65),
        "volatile_enough": atr_pct >= s.get("min_atr_pct", 0.15),
        "green_bar": last.c > last.o,
    }
    if not all(checks.values()) or a <= 0:
        return None

    stop_dist = a * s.get("atr_stop_mult", 1.5)
    score = burst_pct * min(vol_ratio, 5) * min(range_ratio, 4) * close_loc
    return Signal(
        symbol=symbol,
        entry=last.c,
        stop=last.c - stop_dist,
        target=last.c + stop_dist * s.get("reward_risk", 2.0),
        atr=a,
        score=round(score, 4),
        reasons=(
            f"burst {burst_pct:.2f}%",
            f"vol x{vol_ratio:.1f}",
            f"range x{range_ratio:.1f}",
            f"close-loc {close_loc:.2f}",
            f"ATR {atr_pct:.2f}%",
        ),
    )
