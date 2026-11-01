"""Själva handelsloopen."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

from .broker import Broker, norm
from .config import symbols
from .risk import RiskManager, utcnow
from .sizing import position_size
from . import strategies

log = logging.getLogger("bot")


class Engine:
    def __init__(self, cfg: dict[str, Any], broker: Broker, risk: RiskManager, persist: bool = True,
                 logger: logging.Logger | None = None):
        self.log = logger or log
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
        # persist=False används av backtesten: allt hålls i minnet, inget skrivs till disk.
        self.persist = persist
        self.journal_mem: list[dict] = []
        self._last_block = "OK"
        self.last_account: dict | None = None
        self.last_positions: dict[str, dict] = {}
        self.last_block_reason = "OK"
        self.last_scan: dict[str, Any] | None = None
        self.scans = 0
        self.signals_seen = 0
        self.orders_sent = 0
        self._last_summary = 0.0
        self.recent_exits: dict[str, datetime] = {}
        self.skipped: dict[str, int] = {"spread": 0, "edge": 0, "reentry": 0}
        self.trades: dict[str, dict] = (
            json.loads(self.trades_path.read_text()) if self.trades_path.exists() else {}
        )

    def _save_trades(self) -> None:
        if not self.persist:
            return
        tmp = self.trades_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.trades, indent=2))
        os.replace(tmp, self.trades_path)

    def _journal(self, entry: dict) -> None:
        if not self.persist:
            self.journal_mem.append(entry)
            return
        with open(self.journal_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    # ------------------------------------------------------------------
    def step(self, now: datetime | None = None) -> bool:
        """En iteration. Returnerar False när boten ska stanna (nödbroms)."""
        now = now or utcnow()
        if self.risk.state.halted:
            self.log.error("Boten är stoppad: %s", self.risk.state.halt_reason)
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
            self.log.info("Nya trades: %s", "tillåtna" if ok else f"blockerade ({why})")
            self._last_block = why
        if not ok:
            return True
        if not self.broker.market_open():
            self.log.debug("Marknaden stängd.")
            return True
        self._enter(acct, self.broker.positions(), now)
        self.last_positions = self.broker.positions()
        return True

    def _kill(self) -> None:
        self.log.critical(self.risk.state.halt_reason)
        if self.cfg["safety"].get("flatten_on_kill", True):
            try:
                self.broker.flatten()
                self.log.critical("Alla ordrar annullerade och positioner stängda.")
            except Exception:
                self.log.exception("Kunde inte stänga positioner - STÄNG MANUELLT!")
        for sym, t in self.trades.items():
            px = self.last_positions.get(norm(sym), {}).get("current_price")
            entry = {**t, "symbol": sym, "closed_at": utcnow().isoformat(), "exit_reason": "kill_switch"}
            if px is not None:
                entry.update(exit_price=px, **self._trade_costs(t, px, px))
            self._journal(entry)
        self.trades = {}
        self._save_trades()
        self.last_positions = {}

    def _quotes(self, syms: list[str]) -> dict[str, dict]:
        if not syms:
            return {}
        try:
            return self.broker.quotes(syms)
        except Exception as e:
            self.log.warning("Kunde inte hämta bid/ask: %s", e)
            return {}

    def _manage_exits(self, positions: dict[str, dict], now: datetime) -> None:
        quotes = self._quotes(list(self.trades))
        for sym in list(self.trades):
            t = self.trades[sym]
            p = positions.get(norm(sym))
            if p is None:
                self.log.warning("%s: position saknas (stängd utanför boten?) - slutar följa den.", sym)
                del self.trades[sym]
                continue
            if not t.get("fill_confirmed"):
                # Justera nivåerna efter faktiskt fyllnadspris.
                shift = p["avg_entry_price"] - t["entry"]
                t.update(entry=p["avg_entry_price"], stop=t["stop"] + shift,
                         initial_stop=t["initial_stop"] + shift, target=t["target"] + shift,
                         qty=p["qty"], fill_confirmed=True)
            # Bedöm stopp/mål mot bid – det pris vi faktiskt kan sälja till.
            q = quotes.get(sym)
            price = q["bid"] if q else p["current_price"]
            t["last_bid"] = price
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
                exit_mid = (q["bid"] + q["ask"]) / 2 if q else price
                costs = self._trade_costs(t, price, exit_mid)
                self.log.info("EXIT %s %s @ %.4f  P&L %.2f (avgifter %.2f, spread %.2f)",
                              sym, reason, price, costs["pnl"], costs["fees"], costs["spread_cost"])
                self.risk.record_trade(costs["pnl"], now)
                self._journal({**t, "symbol": sym, "exit_price": price, "exit_reason": reason,
                               **costs, "closed_at": now.isoformat()})
                del self.trades[sym]
                self.recent_exits[sym] = now
        self._save_trades()

    def _trade_costs(self, t: dict, exit_price: float, exit_mid: float) -> dict:
        """Nettoresultat efter avgifter, uppdelat i prisrörelse, spread och avgift."""
        qty, entry = t["qty"], t["entry"]
        fee = self.s.get("fee_pct", 0.25) / 100
        entry_mid = t.get("entry_mid", entry)
        fees = fee * qty * (entry + exit_price)
        spread_cost = qty * (max(0.0, entry - entry_mid) + max(0.0, exit_mid - exit_price))
        gross = (exit_price - entry) * qty
        return {"pnl": round(gross - fees, 2), "gross_pnl": round(gross, 2), "fees": round(fees, 2),
                "spread_cost": round(spread_cost, 2), "move_pnl": round((exit_mid - entry_mid) * qty, 2)}

    def _costed(self, sig, quote: dict | None):
        """Anpassar en signal efter verkliga kostnader. Returnerar (entry, stop, target, skäl)."""
        s = self.s
        if not quote:
            return sig.entry, sig.stop, sig.target, None
        bid, ask = quote["bid"], quote["ask"]
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_pct = spread / mid * 100
        if spread_pct > s.get("max_spread_pct", 0.15):
            return None, None, None, f"spread {spread_pct:.2f} % > {s.get('max_spread_pct', 0.15)} %"
        entry = ask
        stop_dist = max(sig.entry - sig.stop,
                        spread * s.get("min_stop_spread_mult", 4.0),
                        entry * s.get("min_stop_pct", 0.3) / 100)
        cost = spread + 2 * entry * s.get("fee_pct", 0.25) / 100
        edge = cost * s.get("min_edge_mult", 2.0)
        if sig.rr:  # momentum: målet följer stoppet (R-multipel)
            reward = max(stop_dist * sig.rr, edge)
        else:       # övriga strategier har eget mål (t.ex. SMA(5) eller 2R från strukturstopp)
            reward = max(sig.target - entry, edge, stop_dist * s.get("min_reward_risk", 1.0))
        # Kostnadsfilter: målet måste gå att nå inom tradens maxtid. Priset rör sig typiskt
        # ungefär ATR(1 min) x roten ur antalet minuter – ligger målet längre bort än så
        # blir det mest tidsstopp, och då äter avgifterna upp kontot.
        hold = max(1.0, float(s.get("max_hold_minutes", 30)))
        reach = sig.atr * hold ** 0.5 * s.get("reach_mult", 1.0)
        if sig.atr > 0 and reward > reach:
            return None, None, None, (f"målet +{reward / entry * 100:.2f} % (för att täcka spread och avgift) är för långt "
                                      f"bort – priset rör sig normalt ~{reach / entry * 100:.2f} % på {hold:.0f} min")
        return entry, entry - stop_dist, entry + reward, None

    def _enter(self, acct: dict, positions: dict[str, dict], now: datetime) -> None:
        free_slots = int(self.z.get("max_open_positions", 3)) - len(positions)
        if free_slots <= 0:
            self.last_scan = {"at": now.isoformat(), "full": True, "rows": (self.last_scan or {}).get("rows", [])}
            return
        held = set(positions)
        bars = self.broker.bars(self.symbols, self.s.get("timeframe", "1Min"),
                                int(self.s.get("lookback_bars", 60)))
        quotes = self._quotes(self.symbols)
        reentry = timedelta(minutes=float(self.s.get("reentry_minutes", 5)))
        names = strategies.active(self.s)
        rows, signals = [], []
        for sym in self.symbols:
            sb = bars.get(sym, [])
            common: dict[str, bool] = {}
            extra: dict[str, float | None] = {"age_min": None}
            if sb:
                try:
                    age = (now - datetime.fromisoformat(sb[-1].t.replace("Z", "+00:00"))).total_seconds() / 60
                    extra["age_min"] = age
                    # Senaste minutstapeln måste vara ny – annars handlar ingen symbolen just nu.
                    common["fresh"] = age <= self.s.get("max_bar_age_minutes", 3)
                except ValueError:
                    pass
            q = quotes.get(sym)
            if q:
                sp = (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2) * 100
                extra["spread_pct"] = sp
                common["spread_ok"] = sp <= self.s.get("max_spread_pct", 0.15)
            last_exit = self.recent_exits.get(sym)
            if last_exit:
                common["not_recent"] = now - last_exit >= reentry
            held_sym = norm(sym) in held
            results = []
            for name in names:
                d = strategies.run(name, sym, sb, self.s)
                if d.get("checks"):
                    d["checks"] = {**d["checks"], **common}
                    if d.get("signal") and q and all(d["checks"].values()):
                        why = self._costed(d["signal"], q)[3]
                        d["checks"]["costs_ok"] = why is None
                        if why:
                            d["cost_note"] = why
                    d["passed"], d["total"] = sum(d["checks"].values()), len(d["checks"])
                    if not all(d["checks"].values()):
                        d["signal"] = None
                d["metrics"] = {**d.get("metrics", {}), **extra}
                results.append(d)
            ready = [d for d in results if d.get("signal")]
            if ready and not held_sym:
                signals.append(max((d["signal"] for d in ready), key=lambda x: x.score))
            best = (max(ready, key=lambda d: d["signal"].score) if ready else
                    max(results, key=lambda d: (d.get("passed", 0) / d["total"]) if d.get("total") else -1))
            rows.append({
                "symbol": sym, "held": held_sym, "ready": bool(ready), "strategy": best["strategy"],
                "checks": best.get("checks", {}), "metrics": best.get("metrics", {}), "note": best.get("note"),
                "passed": best.get("passed"), "total": best.get("total"), "cost_note": best.get("cost_note"),
                "others": [{"strategy": d["strategy"], "passed": d.get("passed"), "total": d.get("total"),
                            "ready": bool(d.get("signal")), "note": d.get("note")} for d in results],
            })
        self.scans += 1
        self.signals_seen += len(signals)
        self.last_scan = {"at": now.isoformat(), "full": False, "rows": rows}
        self._log_summary(rows, len(signals))
        signals.sort(key=lambda x: x.score, reverse=True)
        buying_power = acct["buying_power"]
        for sig in signals[:free_slots]:
            entry, stop, target, why = self._costed(sig, quotes.get(sig.symbol))
            if why:
                self.skipped["edge"] += 1
                continue
            lev = float(self.z.get("leverage", 1.0))
            if lev > 1:
                # Hävstång som på en kryptobörs: marginal = max_position_pct av kontot, position = marginal x hävstång.
                notional = min(acct["equity"] * self.z.get("max_position_pct", 20.0) / 100 * lev, buying_power * 0.98)
                qty = notional / entry if notional > 0 else 0.0
                qty = (int(qty * 1e6) / 1e6) if self.fractional else float(int(qty))
            else:
                qty = position_size(
                    equity=acct["equity"], buying_power=buying_power, entry=entry, stop=stop,
                    risk_pct=self.z.get("risk_per_trade_pct", 1.0),
                    max_position_pct=self.z.get("max_position_pct", 20.0),
                    fractional=self.fractional,
                )
            if qty <= 0 or qty * entry < 10:
                self.log.warning("%s: signal men för liten position (%.6f st) – hoppar över.", sig.symbol, qty)
                continue
            try:
                order_id = self.broker.buy(sig.symbol, qty)
            except Exception as e:
                self.log.error("Köporder för %s nekades: %s", sig.symbol, e)
                continue
            self.orders_sent += 1
            buying_power -= qty * entry
            self.trades[sig.symbol] = {
                "order_id": order_id, "qty": qty, "entry": entry, "stop": stop,
                "initial_stop": stop, "target": target, "opened_at": now.isoformat(),
                "score": sig.score, "reasons": list(sig.reasons), "fill_confirmed": False,
                "strategy": sig.strategy,
                "entry_mid": ((quotes[sig.symbol]["bid"] + quotes[sig.symbol]["ask"]) / 2
                              if sig.symbol in quotes else entry),
            }
            self.log.info("ENTRY %s (%s) qty=%s @ ~%.4f stop %.4f target %.4f [%s]",
                     sig.symbol, strategies.STRATEGIES[sig.strategy]["label"], qty, entry, stop, target,
                     ", ".join(sig.reasons))
        self._save_trades()

    def _log_summary(self, rows: list[dict], n_signals: int) -> None:
        """En rad per minut i loggen om vad skanningen ser, så det syns att boten jobbar."""
        if time.monotonic() - self._last_summary < 60 and not n_signals:
            return
        self._last_summary = time.monotonic()
        scored = [r for r in rows if r.get("total") and not r["held"]]
        if not scored:
            self.log.info("Skanning: väntar på data (%s)", ", ".join(f"{r['symbol']}: {r.get('note', '-')}" for r in rows[:3]))
            return
        best = max(scored, key=lambda r: r["passed"] / r["total"])
        missing = [strategies.check_label(k) for k, ok in best["checks"].items() if not ok]
        self.log.info("Skanning %d: %d symboler, %d signaler. Närmast: %s (%s) %d/%d villkor%s",
                 self.scans, len(rows), n_signals, best["symbol"],
                 strategies.STRATEGIES[best["strategy"]]["label"], best["passed"], best["total"],
                 f" (saknas: {', '.join(missing)})" if missing else "")

    # ------------------------------------------------------------------
    def run_forever(self, stop_event=None, poll: float | None = None, before_step=None,
                    clock=None) -> None:
        """Kör tills nödbromsen löser ut eller stop_event sätts.
        before_step/clock används av simulatorn för att driva fram tiden."""
        poll = float(poll if poll is not None else self.cfg["runtime"].get("poll_seconds", 30))
        self.log.info("Startar. Läge=%s, tillgångar=%s, nödbroms vid equity <= %.2f",
                 self.cfg["account"]["mode"], self.s["asset_class"], self.risk.kill_level)
        errors = 0
        while not (stop_event and stop_event.is_set()):
            try:
                if before_step:
                    before_step()
                if not self.step(clock() if clock else None):
                    self.log.critical("Boten har stannat för manuell granskning. Se state/HALTED.")
                    return
                errors = 0
            except KeyboardInterrupt:
                raise
            except Exception:
                errors += 1
                self.log.exception("Fel i loopen (%d i rad)", errors)
                if errors >= 10:
                    self.risk.halt("10 fel i rad - stoppar för granskning.")
                    return
            if stop_event:
                stop_event.wait(poll)
            else:
                time.sleep(poll)
        self.log.info("Boten stoppad av användaren.")
