from datetime import timedelta

import pytest

from bot.broker import SimBroker
from bot.config import load_config, symbols
from bot.engine import Engine
from bot.risk import RiskManager, utcnow
from bot.sizing import position_size
from bot.strategy import Bar, evaluate


@pytest.fixture
def cfg():
    return load_config()


def flat_bars(n=40, price=100.0):
    return [Bar(str(i), price, price * 1.001, price * 0.999, price, 1000) for i in range(n)]


def test_strategy_detects_burst(cfg):
    bars = flat_bars()
    p = 100.0
    for i in range(5):
        o, p = p, p * 1.003
        bars.append(Bar(f"b{i}", o, p * 1.0005, o * 0.9995, p, 3000))
    sig = evaluate("X", bars, cfg["strategy"])
    assert sig is not None
    assert sig.stop < sig.entry < sig.target


def test_strategy_ignores_flat(cfg):
    assert evaluate("X", flat_bars(60), cfg["strategy"]) is None


def test_sizing_respects_risk_and_caps():
    qty = position_size(equity=100_000, buying_power=100_000, entry=100, stop=99,
                        risk_pct=1, max_position_pct=20, fractional=False)
    assert qty == 200  # 20 % tak (20 000 kr) slår 1 % risk (1000 st)
    qty = position_size(equity=100_000, buying_power=100_000, entry=100, stop=90,
                        risk_pct=1, max_position_pct=50, fractional=False)
    assert qty == 100  # 1000 kr risk / 10 kr per aktie


def test_kill_switch_triggers_at_25pct_and_persists(cfg, tmp_path):
    risk = RiskManager(cfg["safety"], tmp_path)
    assert risk.update(100_000) is False
    assert risk.update(75_001) is False
    assert risk.update(75_000) is True
    assert risk.state.halted and (tmp_path / "HALTED").exists()
    # Ny process läser samma state och är fortfarande stoppad
    again = RiskManager(cfg["safety"], tmp_path)
    assert again.state.halted
    assert again.can_open(80_000)[0] is False


def test_engine_flattens_and_stops_on_crash(cfg, tmp_path):
    risk = RiskManager(cfg["safety"], tmp_path)
    broker = SimBroker(symbols(cfg), seed=3)
    eng = Engine(cfg, broker, risk)
    assert eng.step() is True
    broker.buy(symbols(cfg)[0], 100_000 / broker.price(symbols(cfg)[0]) * 0.9)
    broker.shock(-40)
    assert eng.step() is False
    assert broker.positions() == {}
    assert risk.state.halted
    assert eng.step() is False  # förblir stoppad


def test_unlock_requires_reset_when_still_below(cfg, tmp_path):
    risk = RiskManager(cfg["safety"], tmp_path)
    risk.update(100_000)
    risk.update(70_000)
    with pytest.raises(RuntimeError):
        risk.unlock(70_000, reset_baseline=False)
    risk.unlock(70_000, reset_baseline=True)
    assert not risk.state.halted and risk.state.start_equity == 70_000
    assert not (tmp_path / "HALTED").exists()


def test_live_blocked_during_first_week(cfg, tmp_path, monkeypatch):
    risk = RiskManager(cfg["safety"], tmp_path)
    with pytest.raises(RuntimeError):
        risk.check_mode_allowed("live")  # aldrig kört paper
    now = utcnow()
    risk.check_mode_allowed("paper", now)
    with pytest.raises(RuntimeError):
        risk.check_mode_allowed("live", now + timedelta(days=6))
    with pytest.raises(RuntimeError):  # saknar BOT_ALLOW_LIVE
        risk.check_mode_allowed("live", now + timedelta(days=8))
    monkeypatch.setenv("BOT_ALLOW_LIVE", "JA")
    risk.check_mode_allowed("live", now + timedelta(days=8))


def test_daily_loss_blocks_new_trades(cfg, tmp_path):
    risk = RiskManager(cfg["safety"], tmp_path)
    risk.update(100_000)
    risk.update(91_000)
    ok, why = risk.can_open(91_000)
    assert not ok and "Daglig" in why


def test_stale_bars_block_entry(cfg):
    from datetime import datetime, timezone
    from bot.strategy import diagnose
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    bars = []
    p = 100.0
    for i in range(40):
        bars.append(Bar((t0 + timedelta(minutes=i)).isoformat(), p, p * 1.001, p * 0.999, p, 1000))
    for i in range(5):
        o, p = p, p * 1.003
        bars.append(Bar((t0 + timedelta(minutes=40 + i)).isoformat(), o, p * 1.0005, o * 0.9995, p, 3000))
    fresh = diagnose("X", bars, cfg["strategy"], t0 + timedelta(minutes=45))
    stale = diagnose("X", bars, cfg["strategy"], t0 + timedelta(minutes=90))
    assert fresh["signal"] is not None
    assert stale["signal"] is None and stale["checks"]["fresh"] is False
