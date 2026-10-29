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
from .strategy import CHECK_LABELS, diagnose

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
        self.last_account: dict | None = None
        self.last_positions: dict[str, dict] = {}
        self.last_block_reason = "OK"
        self.last_scan: dict[str, Any] | None = None
        self.scans = 0
        self.signals_seen = 0
        self.orders_sent = 0
        self._last_summary = 0.0
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
        self.last_account = acct
        if self.risk.update(acct["equity"], now):
            self._kill()
            return False

        positions = self.broker.positions()
        self.last_positions = positions
        self._manage_exits(positions, now)

        ok, why = self.risk.can_open(acct["equity"], now)
        self.last_block_reason = why
        if why != self._last_block:
            log.info("Nya trades: %s", "tillåtna" if ok else f"blockerade ({why})")
            self._last_block = why
        if not ok:
            return True
        if not self.broker.market_open():
            log.debug("Marknaden stängd.")
            return True
        self._enter(acct, self.broker.positions(), now)
        self.last_positions = self.broker.positions()
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
            px = self.last_positions.get(norm(sym), {}).get("current_price")
            entry = {**t, "symbol": sym, "closed_at": utcnow().isoformat(), "exit_reason": "kill_switch"}
            if px is not None:
                entry.update(exit_price=px, pnl=round((px - t["entry"]) * t["qty"], 2))
            self._journal(entry)
        self.trades = {}
        self._save_trades()
        self.last_positions = {}

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
            self.last_scan = {"at": now.isoformat(), "full": True, "rows": (self.last_scan or {}).get("rows", [])}
            return
        held = set(positions)
        bars = self.broker.bars(self.symbols, self.s.get("timeframe", "1Min"),
                                int(self.s.get("lookback_bars", 60)))
        rows, signals = [], []
        for sym in self.symbols:
            d = diagnose(sym, bars.get(sym, []), self.s, now)
            d["held"] = norm(sym) in held
            if d["signal"] and not d["held"]:
                signals.append(d["signal"])
            rows.append({k: v for k, v in d.items() if k != "signal"} | {"ready": bool(d["signal"])})
        self.scans += 1
        self.signals_seen += len(signals)
        self.last_scan = {"at": now.isoformat(), "full": False, "rows": rows}
        self._log_summary(rows, len(signals))
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
                log.warning("%s: signal men för liten position (%.6f st) – hoppar över.", sig.symbol, qty)
                continue
            try:
                order_id = self.broker.buy(sig.symbol, qty)
            except Exception as e:
                log.error("Köporder för %s nekades: %s", sig.symbol, e)
                continue
            self.orders_sent += 1
            buying_power -= qty * sig.entry
            self.trades[sig.symbol] = {
                "order_id": order_id, "qty": qty, "entry": sig.entry, "stop": sig.stop,
                "initial_stop": sig.stop, "target": sig.target, "opened_at": now.isoformat(),
                "score": sig.score, "reasons": list(sig.reasons), "fill_confirmed": False,
            }
            log.info("ENTRY %s qty=%s @ ~%.4f stop %.4f target %.4f [%s]",
                     sig.symbol, qty, sig.entry, sig.stop, sig.target, ", ".join(sig.reasons))
        self._save_trades()

    def _log_summary(self, rows: list[dict], n_signals: int) -> None:
        """En rad per minut i loggen om vad skanningen ser, så det syns att boten jobbar."""
        if time.monotonic() - self._last_summary < 60 and not n_signals:
            return
        self._last_summary = time.monotonic()
        scored = [r for r in rows if r.get("total") and not r["held"]]
        if not scored:
            log.info("Skanning: väntar på data (%s)", ", ".join(f"{r['symbol']}: {r.get('note', '-')}" for r in rows[:3]))
            return
        best = max(scored, key=lambda r: r["passed"])
        missing = [CHECK_LABELS[k] for k, ok in best["checks"].items() if not ok]
        log.info("Skanning %d: %d symboler, %d signaler. Närmast: %s %d/%d villkor%s",
                 self.scans, len(rows), n_signals, best["symbol"], best["passed"], best["total"],
                 f" (saknas: {', '.join(missing)})" if missing else "")

    # ------------------------------------------------------------------
    def run_forever(self, stop_event=None, poll: float | None = None, before_step=None,
                    clock=None) -> None:
        """Kör tills nödbromsen löser ut eller stop_event sätts.
        before_step/clock används av simulatorn för att driva fram tiden."""
        poll = float(poll if poll is not None else self.cfg["runtime"].get("poll_seconds", 30))
        log.info("Startar. Läge=%s, tillgångar=%s, nödbroms vid equity <= %.2f",
                 self.cfg["account"]["mode"], self.s["asset_class"], self.risk.kill_level)
        errors = 0
        while not (stop_event and stop_event.is_set()):
            try:
                if before_step:
                    before_step()
                if not self.step(clock() if clock else None):
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
            if stop_event:
                stop_event.wait(poll)
            else:
                time.sleep(poll)
        log.info("Boten stoppad av användaren.")
