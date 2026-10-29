"""Läser config.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.toml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else DEFAULT_PATH
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    for section in ("account", "safety", "strategy", "sizing", "runtime"):
        cfg.setdefault(section, {})
    mode = cfg["account"].get("mode", "paper")
    if mode not in ("paper", "live"):
        raise ValueError(f"account.mode måste vara 'paper' eller 'live', fick {mode!r}")
    if cfg["strategy"].get("asset_class") not in ("crypto", "stocks"):
        raise ValueError("strategy.asset_class måste vara 'crypto' eller 'stocks'")
    pct = cfg["safety"].get("kill_switch_drawdown_pct", 25.0)
    if not 0 < pct < 100:
        raise ValueError("safety.kill_switch_drawdown_pct måste ligga mellan 0 och 100")
    return cfg


def profiles(cfg: dict[str, Any]) -> dict[str, str]:
    out = {"normal": "Normal"}
    for name, prof in cfg.get("profiles", {}).items():
        out[name] = prof.get("label", name)
    return out


def apply_profile(cfg: dict[str, Any], name: str | None) -> dict[str, Any]:
    """Returnerar en kopia av cfg där profilens värden skriver över grundvärdena."""
    import copy

    out = copy.deepcopy(cfg)
    if not name or name == "normal":
        return out
    prof = cfg.get("profiles", {}).get(name)
    if prof is None:
        raise ValueError(f"Okänd profil: {name}")
    for section, values in prof.items():
        if isinstance(values, dict):
            out.setdefault(section, {}).update(values)
    out["active_profile"] = name
    return out


def symbols(cfg: dict[str, Any]) -> list[str]:
    s = cfg["strategy"]
    return list(s["crypto_symbols"] if s["asset_class"] == "crypto" else s["stock_symbols"])
