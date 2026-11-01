"""Strategibibliotek. Alla strategier är long-only (Alpaca krypto är spot) och körs på 1-minutsstaplar.

Varje strategi tar (symbol, bars, params) och returnerar samma struktur som
strategy.diagnose: villkor (checks), mätvärden (metrics) och eventuellt en Signal.
Motorn lägger själv till gemensamma villkor (färsk data, spread, ej nyss såld).

Källor till reglerna (sammanfattade, anpassade till minutdata):
- RSI(2)-återgång: Larry Connors & Cesar Alvarez – köp när RSI(2) < 10 i en upptrend
  (pris över långt glidande medel), sälj när priset går tillbaka över SMA(5).
- VWAP-återtagning: pris som legat under VWAP tar sig tillbaka över både VWAP och EMA(9)
  med ökad volym; stopp under VWAP/återtagningsstapelns botten.
- EMA-pullback: EMA 9 > 21 > 50 (upptrend), priset rekylerar ner till EMA 21 och vänder upp.
- Squeeze-utbrott (TTM Squeeze): Bollingerbanden har legat innanför Keltnerkanalen
  (låg volatilitet) och priset stänger över övre Bollingerbandet när de släpper.
"""

from __future__ import annotations

from math import sqrt
from statistics import fmean as mean  # fmean: snabb flyttalsvariant
from typing import Any, Callable

from .strategy import BASE_BARS, Bar, Signal, atr
from .strategy import diagnose as momentum_diagnose


# ------------------------------------------------------------------ indikatorer
def ema_series(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: list[float], n: int) -> float:
    return mean(values[-n:])


def rsi(closes: list[float], n: int) -> float:
    """Wilders RSI."""
    if len(closes) < n + 1:
        return 50.0
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = mean(gains[:n]), mean(losses[:n])
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def vwap(bars: list[Bar], n: int) -> float:
    window = bars[-n:]
    vol = sum(b.v for b in window)
    if vol <= 0:
        return mean((b.h + b.l + b.c) / 3 for b in window)
    return sum((b.h + b.l + b.c) / 3 * b.v for b in window) / vol


def _result(symbol: str, checks: dict[str, bool], metrics: dict, signal: Signal | None) -> dict:
    return {"symbol": symbol, "checks": checks, "metrics": metrics, "signal": signal,
            "passed": sum(checks.values()), "total": len(checks)}


def _short(symbol: str, have: int, need: int) -> dict:
    return {"symbol": symbol, "checks": {}, "metrics": {}, "signal": None,
            "note": f"för lite data ({have}/{need} minutstaplar)"}


# ------------------------------------------------------------------ strategier
def rsi2_reversion(symbol: str, bars: list[Bar], p: dict[str, Any]) -> dict:
    trend_len = int(p.get("trend_len", 200))
    need = trend_len + 5
    if len(bars) < need:
        return _short(symbol, len(bars), need)
    closes = [b.c for b in bars]
    last = bars[-1]
    trend = sma(closes, trend_len)
    r = rsi(closes[-60:], 2)
    sma5 = sma(closes, 5)
    a = atr(bars, 14)
    checks = {
        "uptrend": last.c > trend,
        "rsi_low": r < p.get("rsi_entry", 10),
        "below_sma5": last.c < sma5,
    }
    metrics = {"price": last.c, "rsi2": r, "trend": trend, "sma5": sma5, "atr_pct": a / last.c * 100}
    sig = None
    if all(checks.values()) and a > 0:
        stop = last.c - a * p.get("atr_stop_mult", 2.0)
        target = max(sma5, last.c + a * 0.5)  # Connors: sälj när priset är tillbaka över SMA(5)
        strength = min(1.0, (p.get("rsi_entry", 10) - r) / max(p.get("rsi_entry", 10), 1) + 0.3)
        sig = Signal(symbol, last.c, stop, target, a, round(strength, 4),
                     (f"RSI(2) {r:.1f}", f"över trend ({trend_len} min)", "mål SMA(5)"), strategy="rsi2")
    return _result(symbol, checks, metrics, sig)


def vwap_reclaim(symbol: str, bars: list[Bar], p: dict[str, Any]) -> dict:
    n = int(p.get("vwap_len", 120))
    look = int(p.get("lookback", 10))
    need = n + look + 1
    if len(bars) < need:
        return _short(symbol, len(bars), need)
    closes = [b.c for b in bars]
    last = bars[-1]
    vw_now = vwap(bars, n)
    was_below = any(b.c < vwap(bars[: len(bars) - k], n) * (1 - p.get("below_pct", 0.05) / 100)
                    for k, b in enumerate(reversed(bars[-look - 1:-1]), start=1))
    ema9 = ema_series(closes[-60:], 9)[-1]
    base_vol = mean(b.v for b in bars[-21:-1]) or 0
    vol_ratio = last.v / base_vol if base_vol > 0 else 0.0
    a = atr(bars, 14)
    checks = {
        "was_below": was_below,
        "above_vwap": last.c > vw_now,
        "above_ema9": last.c > ema9,
        "green_bar": last.c > last.o,
        "volume": vol_ratio >= p.get("volume_mult", 1.2),
    }
    metrics = {"price": last.c, "vwap": vw_now, "ema9": ema9, "vol_ratio": vol_ratio, "atr_pct": a / last.c * 100}
    sig = None
    if all(checks.values()) and a > 0:
        stop = min(vw_now * (1 - p.get("stop_below_vwap_pct", 0.15) / 100), last.l)
        risk = last.c - stop
        if risk > 0:
            sig = Signal(symbol, last.c, stop, last.c + risk * p.get("reward_risk", 2.0), a,
                         round(min(1.0, vol_ratio / 3), 4),
                         (f"tillbaka över VWAP {vw_now:.4g}", f"vol x{vol_ratio:.1f}"), strategy="vwap")
    return _result(symbol, checks, metrics, sig)


def ema_pullback(symbol: str, bars: list[Bar], p: dict[str, Any]) -> dict:
    need = 70
    if len(bars) < need:
        return _short(symbol, len(bars), need)
    closes = [b.c for b in bars]
    last = bars[-1]
    e9, e21, e50 = (ema_series(closes[-200:], n) for n in (9, 21, 50))
    look = int(p.get("pullback_bars", 5))
    touch = p.get("touch_pct", 0.05) / 100
    touched = any(b.l <= e21[-k] * (1 + touch) for k, b in enumerate(reversed(bars[-look:]), start=1))
    a = atr(bars, 14)
    checks = {
        "trend_stack": e9[-1] > e21[-1] > e50[-1],
        "trend_rising": e50[-1] > e50[-11],
        "pulled_back": touched,
        "back_above_ema9": last.c > e9[-1],
        "green_bar": last.c > last.o,
    }
    metrics = {"price": last.c, "ema9": e9[-1], "ema21": e21[-1], "ema50": e50[-1], "atr_pct": a / last.c * 100}
    sig = None
    if all(checks.values()) and a > 0:
        swing_low = min(b.l for b in bars[-look:])
        stop = min(swing_low, e21[-1]) - a * 0.1
        risk = last.c - stop
        if risk > 0:
            spread_stack = (e9[-1] - e50[-1]) / last.c * 100
            sig = Signal(symbol, last.c, stop, last.c + risk * p.get("reward_risk", 2.0), a,
                         round(min(1.0, 0.3 + spread_stack), 4),
                         ("EMA 9>21>50", "rekyl till EMA 21", "vänt upp"), strategy="ema")
    return _result(symbol, checks, metrics, sig)


def squeeze_breakout(symbol: str, bars: list[Bar], p: dict[str, Any]) -> dict:
    n = int(p.get("length", 20))
    look = int(p.get("squeeze_lookback", 6))
    need = n + look + 15
    if len(bars) < need:
        return _short(symbol, len(bars), need)

    def bands(upto: int):
        window = bars[:upto]
        closes = [b.c for b in window[-n:]]
        mid = mean(closes)
        sd = sqrt(mean([(c - mid) ** 2 for c in closes]))
        a = atr(window, n)
        bb = (mid - p.get("bb_mult", 2.0) * sd, mid + p.get("bb_mult", 2.0) * sd)
        kc = (mid - p.get("kc_mult", 1.5) * a, mid + p.get("kc_mult", 1.5) * a)
        return mid, bb, kc, a

    squeezed_before = False
    for k in range(2, look + 2):
        _, bb, kc, _ = bands(len(bars) - k + 1)
        if bb[0] > kc[0] and bb[1] < kc[1]:
            squeezed_before = True
            break
    mid, bb, kc, a = bands(len(bars))
    last, prev = bars[-1], bars[-2]
    released = not (bb[0] > kc[0] and bb[1] < kc[1])
    vol_ratio = last.v / (mean(b.v for b in bars[-21:-1]) or 1)
    checks = {
        "squeeze_before": squeezed_before,
        "released": released,
        "close_above_band": last.c > bb[1],
        "momentum_up": last.c > mid and last.c > prev.h,
        "volume": vol_ratio >= p.get("volume_mult", 1.0),
    }
    metrics = {"price": last.c, "bb_upper": bb[1], "kc_upper": kc[1], "mid": mid, "vol_ratio": vol_ratio,
               "atr_pct": a / last.c * 100 if last.c else 0}
    sig = None
    if all(checks.values()) and a > 0:
        stop = min(mid, last.c - a)
        risk = last.c - stop
        if risk > 0:
            sig = Signal(symbol, last.c, stop, last.c + risk * p.get("reward_risk", 2.0), a,
                         round(min(1.0, (last.c - bb[1]) / a + 0.3), 4),
                         ("squeeze släppt", "stängning över Bollinger", f"vol x{vol_ratio:.1f}"), strategy="squeeze")
    return _result(symbol, checks, metrics, sig)


def momentum(symbol: str, bars: list[Bar], p: dict[str, Any]) -> dict:
    d = momentum_diagnose(symbol, bars, p)
    sig = d.get("signal")
    if sig:  # gör styrkan jämförbar med de andra strategierna (0–1)
        d["signal"] = Signal(sig.symbol, sig.entry, sig.stop, sig.target, sig.atr, round(min(1.0, sig.score / 10), 4),
                             sig.reasons, strategy="momentum", rr=p.get("reward_risk", 2.0))
    return d


# ------------------------------------------------------------------ register
STRATEGIES: dict[str, dict[str, Any]] = {
    "momentum": {
        "label": "Momentum-utbrott", "fn": momentum, "params_key": None,
        "about": "Köper snabba rörelser uppåt med hög volym och stängning nära toppen (Stockbee momentum burst).",
        "checks": {"burst": "Rörelse uppåt", "not_overextended": "Inte för sent", "volume": "Volym",
                   "range_expansion": "Range", "close_near_high": "Stängning nära topp",
                   "volatile_enough": "Volatilitet", "green_bar": "Grön stapel"},
    },
    "rsi2": {
        "label": "RSI(2)-återgång", "fn": rsi2_reversion, "params_key": "rsi2",
        "about": "Connors: köper kraftiga dippar (RSI(2) under 10) när trenden är uppåt, säljer vid återgång till SMA(5).",
        "checks": {"uptrend": "Upptrend", "rsi_low": "RSI(2) låg", "below_sma5": "Under SMA(5)"},
    },
    "vwap": {
        "label": "VWAP-återtagning", "fn": vwap_reclaim, "params_key": "vwap",
        "about": "Köper när priset tar sig tillbaka över VWAP och EMA(9) med ökad volym efter att ha legat under.",
        "checks": {"was_below": "Låg under VWAP", "above_vwap": "Över VWAP", "above_ema9": "Över EMA(9)",
                   "green_bar": "Grön stapel", "volume": "Volym"},
    },
    "ema": {
        "label": "EMA-pullback", "fn": ema_pullback, "params_key": "ema",
        "about": "Köper rekyler till EMA(21) i en upptrend där EMA 9 > 21 > 50, när priset vänder upp igen.",
        "checks": {"trend_stack": "EMA 9>21>50", "trend_rising": "Stigande trend", "pulled_back": "Rekyl till EMA(21)",
                   "back_above_ema9": "Över EMA(9)", "green_bar": "Grön stapel"},
    },
    "squeeze": {
        "label": "Squeeze-utbrott", "fn": squeeze_breakout, "params_key": "squeeze",
        "about": "TTM Squeeze: köper när låg volatilitet (Bollinger inuti Keltner) släpper och priset stänger över bandet.",
        "checks": {"squeeze_before": "Squeeze innan", "released": "Squeeze släppt",
                   "close_above_band": "Över Bollinger", "momentum_up": "Momentum upp", "volume": "Volym"},
    },
}

COMMON_CHECKS = {"fresh": "Färsk data", "spread_ok": "Låg spread", "not_recent": "Ej nyss såld",
                 "costs_ok": "Mål täcker kostnad"}


def active(s: dict[str, Any]) -> list[str]:
    names = [n for n in s.get("strategies", ["momentum"]) if n in STRATEGIES]
    return names or ["momentum"]


def run(name: str, symbol: str, bars: list[Bar], s: dict[str, Any]) -> dict:
    meta = STRATEGIES[name]
    params = s if meta["params_key"] is None else {**s.get(meta["params_key"], {})}
    d = meta["fn"](symbol, bars, params)
    d["strategy"] = name
    return d


def check_label(name: str) -> str:
    for meta in STRATEGIES.values():
        if name in meta["checks"]:
            return meta["checks"][name]
    return COMMON_CHECKS.get(name, name)


def catalog() -> list[dict]:
    return [{"key": k, "label": v["label"], "about": v["about"]} for k, v in STRATEGIES.items()]


Fn = Callable[[str, list[Bar], dict[str, Any]], dict]
__all__ = ["STRATEGIES", "active", "run", "catalog", "check_label", "BASE_BARS"]
