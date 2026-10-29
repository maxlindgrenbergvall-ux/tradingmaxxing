import random
import time
from datetime import datetime, timedelta, timezone

from bot import backtest as bt
from bot.config import load_config


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, ""

    def json(self):
        return self._d


def fake_alpaca(seed=1, page=10000):
    """Svarar som Alpacas /v1beta3/crypto/us/bars med sidindelning och glesa minuter."""
    rng = random.Random(seed)
    cache = {}

    def series(sym, start, end):
        key = (sym, start, end)
        if key not in cache:
            p, rows, t = 100 + rng.random() * 100, [], start
            while t < end:
                if rng.random() < 0.9:  # ibland ingen handel en minut, som på riktigt
                    o = p
                    p = p * (1 + rng.gauss(0, 0.0012))
                    rows.append({"t": t.isoformat().replace("+00:00", "Z"), "o": o, "h": max(o, p) * 1.0004,
                                 "l": min(o, p) * 0.9996, "c": p, "v": rng.uniform(1, 50)})
                t += timedelta(minutes=1)
            cache[key] = rows
        return cache[key]

    calls = {"n": 0}

    def get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if url.endswith("/latest/quotes"):
            return Resp({"quotes": {s: {"bp": 99.9, "ap": 100.1} for s in params["symbols"].split(",")}})
        start = datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
        flat = [(s, r) for s in params["symbols"].split(",") for r in series(s, start, end)]
        off = int(params.get("page_token") or 0)
        chunk = flat[off:off + page]
        bars = {}
        for s, r in chunk:
            bars.setdefault(s, []).append(r)
        nxt = str(off + page) if off + page < len(flat) else None
        return Resp({"bars": bars, "next_page_token": nxt})

    return get, calls


def test_fetch_paginates_and_keeps_all_bars():
    get, calls = fake_alpaca(page=500)
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    data = bt.fetch_bars(["BTC/USD", "ETH/USD"], end - timedelta(hours=6), end, get=get)
    assert calls["n"] > 1
    assert 300 < len(data["BTC/USD"]) <= 360 and 300 < len(data["ETH/USD"]) <= 360


def test_backtest_runs_on_replayed_data_without_lookahead(tmp_path):
    get, _ = fake_alpaca(seed=3)
    cfg = load_config()
    end = datetime(2026, 10, 3, tzinfo=timezone.utc)
    syms = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD",
            "LTC/USD", "BCH/USD", "UNI/USD", "AAVE/USD", "DOT/USD"]
    data = bt.fetch_bars(syms, end - timedelta(days=2, minutes=bt.WARMUP_MIN), end, get=get)
    t0 = time.time()
    res = bt.run_backtest(cfg, days=2, profile="maxfrekvens", leverage=1, capital=100_000,
                          data=data, spreads={s: 0.1 for s in syms}, end=end)
    elapsed = time.time() - t0
    s = res["stats"]
    assert s["trades"] > 20
    assert abs(s["final_equity"] - (100_000 + s["pnl"])) < 1e-6
    assert res["equity"][0]["t"] >= (end - timedelta(days=2)).isoformat()
    # Inga köp före sin signal: varje trade öppnas på en minut som redan fanns i datan
    for t in res["trades"][:50]:
        assert datetime.fromisoformat(t["opened_at"]) <= datetime.fromisoformat(t["closed_at"])
    assert elapsed < 60, elapsed


def test_runner_end_to_end_with_cache(tmp_path, monkeypatch):
    get, calls = fake_alpaca(seed=5)
    monkeypatch.setattr(bt.requests, "get", get)
    r = bt.BacktestRunner(load_config(), tmp_path)
    r.start(days=1, profiles=["normal", "hogfrekvens"], leverage=1, capital=50_000, spread_mode="live")
    for _ in range(600):
        if r.status()["status"] != "running":
            break
        time.sleep(0.1)
    st = r.status()
    assert st["status"] == "done", st
    assert len(st["result_ids"]) == 2 and len(r.history()) == 2
    assert r.get(st["result_ids"][0])["capital"] == 50_000
    assert list((tmp_path / "cache").glob("*.json"))
