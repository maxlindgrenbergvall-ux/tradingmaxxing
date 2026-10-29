"""Broker-kopplingar: Alpaca (paper/live) och en offline-simulator."""

from __future__ import annotations

import logging
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
    def quotes(self, symbols: list[str]) -> dict[str, dict]: ...
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
        import requests

        try:
            r = self.http.request(method, url, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            raise RuntimeError("Kunde inte nå Alpaca – kontrollera internetanslutningen.") from e
        if r.status_code in (401, 403):
            raise RuntimeError("Alpaca nekade nycklarna – kontrollera att du kopierat "
                               "Paper Trading-nycklar (inte Live) och att de är rätt.")
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
        try:
            return self._bars(symbols, timeframe, limit)
        except RuntimeError:
            if len(symbols) == 1:
                raise
        # En ogiltig symbol får inte stoppa resten: hämta en i taget och hoppa över fel.
        out: dict[str, list[Bar]] = {}
        for sym in symbols:
            try:
                out.update(self._bars([sym], timeframe, limit))
            except RuntimeError as e:
                logging.getLogger("bot").warning("Hoppar över %s: %s", sym, e)
        return out

    def _bars(self, symbols: list[str], timeframe: str, limit: int) -> dict[str, list[Bar]]:
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

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Senaste köp-/säljkurs (bid/ask) per symbol."""
        if self.crypto:
            url = f"{DATA_URL}/v1beta3/crypto/us/latest/quotes"
            params = {"symbols": ",".join(symbols)}
        else:
            url = f"{DATA_URL}/v2/stocks/quotes/latest"
            params = {"symbols": ",".join(symbols), "feed": "iex"}
        data = self._req("GET", url, params=params) or {}
        out = {}
        for sym, q in (data.get("quotes") or {}).items():
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            if bid > 0 and ask >= bid:
                out[sym] = {"bid": bid, "ask": ask}
        return out

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

    def __init__(self, symbols: list[str], cash: float = 100_000, seed: int | None = 1, drift: float = 0.0,
                 spread: float | None = None, fee_pct: float = 0.25, leverage: float = 1.0):
        self.rng = random.Random(seed)
        self.cash = cash
        self.drift = drift
        self.pos: dict[str, dict] = {}
        self.series: dict[str, list[Bar]] = {}
        # Spread i procent per symbol – som hos en riktig mäklare köper man till ask och säljer till bid.
        self.fee = fee_pct / 100
        # Hävstång: köpkraft = kontovärde x hävstång. Lånade pengar syns som negativ kassa.
        # Likvidation när kontovärdet understiger underhållsmarginalen (hälften av startmarginalen).
        self.leverage = max(1.0, float(leverage))
        self.maintenance = 0.5 / self.leverage
        self.liquidations = 0
        self.spread_pct = {s: (spread if spread is not None else self.rng.uniform(0.05, 0.4)) for s in symbols}
        self.minute = 0
        self.burst_left: dict[str, int] = {}
        self.burst_dir: dict[str, int] = {}
        # Simulerad klocka: varje tick = en minut. Historiken backdateras 80 minuter.
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        for s in symbols:
            self.series[s] = []
            price = self.rng.uniform(20, 300)
            for k in range(80, 0, -1):
                self._t = self.now - timedelta(minutes=k)
                price = self._step(s, price)

    def _step(self, symbol: str, price: float) -> float:
        # Utbrott pågår några minuter; ungefär hälften fortsätter, resten vänder.
        left = self.burst_left.get(symbol, 0)
        if left == 0 and self.rng.random() < 0.012:
            left = self.rng.randint(4, 9)
            self.burst_dir[symbol] = 1 if self.rng.random() < 0.5 else -1
        burst = left > 0
        self.burst_left[symbol] = max(0, left - 1)
        if burst:
            # Första minuterna går uppåt, sedan avgör burst_dir om rörelsen fortsätter.
            mu = 0.0035 if left > 3 else 0.003 * self.burst_dir.get(symbol, 1)
            sigma = 0.004
        else:
            mu, sigma = self.drift, 0.0015
        o = price
        c = max(0.01, o * (1 + self.rng.gauss(mu, sigma)))
        wick = sigma / 4 if not burst else 0.0008
        h = max(o, c) * (1 + abs(self.rng.gauss(0, wick)))
        l = min(o, c) * (1 - abs(self.rng.gauss(0, wick)))
        v = self.rng.uniform(800, 1200) * (self.rng.uniform(2.5, 4.5) if burst else 1)
        self.series[symbol].append(Bar(self._t.isoformat(), o, h, l, c, v))
        return c

    def tick(self) -> None:
        self.minute += 1
        self.now += timedelta(minutes=1)
        self._t = self.now
        for s, bars in self.series.items():
            self._step(s, bars[-1].c)

    def _key(self, symbol: str) -> str:
        for s in self.series:
            if norm(s) == norm(symbol):
                return s
        raise KeyError(symbol)

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        out = {}
        for sym in symbols:
            mid, half = self.price(sym), self.spread_pct[self._key(sym)] / 200
            out[sym] = {"bid": mid * (1 - half), "ask": mid * (1 + half)}
        return out

    def price(self, symbol: str) -> float:
        for s, bars in self.series.items():
            if norm(s) == norm(symbol):
                return bars[-1].c
        raise KeyError(symbol)

    def shock(self, pct: float) -> None:
        """Simulera en krasch: sänk alla priser pct procent i senaste baren."""
        for bars in self.series.values():
            b = bars[-1]
            c = b.c * (1 + pct / 100)
            bars[-1] = Bar(b.t, b.o, max(b.h, c), min(b.l, c), c, b.v * 5)

    def exposure(self) -> float:
        return sum(p["qty"] * self.price(s) for s, p in self.pos.items())

    def account(self) -> dict:
        exposure = self.exposure()
        equity = self.cash + exposure
        if exposure > 0 and self.leverage > 1 and equity <= exposure * self.maintenance:
            self.liquidations += 1
            import logging
            logging.getLogger("bot").critical(
                "LIKVIDATION: kontovärdet %.2f räcker inte som marginal för positioner på %.2f (%.0fx) – allt stängs.",
                equity, exposure, self.leverage)
            self.flatten()
            exposure, equity = 0.0, self.cash
        buying_power = max(0.0, equity * self.leverage - exposure)
        return {"equity": equity, "buying_power": buying_power, "exposure": exposure, "leverage": self.leverage}

    def positions(self) -> dict[str, dict]:
        return {
            s: {**p, "current_price": self.price(s)} for s, p in self.pos.items()
        }

    def bars(self, symbols: list[str], timeframe: str, limit: int) -> dict[str, list[Bar]]:
        return {s: self.series[s][-limit:] for s in symbols}

    def market_open(self) -> bool:
        return True

    def buy(self, symbol: str, qty: float) -> str:
        px = self.quotes([symbol])[symbol]["ask"]
        self.cash -= px * qty * (1 + self.fee)
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
            self.cash += p["qty"] * self.quotes([key])[key]["bid"] * (1 - self.fee)

    def flatten(self) -> None:
        for s in list(self.pos):
            self.close(s)
