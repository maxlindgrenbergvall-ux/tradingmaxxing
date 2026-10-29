"""Själva handelsloopen."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any

from .broker import Broker, norm
from .config import symbols
from .risk import RiskManager, utcnow
from .sizing import position_size
from .strategy import evaluate

log = logging.getLogger("bot")


class Engine:
    def __init__(self, cfg: dict[str, Any], broker: Broker, risk: RiskManager):
        self.cfg = cfg
        self.broker = broker
        self.risk = risk
        self.s = cfg["strategy"]
        self.z = cfg["sizing"]
        self.symbols = symbols(cfg)
        self.fractional = self.s["asset_class"] == "crypto"
        self.dir = risk.dir
        self.trades_path = self.dir / "open_trades.json"
        self.journal_path = self.dir / "journal.jsonl"
        self._last_block = "OK"
        self.trades: dict[str, dict] = (
            json.loads(self.trades_path.read_text()) if self.trades_path.exists() else {}
        )

    def _save_trades(self) -> None:
        tmp = self.trades_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.trades, indent=2))
        os.replace(tmp, self.trades_path)

    def _journal(self, entry: dict) -> None:
        with open(self.journal_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    # ------------------------------------------------------------------
    def step(self, now: datetime | None = None) -> bool:
        """En iteration. Returnerar False när boten ska stanna (nödbroms)."""
        now = now or utcnow()
        if self.risk.state.halted:
            log.error("Boten är stoppad: %s", self.risk.state.halt_reason)
            return False

        acct = self.broker.account()
        if self.risk.update(acct["equity"], now):
            self._kill()
            return False

        positions = self.broker.positions()
        self._manage_exits(positions, now)

        ok, why = self.risk.can_open(acct["equity"], now)
        if why != self._last_block:
            log.info("Nya trades: %s", "tillåtna" if ok else f"blockerade ({why})")
            self._last_block = why
        if not ok:
            return True
        if not self.broker.market_open():
            log.debug("Marknaden stängd.")
            return True
        self._enter(acct, self.broker.positions(), now)
        return True

    def _kill(self) -> None:
        log.critical(self.risk.state.halt_reason)
        if self.cfg["safety"].get("flatten_on_kill", True):
            try:
                self.broker.flatten()
                log.critical("Alla ordrar annullerade och positioner stängda.")
            except Exception:
                log.exception("Kunde inte stänga positioner - STÄNG MANUELLT!")
        for sym, t in self.trades.items():
            self._journal({**t, "symbol": sym, "closed_at": utcnow().isoformat(), "exit_reason": "kill_switch"})
        self.trades = {}
        self._save_trades()

    def _manage_exits(self, positions: dict[str, dict], now: datetime) -> None:
        for sym in list(self.trades):
            t = self.trades[sym]
            p = positions.get(norm(sym))
            if p is None:
                log.warning("%s: position saknas (stängd utanför boten?) - slutar följa den.", sym)
                del self.trades[sym]
                continue
            if not t.get("fill_confirmed"):
                # Justera nivåerna efter faktiskt fyllnadspris.
                shift = p["avg_entry_price"] - t["entry"]
                t.update(entry=p["avg_entry_price"], stop=t["stop"] + shift,
                         initial_stop=t["initial_stop"] + shift, target=t["target"] + shift,
                         qty=p["qty"], fill_confirmed=True)
            price = p["current_price"]
            r = t["entry"] - t["initial_stop"]
            if r > 0 and price >= t["entry"] + r * self.s.get("trail_after_r", 1.0):
                t["stop"] = max(t["stop"], t["entry"])
            age_min = (now - datetime.fromisoformat(t["opened_at"])).total_seconds() / 60

            reason = None
            if price <= t["stop"]:
                reason = "stop"
            elif price >= t["target"]:
                reason = "target"
            elif age_min >= self.s.get("max_hold_minutes", 30):
                reason = "time_stop"
            if reason:
                self.broker.close(sym)
                pnl = (price - t["entry"]) * t["qty"]
                log.info("EXIT %s %s @ %.4f  P&L %.2f", sym, reason, price, pnl)
                self.risk.record_trade(pnl, now)
                self._journal({**t, "symbol": sym, "exit_price": price, "exit_reason": reason,
                               "pnl": round(pnl, 2), "closed_at": now.isoformat()})
                del self.trades[sym]
        self._save_trades()

    def _enter(self, acct: dict, positions: dict[str, dict], now: datetime) -> None:
        free_slots = int(self.z.get("max_open_positions", 3)) - len(positions)
        if free_slots <= 0:
            return
        held = set(positions)
        bars = self.broker.bars(self.symbols, self.s.get("timeframe", "1Min"),
                                int(self.s.get("lookback_bars", 60)))
        signals = [sig for sym, b in bars.items()
                   if norm(sym) not in held and (sig := evaluate(sym, b, self.s))]
        signals.sort(key=lambda x: x.score, reverse=True)
        buying_power = acct["buying_power"]
        for sig in signals[:free_slots]:
            qty = position_size(
                equity=acct["equity"], buying_power=buying_power, entry=sig.entry, stop=sig.stop,
                risk_pct=self.z.get("risk_per_trade_pct", 1.0),
                max_position_pct=self.z.get("max_position_pct", 20.0),
                fractional=self.fractional,
            )
            if qty <= 0 or qty * sig.entry < 10:
                continue
            order_id = self.broker.buy(sig.symbol, qty)
            buying_power -= qty * sig.entry
            self.trades[sig.symbol] = {
                "order_id": order_id, "qty": qty, "entry": sig.entry, "stop": sig.stop,
                "initial_stop": sig.stop, "target": sig.target, "opened_at": now.isoformat(),
                "score": sig.score, "reasons": list(sig.reasons), "fill_confirmed": False,
            }
            log.info("ENTRY %s qty=%s @ ~%.4f stop %.4f target %.4f [%s]",
                     sig.symbol, qty, sig.entry, sig.stop, sig.target, ", ".join(sig.reasons))
        self._save_trades()

    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        poll = float(self.cfg["runtime"].get("poll_seconds", 30))
        log.info("Startar. Läge=%s, tillgångar=%s, nödbroms vid equity <= %.2f",
                 self.cfg["account"]["mode"], self.s["asset_class"], self.risk.kill_level)
        errors = 0
        while True:
            try:
                if not self.step():
                    log.critical("Boten har stannat för manuell granskning. Se state/HALTED.")
                    return
                errors = 0
            except KeyboardInterrupt:
                raise
            except Exception:
                errors += 1
                log.exception("Fel i loopen (%d i rad)", errors)
                if errors >= 10:
                    self.risk.halt("10 fel i rad - stoppar för granskning.")
                    return
            time.sleep(poll)
