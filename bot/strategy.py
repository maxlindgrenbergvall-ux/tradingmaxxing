"""Intradag-momentum-burst.

Samma idé som skillen stockbee-momentum-burst-screener (prisrörelse +
volymexpansion + range-expansion + stängning nära toppen + filter mot att
jaga redan utsträckta rörelser), men på minutbarer i stället för dagsbarer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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


CHECK_LABELS = {
    "burst": "Rörelse uppåt",
    "not_overextended": "Inte för sent",
    "volume": "Volym",
    "range_expansion": "Range",
    "close_near_high": "Stängning nära topp",
    "volatile_enough": "Volatilitet",
    "green_bar": "Grön stapel",
    "fresh": "Färsk data",
}


def diagnose(symbol: str, bars: list[Bar], s: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Räknar ut alla villkor för en symbol – används både för köpbeslut och signalradarn."""
    burst = int(s.get("burst_bars", 5))
    period = int(s.get("atr_period", 14))
    need = max(period + 1, burst + BASE_BARS + 1)
    out: dict[str, Any] = {"symbol": symbol, "bars": len(bars), "signal": None, "checks": {}, "metrics": {}}
    if len(bars) < need:
        out["note"] = f"för lite data ({len(bars)}/{need} minutstaplar)"
        return out

    last = bars[-1]
    window = bars[-burst:]
    base = bars[-burst - BASE_BARS : -burst]
    ref_close = bars[-burst - 1].c
    if ref_close <= 0 or last.c <= 0:
        out["note"] = "ogiltigt pris"
        return out

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
    age_min = None
    if now is not None:
        try:
            age_min = (now - datetime.fromisoformat(last.t.replace("Z", "+00:00"))).total_seconds() / 60
            # Senaste minutstapeln måste vara ny – annars handlar ingen symbolen just nu.
            checks["fresh"] = age_min <= s.get("max_bar_age_minutes", 3)
        except ValueError:
            pass
    out.update(
        checks=checks,
        metrics={"price": last.c, "bar_time": last.t, "burst_pct": burst_pct, "vol_ratio": vol_ratio,
                 "range_ratio": range_ratio, "close_loc": close_loc, "atr_pct": atr_pct, "age_min": age_min},
        passed=sum(checks.values()),
        total=len(checks),
    )
    if not all(checks.values()) or a <= 0:
        return out

    stop_dist = a * s.get("atr_stop_mult", 1.5)
    score = max(burst_pct, 0.01) * max(min(vol_ratio, 5), 0.2) * max(min(range_ratio, 4), 0.2) * max(close_loc, 0.1)
    out["signal"] = Signal(
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
    return out


def evaluate(symbol: str, bars: list[Bar], s: dict[str, Any]) -> Signal | None:
    return diagnose(symbol, bars, s)["signal"]
