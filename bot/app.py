"""Desktop-appen: kör boten i en bakgrundstråd och serverar dashboarden på localhost."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .broker import AlpacaBroker, SimBroker, norm
from .config import DEFAULT_PATH, apply_profile, load_config, profiles, symbols
from .engine import Engine
from .risk import RiskManager, utcnow
from .backtest import BacktestRunner
from .updater import RESTART_CODE, Updater

ROOT = DEFAULT_PATH.parent
WEB = ROOT / "web"
HOST, PORT = "127.0.0.1", 8765
MAX_POINTS = 600

log = logging.getLogger("bot")


class MemoryLog(logging.Handler):
    def __init__(self, size: int = 300):
        super().__init__()
        self.lines: deque[dict] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append({
            "t": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "msg": record.getMessage(),
        })


class Controller:
    """Startar/stoppar boten och samlar ihop allt dashboarden visar."""

    def __init__(self):
        self.base_cfg = load_config()
        self.cfg = self.base_cfg
        self.state_root = ROOT / self.cfg["runtime"].get("state_dir", "state")
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._bar_cache: dict[str, tuple[float, dict]] = {}
        self.thread: threading.Thread | None = None
        self.creds_path = self.state_root / "credentials.json"
        self.settings_path = self.state_root / "settings.json"
        settings = json.loads(self.settings_path.read_text()) if self.settings_path.exists() else {}
        self.profile = "normal"
        self.leverage = 1.0
        try:
            self.set_profile(settings.get("profile", "hogfrekvens"), save=False)
            self.set_leverage(settings.get("leverage", 1), save=False)
        except ValueError:
            pass
        self._load_credentials()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.engine: Engine | None = None
        self.mode = "paper"
        self.updater = Updater(ROOT, self.base_cfg, self.state_root)
        self.backtests = BacktestRunner(self.base_cfg, self.state_root)
        self.restart_event = threading.Event()
        self.error: str | None = None
        self.sim_clock: datetime | None = None
        self._data_broker: AlpacaBroker | None = None
        self._bars_lock = threading.Lock()

    # ---------- nycklar ----------
    def _load_credentials(self) -> None:
        if self.creds_path.exists():
            data = json.loads(self.creds_path.read_text())
            os.environ.setdefault("ALPACA_API_KEY", data.get("key", ""))
            os.environ.setdefault("ALPACA_SECRET_KEY", data.get("secret", ""))

    def save_credentials(self, key: str, secret: str) -> dict:
        key, secret = key.strip(), secret.strip()
        if not key or not secret:
            raise ValueError("Fyll i både API Key och Secret Key.")
        os.environ["ALPACA_API_KEY"] = key
        os.environ["ALPACA_SECRET_KEY"] = secret
        acct = AlpacaBroker(self.cfg["strategy"]["asset_class"], "paper").account()
        self.creds_path.write_text(json.dumps({"key": key, "secret": secret}))
        self._data_broker = None
        os.chmod(self.creds_path, 0o600)
        return {"ok": True, "equity": acct["equity"]}

    def set_profile(self, name: str, save: bool = True) -> None:
        if self.running:
            raise RuntimeError("Stoppa boten innan du byter tempo.")
        self.cfg = apply_profile(self.base_cfg, name)
        self.profile = name
        self._bar_cache = {}
        if save:
            self._save_settings()

    LEVERAGES = (1, 2, 5, 10, 20)

    def set_leverage(self, lev: float, save: bool = True) -> None:
        if self.running:
            raise RuntimeError("Stoppa boten innan du byter hävstång.")
        lev = float(lev)
        if lev not in self.LEVERAGES:
            raise ValueError("Ogiltig hävstång.")
        self.leverage = lev
        if save:
            self._save_settings()

    def _save_settings(self) -> None:
        self.settings_path.write_text(json.dumps({"profile": self.profile, "leverage": self.leverage}))

    def has_keys(self) -> bool:
        return bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))

    # ---------- state per läge ----------
    def state_dir(self, mode: str) -> Path:
        return self.state_root / "sim" if mode == "sim" else self.state_root

    def risk_for(self, mode: str) -> RiskManager:
        return RiskManager(self.cfg["safety"], self.state_dir(mode))

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    # ---------- start/stopp ----------
    def start(self, mode: str) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("Boten kör redan.")
            if mode not in ("paper", "sim"):
                raise ValueError("Okänt läge.")
            self.error = None
            if mode == "sim":
                shutil.rmtree(self.state_dir("sim"), ignore_errors=True)
            risk = self.risk_for(mode)
            if risk.state.halted or risk.halt_file.exists():
                raise RuntimeError("Nödbromsen har löst ut – granska och lås upp först.")
            cfg = json.loads(json.dumps(self.cfg))
            if mode == "sim":
                cfg["runtime"]["poll_seconds"] = 1
                cfg["sizing"]["leverage"] = self.leverage
                broker = SimBroker(symbols(cfg), seed=None, drift=-0.0002, leverage=self.leverage)
                self.sim_clock = broker.now
            else:
                if not self.has_keys():
                    raise RuntimeError("Lägg in dina Alpaca paper-nycklar under Inställningar först.")
                risk.check_mode_allowed("paper")
                broker = AlpacaBroker(cfg["strategy"]["asset_class"], "paper")
            self.mode = mode
            self._bar_cache = {}
            self.engine = Engine(cfg, broker, risk)
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self) -> None:
        eng = self.engine
        assert eng is not None
        kwargs: dict[str, Any] = {"stop_event": self.stop_event}
        if self.mode == "sim":
            def advance():
                eng.broker.tick()
                self.sim_clock = eng.broker.now
            kwargs.update(poll=1, before_step=advance, clock=lambda: self.sim_clock)
        orig_step = eng.step

        def step_and_record(now=None):
            ok = orig_step(now)
            if eng.last_account:
                self._record_equity(eng.last_account["equity"], now or utcnow())
            return ok
        eng.step = step_and_record
        try:
            eng.run_forever(**kwargs)
        except Exception as e:  # visas i UI:t
            self.error = str(e)
            log.exception("Boten kraschade")

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def crash_test(self) -> None:
        if self.mode != "sim" or not self.running or not self.engine:
            raise RuntimeError("Krasch-test finns bara i simuleringsläge medan boten kör.")
        broker = self.engine.broker
        sym = symbols(self.cfg)[0]
        acct = broker.account()
        broker.buy(sym, acct["buying_power"] * 0.95 / broker.price(sym))
        broker.shock(-35)
        log.warning("Krasch-test: köpte för hela kontot och sänkte alla priser 35 %.")

    def unlock(self, confirm: str, reset: bool) -> str:
        if confirm.strip().upper() != "GRANSKAT":
            raise ValueError("Skriv GRANSKAT för att bekräfta att du granskat kontot.")
        if self.running:
            raise RuntimeError("Stoppa boten först.")
        risk = self.risk_for(self.mode)
        if self.mode == "sim":
            eq = self.engine.broker.account()["equity"] if self.engine else None
        else:
            eq = AlpacaBroker(self.cfg["strategy"]["asset_class"], "paper").account()["equity"]
        return risk.unlock(eq, reset)

    def _scan_info(self) -> dict | None:
        eng = self.engine
        if not eng:
            return None
        st = self.cfg["strategy"]
        return {
            "last": eng.last_scan,
            "scans": eng.scans,
            "signals": eng.signals_seen,
            "orders": eng.orders_sent,
            "poll_seconds": eng.cfg["runtime"].get("poll_seconds", 30),
            "thresholds": {
                "burst_pct": st.get("min_burst_pct", 0.6), "max_burst_pct": st.get("max_burst_pct", 3.0),
                "vol_ratio": st.get("volume_mult", 1.8), "range_ratio": st.get("range_expansion_mult", 1.3),
                "close_loc": st.get("min_close_location", 0.65), "atr_pct": st.get("min_atr_pct", 0.15),
                "burst_bars": st.get("burst_bars", 5), "max_spread_pct": st.get("max_spread_pct", 0.15),
            },
        }

    # ---------- uppdateringar ----------
    def apply_update(self) -> dict:
        if self.running:
            raise RuntimeError("Stoppa boten innan du uppdaterar.")
        out = self.updater.apply()
        (self.state_root / ".restarting").write_text("1")
        threading.Timer(1.0, self.restart_event.set).start()  # hinna svara webbsidan först
        return out

    # ---------- diagramdata för en position ----------
    def bars(self, symbol: str) -> dict:
        allowed = {norm(x): x for x in symbols(self.cfg)}
        if norm(symbol) not in allowed:
            raise ValueError("Okänd symbol.")
        symbol = allowed[norm(symbol)]
        now = utcnow().timestamp()
        hit = self._bar_cache.get(symbol)
        if hit and now - hit[0] < (1 if self.mode == "sim" else 5):
            return hit[1]
        if self.mode == "sim":
            if not self.engine:
                raise RuntimeError("Ingen simulering körs.")
            rows = list(self.engine.broker.series[symbol][-150:])
        else:
            with self._bars_lock:
                if not self._data_broker:
                    self._data_broker = AlpacaBroker(self.cfg["strategy"]["asset_class"], "paper")
                rows = self._data_broker.bars([symbol], "1Min", 150).get(symbol, [])
        out = {"symbol": symbol,
               "bars": [{"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v} for b in rows]}
        self._bar_cache[symbol] = (now, out)
        return out

    # ---------- data till dashboarden ----------
    def _record_equity(self, equity: float, at: datetime) -> None:
        path = self.state_dir(self.mode) / "equity.jsonl"
        with open(path, "a") as fh:
            fh.write(json.dumps({"t": at.isoformat(), "equity": round(equity, 2)}) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def status(self) -> dict:
        mode = self.mode
        d = self.state_dir(mode)
        risk = self.engine.risk if (self.engine and self.mode == mode) else self.risk_for(mode)
        st = risk.state
        equity_pts = self._read_jsonl(d / "equity.jsonl")
        journal = self._read_jsonl(d / "journal.jsonl")
        closed = [t for t in journal if "pnl" in t]

        acct = self.engine.last_account if self.engine else None
        equity = acct["equity"] if acct else (equity_pts[-1]["equity"] if equity_pts else None)
        start = st.start_equity

        # max drawdown från topp
        peak, max_dd = None, 0.0
        for p in equity_pts:
            peak = p["equity"] if peak is None else max(peak, p["equity"])
            if peak:
                max_dd = max(max_dd, (1 - p["equity"] / peak) * 100)

        # Frekvens: trades per timme och snittlig hålltid
        now_dt = self.sim_clock if (mode == "sim" and self.sim_clock) else utcnow()
        holds, first_open = [], None
        for t in closed:
            try:
                o, c = datetime.fromisoformat(t["opened_at"]), datetime.fromisoformat(t["closed_at"])
            except (KeyError, ValueError):
                continue
            holds.append((c - o).total_seconds() / 60)
            first_open = o if first_open is None else min(first_open, o)
        hours = (now_dt - first_open).total_seconds() / 3600 if first_open else 0
        last_hour = [t for t in closed if "closed_at" in t
                     and (now_dt - datetime.fromisoformat(t["closed_at"])).total_seconds() <= 3600]

        wins = [t["pnl"] for t in closed if t["pnl"] > 0]
        losses = [t["pnl"] for t in closed if t["pnl"] <= 0]
        gross_win, gross_loss = sum(wins), -sum(losses)
        kill_pct = float(self.cfg["safety"].get("kill_switch_drawdown_pct", 25))

        positions = []
        if self.engine:
            for sym, t in list(self.engine.trades.items()):
                p = self.engine.last_positions.get(norm(sym), {})
                cur = p.get("current_price", t["entry"])
                positions.append({
                    "symbol": sym, "qty": t["qty"], "entry": t["entry"], "price": cur,
                    "stop": t["stop"], "target": t["target"], "opened_at": t["opened_at"],
                    "pnl": (cur - t["entry"]) * t["qty"],
                    "initial_stop": t.get("initial_stop", t["stop"]),
                    "reasons": t.get("reasons", []),
                })

        demo_day = None
        if st.first_paper_run_at and mode == "paper":
            days = (utcnow() - datetime.fromisoformat(st.first_paper_run_at)).total_seconds() / 86400
            demo_day = days

        step = max(1, len(equity_pts) // MAX_POINTS)
        series = equity_pts[::step]
        if equity_pts and series[-1] is not equity_pts[-1]:
            series.append(equity_pts[-1])

        return {
            "mode": mode,
            "running": self.running,
            "error": self.error,
            "has_keys": self.has_keys(),
            "halted": st.halted,
            "halt_reason": st.halt_reason,
            "block_reason": self.engine.last_block_reason if self.engine else None,
            "asset_class": self.cfg["strategy"]["asset_class"],
            "equity": equity,
            "start_equity": start,
            "kill_level": risk.kill_level if start else None,
            "kill_pct": kill_pct,
            "drawdown_pct": risk.drawdown_pct(equity) if (equity and start) else 0.0,
            "day_pnl": (equity - st.day_start_equity) if (equity and st.day_start_equity) else 0.0,
            "total_pnl": (equity - start) if (equity and start) else 0.0,
            "max_drawdown_pct": max_dd,
            "trades": len(closed),
            "win_rate": len(wins) / len(closed) * 100 if closed else None,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
            "avg_win": sum(wins) / len(wins) if wins else None,
            "avg_loss": sum(losses) / len(losses) if losses else None,
            "expectancy": sum(t["pnl"] for t in closed) / len(closed) if closed else None,
            "realized_pnl": sum(t["pnl"] for t in closed),
            "losing_streak": st.losing_streak,
            "cooldown_until": st.cooldown_until,
            "demo_days": demo_day,
            "paper_only_days": int(self.cfg["safety"].get("paper_only_days", 7)),
            "positions": positions,
            "max_positions": int(self.cfg["sizing"].get("max_open_positions", 3)),
            "closed": closed[-200:][::-1],
            "trades_per_hour": len(closed) / hours if hours >= 0.25 else None,
            "trades_last_hour": len(last_hour),
            "avg_hold_min": sum(holds) / len(holds) if holds else None,
            "profile": self.profile,
            "leverage": self.leverage,
            "leverages": list(self.LEVERAGES),
            "exposure": (acct or {}).get("exposure"),
            "liquidations": getattr(self.engine.broker, "liquidations", 0) if self.engine else 0,
            "scan": self._scan_info(),
            "profiles": profiles(self.base_cfg),
            "equity_series": [{"t": p["t"], "v": p["equity"]} for p in series],
            "logs": list(MEMLOG.lines)[-120:][::-1],
            "watchlist": symbols(self.cfg),
            "max_hold_minutes": int(self.cfg["strategy"].get("max_hold_minutes", 30)),
            "sim_time": self.sim_clock.isoformat() if (mode == "sim" and self.sim_clock) else None,
        }


MEMLOG = MemoryLog()


def make_handler(ctrl: Controller):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # tyst
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self):
            if self.path == "/api/status":
                return self._json(200, {**ctrl.status(), "update": ctrl.updater.info()})
            if self.path == "/api/backtest/status":
                return self._json(200, ctrl.backtests.status())
            if self.path == "/api/backtest/history":
                return self._json(200, ctrl.backtests.history())
            if self.path.startswith("/api/backtest/result"):
                from urllib.parse import parse_qs, urlparse

                try:
                    rid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
                    return self._json(200, ctrl.backtests.get(rid))
                except Exception as e:
                    return self._json(400, {"error": str(e)})
            if self.path.startswith("/api/bars"):
                from urllib.parse import parse_qs, urlparse

                q = parse_qs(urlparse(self.path).query)
                try:
                    return self._json(200, ctrl.bars(q.get("symbol", [""])[0]))
                except Exception as e:
                    return self._json(400, {"error": str(e)})
            if self.path.startswith("/vendor/"):
                f = (WEB / "vendor" / Path(self.path).name)
                if f.exists():
                    ctype = "text/javascript" if f.suffix == ".js" else "text/plain"
                    return self._send(200, f.read_bytes(), ctype + "; charset=utf-8")
            if self.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            # Endast anrop från själva dashboarden (skydd mot andra webbsidor).
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
                return self._json(403, {"error": "forbidden"})
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                if self.path == "/api/start":
                    ctrl.start(body.get("mode", "paper"))
                    out = {"ok": True}
                elif self.path == "/api/stop":
                    ctrl.stop()
                    out = {"ok": True}
                elif self.path == "/api/keys":
                    out = ctrl.save_credentials(body.get("key", ""), body.get("secret", ""))
                elif self.path == "/api/unlock":
                    out = {"ok": True, "msg": ctrl.unlock(body.get("confirm", ""), bool(body.get("reset")))}
                elif self.path == "/api/crash-test":
                    ctrl.crash_test()
                    out = {"ok": True}
                elif self.path == "/api/update/check":
                    out = ctrl.updater.check()
                elif self.path == "/api/update/apply":
                    out = ctrl.apply_update()
                elif self.path == "/api/update/token":
                    ctrl.updater.set_token(body.get("token", ""))
                    out = ctrl.updater.check()
                elif self.path == "/api/backtest/start":
                    out = ctrl.backtests.start(
                        days=float(body.get("days", 7)), profiles=list(body.get("profiles") or []),
                        leverage=float(body.get("leverage", 1)), capital=float(body.get("capital", 100_000)),
                        spread_mode=body.get("spread_mode", "live"), fixed_spread=float(body.get("fixed_spread", 0.1)),
                        fee_pct=None if body.get("fee_pct") is None else float(body["fee_pct"]))
                elif self.path == "/api/leverage":
                    ctrl.set_leverage(body.get("leverage", 1))
                    out = {"ok": True}
                elif self.path == "/api/profile":
                    ctrl.set_profile(body.get("profile", "normal"))
                    out = {"ok": True}
                elif self.path == "/api/mode":
                    if ctrl.running:
                        raise RuntimeError("Stoppa boten innan du byter läge.")
                    ctrl.mode = body.get("mode", "paper")
                    ctrl.engine = None
                    out = {"ok": True}
                else:
                    return self._json(404, {"error": "okänd endpoint"})
                self._json(200, out)
            except Exception as e:
                self._json(400, {"error": str(e)})

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("bot").addHandler(MEMLOG)
    ctrl = Controller()
    fh = logging.FileHandler(ctrl.state_root / "bot.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("bot").addHandler(fh)

    url = f"http://{HOST}:{PORT}/"
    try:
        server = ThreadingHTTPServer((HOST, PORT), make_handler(ctrl))
    except OSError:
        print("Programmet verkar redan vara igång – öppnar det i webbläsaren.")
        webbrowser.open(url)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    keep_awake()
    print(f"Dashboard: {url}")
    print("Låt det här fönstret vara öppet – boten kör så länge programmet är igång.")

    def check_updates():
        while not ctrl.restart_event.wait(5 if ctrl.updater.checked_at is None else 1800):
            ctrl.updater.check()
    threading.Thread(target=check_updates, daemon=True).start()

    # Efter en uppdatering laddar den öppna sidan om sig själv – öppna inte en ny flik.
    marker = ctrl.state_root / ".restarting"
    restarted = marker.exists()
    marker.unlink(missing_ok=True)

    try:
        import webview  # pywebview: eget programfönster om det finns installerat

        window = webview.create_window("Momentum Bot", url, width=1400, height=900, min_size=(380, 600))
        threading.Thread(target=lambda: (ctrl.restart_event.wait(), window.destroy()), daemon=True).start()
        webview.start()
    except Exception:
        if not restarted:
            webbrowser.open(url)
        try:
            ctrl.restart_event.wait()
        except KeyboardInterrupt:
            pass
    finally:
        ctrl.stop()
        server.shutdown()
        server.server_close()
    if ctrl.restart_event.is_set():
        print("Uppdaterad – startar om...")
        sys.exit(RESTART_CODE)


def keep_awake() -> None:
    """Hindra Windows från att gå i viloläge medan programmet kör (Mac: caffeinate i startfilen)."""
    if os.name == "nt":
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


if __name__ == "__main__":
    main()
