"""Broker-kopplingar: Alpaca (paper/live) och en offline-simulator."""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .strategy import Bar

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
TIMEOUT = 15


def norm(symbol: str) -> str:
    """Alpaca returnerar kryptopositioner som 'BTCUSD' men ordrar/data använder 'BTC/USD'."""
    return symbol.replace("/", "").upper()


class Broker(Protocol):
    def account(self) -> dict: ...
    def positions(self) -> dict[str, dict]: ...
    def bars(self, symbols: list[str], timeframe: str, limit: int) -> dict[str, list[Bar]]: ...
    def market_open(self) -> bool: ...
    def buy(self, symbol: str, qty: float) -> str: ...
    def close(self, symbol: str) -> None: ...
    def flatten(self) -> None: ...


class AlpacaBroker:
    def __init__(self, asset_class: str, mode: str = "paper"):
        import requests

        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("Sätt ALPACA_API_KEY och ALPACA_SECRET_KEY (paper-nycklar).")
        self.base = PAPER_URL if mode == "paper" else LIVE_URL
        self.mode = mode
        self.crypto = asset_class == "crypto"
        self.http = requests.Session()
        self.http.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _req(self, method: str, url: str, **kw):
        r = self.http.request(method, url, timeout=TIMEOUT, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca {method} {url} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def account(self) -> dict:
        a = self._req("GET", f"{self.base}/v2/account")
        if a.get("trading_blocked") or a.get("account_blocked"):
            raise RuntimeError("Alpaca-kontot är blockerat för handel.")
        return {"equity": float(a["equity"]), "buying_power": float(a["buying_power"])}

    def positions(self) -> dict[str, dict]:
        out = {}
        for p in self._req("GET", f"{self.base}/v2/positions"):
            out[norm(p["symbol"])] = {
                "qty": float(p["qty"]),
                "avg_entry_price": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
            }
        return out

    def bars(self, symbols: list[str], timeframe: str, limit: int) -> dict[str, list[Bar]]:
        start = (datetime.now(timezone.utc) - timedelta(minutes=limit * 3 + 30)).isoformat()
        if self.crypto:
            url = f"{DATA_URL}/v1beta3/crypto/us/bars"
            params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start}
        else:
            url = f"{DATA_URL}/v2/stocks/bars"
            params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "feed": "iex"}
        params["limit"] = 10000
        out: dict[str, list[Bar]] = {s: [] for s in symbols}
        while True:
            data = self._req("GET", url, params=params)
            for sym, rows in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(
                    Bar(r["t"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in rows
                )
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return {s: b[-limit:] for s, b in out.items()}

    def market_open(self) -> bool:
        if self.crypto:
            return True
        return bool(self._req("GET", f"{self.base}/v2/clock")["is_open"])

    def buy(self, symbol: str, qty: float) -> str:
        order = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc" if self.crypto else "day",
        }
        return self._req("POST", f"{self.base}/v2/orders", json=order)["id"]

    def close(self, symbol: str) -> None:
        self._req("DELETE", f"{self.base}/v2/positions/{norm(symbol)}")

    def flatten(self) -> None:
        self._req("DELETE", f"{self.base}/v2/orders")
        self._req("DELETE", f"{self.base}/v2/positions", params={"cancel_orders": "true"})


class SimBroker:
    """Offline-simulator med slumpmässiga prisserier och volatilitetsutbrott.
    Används för `python -m bot sim` och tester - inga riktiga pengar eller API."""

    def __init__(self, symbols: list[str], cash: float = 100_000, seed: int | None = 1, drift: float = 0.0):
        self.rng = random.Random(seed)
        self.cash = cash
        self.drift = drift
        self.pos: dict[str, dict] = {}
        self.series: dict[str, list[Bar]] = {}
        self.minute = 0
        for s in symbols:
            self.series[s] = []
            price = self.rng.uniform(20, 300)
            for _ in range(80):
                price = self._step(s, price)

    def _step(self, symbol: str, price: float) -> float:
        burst = self.rng.random() < 0.03
        sigma = 0.012 if burst else 0.002
        o = price
        c = max(0.01, o * (1 + self.rng.gauss(self.drift + (0.004 if burst else 0), sigma)))
        h = max(o, c) * (1 + abs(self.rng.gauss(0, sigma / 3)))
        l = min(o, c) * (1 - abs(self.rng.gauss(0, sigma / 3)))
        v = self.rng.uniform(800, 1200) * (4 if burst else 1)
        self.series[symbol].append(Bar(str(self.minute), o, h, l, c, v))
        return c

    def tick(self) -> None:
        self.minute += 1
        for s, bars in self.series.items():
            self._step(s, bars[-1].c)

    def price(self, symbol: str) -> float:
        for s, bars in self.series.items():
            if norm(s) == norm(symbol):
                return bars[-1].c
        raise KeyError(symbol)

    def shock(self, pct: float) -> None:
        """Simulera en krasch: flytta alla priser pct procent."""
        for s, bars in self.series.items():
            b = bars[-1]
            c = b.c * (1 + pct / 100)
            bars.append(Bar(b.t + "s", b.c, max(b.c, c), min(b.c, c), c, b.v))

    def account(self) -> dict:
        equity = self.cash + sum(p["qty"] * self.price(s) for s, p in self.pos.items())
        return {"equity": equity, "buying_power": self.cash}

    def positions(self) -> dict[str, dict]:
        return {
            s: {**p, "current_price": self.price(s)} for s, p in self.pos.items()
        }

    def bars(self, symbols: list[str], timeframe: str, limit: int) -> dict[str, list[Bar]]:
        return {s: self.series[s][-limit:] for s in symbols}

    def market_open(self) -> bool:
        return True

    def buy(self, symbol: str, qty: float) -> str:
        px = self.price(symbol)
        self.cash -= px * qty
        key = norm(symbol)
        p = self.pos.get(key)
        if p:
            total = p["qty"] + qty
            p["avg_entry_price"] = (p["avg_entry_price"] * p["qty"] + px * qty) / total
            p["qty"] = total
        else:
            self.pos[key] = {"qty": qty, "avg_entry_price": px}
        return f"sim-{self.minute}-{key}"

    def close(self, symbol: str) -> None:
        key = norm(symbol)
        p = self.pos.pop(key, None)
        if p:
            self.cash += p["qty"] * self.price(key)

    def flatten(self) -> None:
        for s in list(self.pos):
            self.close(s)
