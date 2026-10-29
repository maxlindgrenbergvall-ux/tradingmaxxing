"""Backtest på riktig historisk data från Alpaca.

Kör exakt samma Engine (signaler, stopp, mål, nödbroms, dagsgräns, hävstång) över
historiska 1-minutsstaplar. Varje minut spelas upp i fyra steg (öppning, lägsta/högsta
i den ordning stapeln troligen gick, stängning) så att stopp och mål kan slå till inne i
minuten. Köp görs bara på minutens öppningspris och bara på redan stängda staplar –
boten ser aldrig framtiden.

Spread: historiska bid/ask är för tunga att hämta, så spreaden per symbol uppskattas
från Alpacas aktuella bid/ask (eller ett fast värde) och läggs på varje köp och sälj.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from .broker import SimBroker, norm
from .config import apply_profile, symbols
from .engine import Engine
from .risk import RiskManager
from .strategy import Bar

DATA_URL = "https://data.alpaca.markets/v1beta3/crypto/us"
WARMUP_MIN = 120
log = logging.getLogger("bot")


# ---------------------------------------------------------------- datahämtning
def _headers() -> dict[str, str]:
    h = {"Accept": "application/json"}
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if key and secret:  # kryptodata kräver inga nycklar, men med nycklar blir gränserna högre
        h.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    return h


def fetch_bars(syms: list[str], start: datetime, end: datetime, get=None,
               progress: Callable[[str], None] | None = None) -> dict[str, list[Bar]]:
    """Hämtar 1-minutsstaplar för alla symboler mellan start och end (med sidindelning)."""
    get = get or requests.get
    out: dict[str, list[Bar]] = {s: [] for s in syms}
    params: dict[str, Any] = {"symbols": ",".join(syms), "timeframe": "1Min", "limit": 10000,
                              "start": start.isoformat().replace("+00:00", "Z"),
                              "end": end.isoformat().replace("+00:00", "Z"), "sort": "asc"}
    pages = 0
    while True:
        for attempt in range(4):
            r = get(f"{DATA_URL}/bars", params=params, headers=_headers(), timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            break
        if r.status_code >= 400:
            raise RuntimeError(f"Alpaca svarade {r.status_code}: {r.text[:200]}")
        data = r.json()
        for sym, rows in (data.get("bars") or {}).items():
            out.setdefault(sym, []).extend(Bar(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]) for b in rows)
        pages += 1
        if progress:
            progress(f"Hämtar historik från Alpaca… ({pages} sidor, {sum(len(v) for v in out.values()):,} staplar)")
        token = data.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    return out


def fetch_spreads(syms: list[str], get=None) -> dict[str, float]:
    """Nuvarande spread i procent per symbol, som uppskattning för hela perioden."""
    get = get or requests.get
    r = get(f"{DATA_URL}/latest/quotes", params={"symbols": ",".join(syms)}, headers=_headers(), timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Alpaca svarade {r.status_code} på bid/ask")
    out = {}
    for sym, q in (r.json().get("quotes") or {}).items():
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        if bid > 0 and ask >= bid:
            out[sym] = (ask - bid) / ((ask + bid) / 2) * 100
    return out


class BarCache:
    """Sparar hämtad historik per symbol och dygn i state/cache, så samma period inte hämtas två gånger."""

    def __init__(self, folder: Path):
        self.dir = folder
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, sym: str, day: str) -> Path:
        return self.dir / f"{norm(sym)}_{day}.json"

    def load(self, syms: list[str], start: datetime, end: datetime, fetch, progress=None) -> dict[str, list[Bar]]:
        today = datetime.now(timezone.utc).date()
        days = []
        d = start.date()
        while d <= end.date():
            days.append(d)
            d += timedelta(days=1)
        out: dict[str, list[Bar]] = {s: [] for s in syms}
        for day in days:
            key = day.isoformat()
            missing = [s for s in syms if not self._path(s, key).exists() or day >= today]
            if missing:
                d0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
                got = fetch(missing, max(d0, start), min(d0 + timedelta(days=1), end), progress=progress)
                for s in missing:
                    rows = got.get(s, [])
                    if day < today:  # dagens data är inte klar – cacha bara hela dygn
                        self._path(s, key).write_text(json.dumps([b.__dict__ for b in rows]))
                    out[s].extend(rows)
            for s in syms:
                if s not in missing:
                    out[s].extend(Bar(**b) for b in json.loads(self._path(s, key).read_text()))
        for s in syms:
            out[s] = [b for b in out[s] if start <= _ts(b.t) < end]
        return out


def _ts(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


# ---------------------------------------------------------------- uppspelning
class ReplayBroker(SimBroker):
    """Spelar upp riktig historik. Återanvänder simulatorns konto, avgifter, spread och hävstång."""

    SUBSTEPS = 4

    def __init__(self, data: dict[str, list[Bar]], spreads: dict[str, float], cash: float,
                 fee_pct: float, leverage: float):
        # Ingen super().__init__ – den skapar påhittade priser.
        self.cash = cash
        self.fee = fee_pct / 100
        self.leverage = max(1.0, float(leverage))
        self.maintenance = 0.5 / self.leverage
        self.liquidations = 0
        self.pos: dict[str, dict] = {}
        self.syms = list(data)
        self.by_norm = {norm(s): s for s in self.syms}
        self.spread_pct = {s: spreads.get(s, 0.1) for s in self.syms}
        self.bars_by_min: dict[str, dict[datetime, Bar]] = {
            s: {_ts(b.t).replace(second=0, microsecond=0): b for b in rows} for s, rows in data.items()}
        self.history: dict[str, list[Bar]] = {s: [] for s in self.syms}
        self.last_close: dict[str, float] = {}
        self.cur: dict[str, Bar | None] = {}
        self.now: datetime | None = None
        self.sub = 0
        self.minute = 0  # används i simulatorns order-id

    def set_minute(self, minute: datetime) -> None:
        # Stäng föregående minut: den blir synlig historik för strategin.
        for s in self.syms:
            b = self.cur.get(s)
            if b is not None:
                self.history[s].append(b)
                self.last_close[s] = b.c
        self.now = minute
        self.cur = {s: self.bars_by_min[s].get(minute) for s in self.syms}
        self.sub = 0
        self.minute += 1

    def set_sub(self, sub: int) -> None:
        self.sub = sub

    def _key(self, symbol: str) -> str:
        return self.by_norm[norm(symbol)]

    def price(self, symbol: str) -> float:
        s = self._key(symbol)
        b = self.cur.get(s)
        if b is None:
            return self.last_close.get(s, 0.0)
        up = b.c >= b.o
        path = (b.o, b.l, b.h, b.c) if up else (b.o, b.h, b.l, b.c)
        return path[self.sub]

    def has_price(self, symbol: str) -> bool:
        return self.price(symbol) > 0

    def bars(self, symbols_: list[str], timeframe: str, limit: int) -> dict[str, list[Bar]]:
        return {s: self.history[self._key(s)][-limit:] for s in symbols_}

    def quotes(self, symbols_: list[str]) -> dict[str, dict]:
        out = {}
        for sym in symbols_:
            mid = self.price(sym)
            if mid > 0:
                half = self.spread_pct[self._key(sym)] / 200
                out[sym] = {"bid": mid * (1 - half), "ask": mid * (1 + half)}
        return out

    def market_open(self) -> bool:
        return self.sub == 0  # köp bara på minutens öppning

    def step_time(self) -> datetime:
        return self.now + timedelta(seconds=15 * self.sub)


# ---------------------------------------------------------------- körning
def run_backtest(base_cfg: dict, *, days: float, profile: str, leverage: float, capital: float,
                 data: dict[str, list[Bar]], spreads: dict[str, float], end: datetime,
                 fee_pct: float | None = None,
                 progress: Callable[[float, str], None] | None = None) -> dict:
    cfg = apply_profile(base_cfg, profile)
    cfg["sizing"]["leverage"] = leverage
    if fee_pct is not None:
        cfg["strategy"]["fee_pct"] = fee_pct
    syms = symbols(cfg)
    data = {s: data.get(s, []) for s in syms}
    start = end - timedelta(days=days)
    broker = ReplayBroker(data, spreads, capital, cfg["strategy"].get("fee_pct", 0.25), leverage)
    tmp = tempfile.mkdtemp(prefix="backtest-")
    risk = RiskManager(cfg["safety"], tmp)
    risk.state.first_paper_run_at = start.isoformat()
    # Egen tyst logger: tusentals ENTRY/EXIT-rader ska inte dränka den riktiga botens logg.
    quiet = logging.getLogger("backtest")
    quiet.propagate = False
    quiet.setLevel(logging.ERROR)
    eng = Engine(cfg, broker, risk, persist=False, logger=quiet)
    equity, halted_at = [], None
    total = int((end - (start - timedelta(minutes=WARMUP_MIN))).total_seconds() // 60)
    minute = (start - timedelta(minutes=WARMUP_MIN)).replace(second=0, microsecond=0)
    i = 0
    while minute < end:
        broker.set_minute(minute)
        if minute >= start:
            for sub in range(broker.SUBSTEPS):
                broker.set_sub(sub)
                if not eng.step(broker.step_time()):
                    halted_at = broker.step_time().isoformat()
                    break
            if halted_at:
                break
            if i % 15 == 0:
                equity.append({"t": minute.isoformat(), "v": round(broker.account()["equity"], 2)})
        i += 1
        if progress and i % 240 == 0:
            progress(i / total, f"Spelar upp {minute.astimezone().strftime('%d %b %H:%M')}")
        minute += timedelta(minutes=1)

    acct = broker.account()
    equity.append({"t": (halted_at or end.isoformat()), "v": round(acct["equity"], 2)})
    closed = [t for t in eng.journal_mem if "pnl" in t]
    return {
        "profile": profile, "leverage": leverage, "days": days, "capital": capital,
        "start": start.isoformat(), "end": end.isoformat(),
        "symbols": syms, "bars": {s: len(v) for s, v in data.items()},
        "spreads": {s: round(broker.spread_pct[s], 4) for s in syms},
        "fee_pct": cfg["strategy"].get("fee_pct", 0.25),
        "halted": risk.state.halted, "halt_reason": risk.state.halt_reason, "halted_at": halted_at,
        "liquidations": broker.liquidations,
        "open_positions_at_end": len(broker.pos),
        "stats": stats(closed, equity, capital, acct["equity"], days),
        "equity": equity,
        "trades": closed[-500:],
        "per_symbol": per_symbol(closed),
    }


def stats(closed: list[dict], equity: list[dict], capital: float, final: float, days: float) -> dict:
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [t["pnl"] for t in closed if t["pnl"] <= 0]
    gl = -sum(losses)
    peak, mdd = capital, 0.0
    for p in equity:
        peak = max(peak, p["v"])
        mdd = max(mdd, (1 - p["v"] / peak) * 100 if peak else 0)
    reasons: dict[str, int] = {}
    for t in closed:
        reasons[t.get("exit_reason", "?")] = reasons.get(t.get("exit_reason", "?"), 0) + 1
    holds = []
    for t in closed:
        try:
            holds.append((_ts(t["closed_at"]) - _ts(t["opened_at"])).total_seconds() / 60)
        except (KeyError, ValueError):
            pass
    return {
        "return_pct": (final / capital - 1) * 100,
        "pnl": final - capital,
        "final_equity": final,
        "trades": len(closed),
        "trades_per_day": len(closed) / days if days else 0,
        "win_rate": len(wins) / len(closed) * 100 if closed else None,
        "profit_factor": sum(wins) / gl if gl > 0 else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "expectancy": sum(t["pnl"] for t in closed) / len(closed) if closed else None,
        "max_drawdown_pct": mdd,
        "avg_hold_min": sum(holds) / len(holds) if holds else None,
        "exit_reasons": reasons,
    }


def per_symbol(closed: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for t in closed:
        a = agg.setdefault(t["symbol"], {"symbol": t["symbol"], "trades": 0, "wins": 0, "pnl": 0.0})
        a["trades"] += 1
        a["wins"] += t["pnl"] > 0
        a["pnl"] += t["pnl"]
    return sorted(agg.values(), key=lambda a: a["pnl"], reverse=True)


# ---------------------------------------------------------------- jobb i bakgrunden
class BacktestRunner:
    """Kör en backtest i taget i en bakgrundstråd och sparar resultaten i state/backtests."""

    def __init__(self, base_cfg: dict, state_root: Path):
        self.base_cfg = base_cfg
        self.dir = state_root / "backtests"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache = BarCache(state_root / "cache")
        self.lock = threading.Lock()
        self.job: dict | None = None

    def start(self, *, days: float, profiles: list[str], leverage: float, capital: float,
              spread_mode: str, fixed_spread: float = 0.1, fee_pct: float | None = None) -> dict:
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise RuntimeError("En backtest körs redan.")
            if not 0.5 <= days <= 30:
                raise ValueError("Välj mellan 0,5 och 30 dagar.")
            if capital < 100:
                raise ValueError("Startkapitalet måste vara minst 100 $.")
            if not profiles:
                raise ValueError("Välj minst ett tempo.")
            known = set(self.base_cfg.get("profiles", {})) | {"normal"}
            if any(p not in known for p in profiles):
                raise ValueError("Okänt tempo.")
            self.job = {"id": uuid.uuid4().hex[:8], "status": "running", "progress": 0.0,
                        "stage": "Förbereder…", "results": [], "error": None}
            job = self.job
        threading.Thread(target=self._run, args=(job, days, profiles, leverage, capital, spread_mode, fixed_spread,
                                                 fee_pct), daemon=True).start()
        return {"id": job["id"]}

    def _run(self, job, days, profiles, leverage, capital, spread_mode, fixed_spread, fee_pct):
        try:
            all_syms: list[str] = []
            for p in profiles:
                for s in symbols(apply_profile(self.base_cfg, p)):
                    if s not in all_syms:
                        all_syms.append(s)
            end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            start = end - timedelta(days=days) - timedelta(minutes=WARMUP_MIN)

            def stage(msg):
                job["stage"] = msg
            data = self.cache.load(all_syms, start, end, fetch_bars, progress=stage)
            if not any(data.values()):
                raise RuntimeError("Fick ingen historik från Alpaca för perioden.")
            if spread_mode == "fixed":
                spreads = {s: fixed_spread for s in all_syms}
            else:
                stage("Hämtar aktuell spread…")
                try:
                    spreads = fetch_spreads(all_syms)
                except Exception as e:
                    log.warning("Kunde inte hämta spread (%s) – använder 0,1 %%", e)
                    spreads = {}
                spreads = {s: max(spreads.get(s, 0.1), 0.02) for s in all_syms}

            for k, prof in enumerate(profiles):
                def prog(frac, msg, k=k):
                    job["progress"] = (k + frac) / len(profiles)
                    job["stage"] = f"{prof}: {msg}"
                res = run_backtest(self.base_cfg, days=days, profile=prof, leverage=leverage, capital=capital,
                                   data=data, spreads=spreads, end=end, fee_pct=fee_pct, progress=prog)
                res["id"] = f"{job['id']}-{prof}"
                res["created_at"] = datetime.now(timezone.utc).isoformat()
                (self.dir / f"{res['id']}.json").write_text(json.dumps(res))
                job["results"].append(res)
            job["progress"] = 1.0
            job["stage"] = "Klar"
            job["status"] = "done"
        except Exception as e:
            log.exception("Backtest misslyckades")
            job["status"] = "error"
            job["error"] = str(e)

    def history(self, limit: int = 20) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                r = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            out.append({k: r[k] for k in ("id", "profile", "leverage", "days", "capital", "created_at",
                                           "halted", "liquidations", "fee_pct", "start", "end") if k in r}
                       | {"stats": r["stats"], "avg_spread": sum(r["spreads"].values()) / max(len(r["spreads"]), 1)})
        return out

    def get(self, rid: str) -> dict:
        f = self.dir / f"{Path(rid).name}.json"
        if not f.exists():
            raise ValueError("Okänd backtest.")
        return json.loads(f.read_text())

    def status(self) -> dict:
        j = self.job
        if not j:
            return {"status": "idle"}
        return {k: j[k] for k in ("id", "status", "progress", "stage", "error")} | {
            "result_ids": [r["id"] for r in j["results"]]}
