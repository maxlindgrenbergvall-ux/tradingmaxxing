"""CLI:
  python -m bot run              kör mot Alpaca (paper som standard)
  python -m bot status           visa nödbroms-/kontostatus
  python -m bot unlock --confirm GRANSKAT [--reset-baseline]
  python -m bot sim              offline-simulering, inga API-nycklar behövs
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from .broker import AlpacaBroker, SimBroker
from .config import DEFAULT_PATH, apply_profile, load_config, symbols
from .engine import Engine
from .risk import RiskManager, utcnow

ROOT = DEFAULT_PATH.parent


def setup_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bot")
    ap.add_argument("--config", default=str(DEFAULT_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--profile", default="normal", help="t.ex. hogfrekvens")
    sub.add_parser("status")
    u = sub.add_parser("unlock")
    u.add_argument("--confirm", required=True, help="Skriv GRANSKAT för att bekräfta manuell granskning")
    u.add_argument("--reset-baseline", action="store_true",
                   help="Använd nuvarande equity som nytt startkapital")
    sm = sub.add_parser("sim")
    sm.add_argument("--minutes", type=int, default=600)
    sm.add_argument("--drift", type=float, default=-0.0004, help="negativ = fallande marknad")
    sm.add_argument("--seed", type=int, default=7)
    sm.add_argument("--profile", default="normal")
    args = ap.parse_args(argv)

    cfg = apply_profile(load_config(args.config), getattr(args, "profile", None))
    state_dir = ROOT / cfg["runtime"].get("state_dir", "state")

    if args.cmd == "sim":
        setup_logging(None)
        tmp = Path(tempfile.mkdtemp(prefix="botsim-"))
        risk = RiskManager(cfg["safety"], tmp)
        broker = SimBroker(symbols(cfg), seed=args.seed, drift=args.drift)
        eng = Engine(cfg, broker, risk)
        t0 = utcnow()
        for m in range(args.minutes):
            if not eng.step(t0 + timedelta(minutes=m)):
                break
            broker.tick()
        eq = broker.account()["equity"]
        print(f"\nSim klar efter {m + 1} min. Equity {eq:,.2f} "
              f"(drawdown {risk.drawdown_pct(eq):.1f} %). Halted={risk.state.halted}. State i {tmp}")
        return 0

    setup_logging(ROOT / cfg["runtime"].get("log_file", "state/bot.log"))
    risk = RiskManager(cfg["safety"], state_dir)
    mode = cfg["account"]["mode"]

    if args.cmd == "status":
        st = risk.state
        print(f"Läge: {mode}\nStoppad: {st.halted}  {st.halt_reason or ''}\n"
              f"Startkapital: {st.start_equity}\nNödbromsnivå: {risk.kill_level:,.2f}\n"
              f"Första demokörning: {st.first_paper_run_at}\nCooldown till: {st.cooldown_until}")
        try:
            eq = AlpacaBroker(cfg["strategy"]["asset_class"], mode).account()["equity"]
            print(f"Aktuell equity: {eq:,.2f} (drawdown {risk.drawdown_pct(eq):.1f} %)")
        except Exception as e:  # status ska fungera även utan nät/nycklar
            print(f"(kunde inte hämta konto: {e})")
        return 0

    if args.cmd == "unlock":
        if args.confirm != "GRANSKAT":
            print("Skriv exakt --confirm GRANSKAT")
            return 2
        eq = AlpacaBroker(cfg["strategy"]["asset_class"], mode).account()["equity"]
        print(risk.unlock(eq, args.reset_baseline))
        return 0

    # run
    if risk.state.halted or risk.halt_file.exists():
        print(f"VÄGRAR STARTA - boten är stoppad för granskning:\n{risk.state.halt_reason}\n"
              "Kör `python -m bot unlock --confirm GRANSKAT` när du granskat kontot.")
        return 3
    try:
        risk.check_mode_allowed(mode)
    except RuntimeError as e:
        print(e)
        return 4
    broker = AlpacaBroker(cfg["strategy"]["asset_class"], mode)
    Engine(cfg, broker, risk).run_forever()
    return 3 if risk.state.halted else 0


if __name__ == "__main__":
    sys.exit(main())
