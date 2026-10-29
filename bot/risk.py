"""Riskhantering och nödbroms.

- Nödbroms (kill switch): equity <= startkapital * (1 - 25 %) => HALTED.
  Tillståndet sparas på disk och boten vägrar starta igen förrän någon kört
  `python -m bot unlock` efter manuell granskning.
- Daglig förlustgräns och förlust-streak-paus (från drawdown-circuit-breaker-skillen).
- Live-spärr: live-handel tillåts först efter `paper_only_days` dagar på demokonto.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from datetime import tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class _USEastern(tzinfo):
    """Reserv om tidszonsdatabasen saknas (vanligt på Windows utan paketet tzdata).
    Amerikansk sommartid: andra söndagen i mars 02:00 till första söndagen i november 02:00."""

    @staticmethod
    def _sunday(year: int, month: int, nth: int) -> datetime:
        d = datetime(year, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7 + 7 * (nth - 1))
        return d.replace(hour=2)

    def utcoffset(self, dt):
        return timedelta(hours=-5) + self.dst(dt)

    def dst(self, dt):
        naive = dt.replace(tzinfo=None)
        start, end = self._sunday(dt.year, 3, 2), self._sunday(dt.year, 11, 1)
        return timedelta(hours=1) if start <= naive < end else timedelta(0)

    def tzname(self, dt):
        return "EDT" if self.dst(dt) else "EST"

    def fromutc(self, dt):
        utc = dt.replace(tzinfo=None)
        start = self._sunday(utc.year, 3, 2) + timedelta(hours=5)   # 02:00 EST = 07:00 UTC
        end = self._sunday(utc.year, 11, 1) + timedelta(hours=4)    # 02:00 EDT = 06:00 UTC
        return dt + timedelta(hours=-4 if start <= utc < end else -5)


try:
    ET: tzinfo = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    ET = _USEastern()
LIVE_ENV_FLAG = "BOT_ALLOW_LIVE"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RiskState:
    start_equity: float | None = None
    first_paper_run_at: str | None = None
    halted: bool = False
    halt_reason: str | None = None
    halted_at: str | None = None
    day: str | None = None
    day_start_equity: float | None = None
    losing_streak: int = 0
    cooldown_until: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


class RiskManager:
    def __init__(self, safety: dict[str, Any], state_dir: str | Path):
        self.cfg = safety
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "risk_state.json"
        self.halt_file = self.dir / "HALTED"
        self.state = self._load()

    # ---------- persistens ----------
    def _load(self) -> RiskState:
        if not self.path.exists():
            return RiskState()
        data = json.loads(self.path.read_text())
        return RiskState(**data)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2))
        os.replace(tmp, self.path)

    def _event(self, kind: str, **info: Any) -> None:
        self.state.history.append({"at": utcnow().isoformat(), "event": kind, **info})
        self.state.history = self.state.history[-200:]

    # ---------- start ----------
    def check_mode_allowed(self, mode: str, now: datetime | None = None) -> None:
        """Kastar RuntimeError om live-handel inte är tillåten än."""
        now = now or utcnow()
        if mode == "paper":
            if self.state.first_paper_run_at is None:
                self.state.first_paper_run_at = now.isoformat()
                self._event("first_paper_run")
                self.save()
            return
        days = int(self.cfg.get("paper_only_days", 7))
        first = self.state.first_paper_run_at
        if first is None:
            raise RuntimeError("Live spärrat: boten har aldrig körts mot demokontot.")
        unlock_at = datetime.fromisoformat(first) + timedelta(days=days)
        if now < unlock_at:
            raise RuntimeError(
                f"Live spärrat: demoperioden ({days} dagar) tar slut {unlock_at.isoformat()}."
            )
        if os.environ.get(LIVE_ENV_FLAG) != "JA":
            raise RuntimeError(f"Live spärrat: sätt miljövariabeln {LIVE_ENV_FLAG}=JA för att bekräfta.")

    def init_baseline(self, equity: float) -> None:
        if self.state.start_equity is None:
            self.state.start_equity = equity
            self._event("baseline_set", equity=equity)
            self.save()

    # ---------- löpande kontroll ----------
    @property
    def kill_level(self) -> float:
        pct = float(self.cfg.get("kill_switch_drawdown_pct", 25.0))
        return (self.state.start_equity or 0.0) * (1 - pct / 100)

    def drawdown_pct(self, equity: float) -> float:
        start = self.state.start_equity or equity
        return (1 - equity / start) * 100 if start else 0.0

    def update(self, equity: float, now: datetime | None = None) -> bool:
        """Uppdatera med aktuell equity. Returnerar True om nödbromsen ska lösas ut."""
        now = now or utcnow()
        self.init_baseline(equity)
        today = now.astimezone(ET).date().isoformat()
        if self.state.day != today:
            self.state.day = today
            self.state.day_start_equity = equity
            self.save()
        self._repair_day_baseline(today)
        if not self.state.halted and equity <= self.kill_level:
            self.halt(
                f"Nödbroms: equity {equity:,.2f} har tappat {self.drawdown_pct(equity):.1f} % "
                f"från startkapitalet {self.state.start_equity:,.2f} "
                f"(gräns {self.cfg.get('kill_switch_drawdown_pct', 25.0)} %).",
                now,
            )
            return True
        return False

    def _repair_day_baseline(self, today: str) -> None:
        """Rättar state från äldre versioner, där upplåsning med nytt startkapital inte
        nollställde dagens startvärde (gav t.ex. "Daglig förlustgräns nådd (90 %)")."""
        st = self.state
        if not (st.day_start_equity and st.start_equity and st.day_start_equity > st.start_equity):
            return
        for ev in reversed(st.history):
            if ev.get("event") == "unlocked" and ev.get("reset_baseline"):
                if datetime.fromisoformat(ev["at"]).astimezone(ET).date().isoformat() == today:
                    st.day_start_equity = st.start_equity
                    self._event("day_baseline_repaired", day_start_equity=st.start_equity)
                    self.save()
                return

    def halt(self, reason: str, now: datetime | None = None) -> None:
        now = now or utcnow()
        self.state.halted = True
        self.state.halt_reason = reason
        self.state.halted_at = now.isoformat()
        self._event("halted", reason=reason)
        self.save()
        self.halt_file.write_text(
            f"{now.isoformat()}\n{reason}\n\nGranska kontot och kör sedan:\n"
            "  python -m bot unlock --confirm GRANSKAT [--reset-baseline]\n"
        )

    def can_open(self, equity: float, now: datetime | None = None) -> tuple[bool, str]:
        now = now or utcnow()
        if self.state.halted or self.halt_file.exists():
            return False, "HALTED"
        if self.state.cooldown_until and now < datetime.fromisoformat(self.state.cooldown_until):
            return False, f"COOLDOWN till {self.state.cooldown_until}"
        day_start = self.state.day_start_equity or equity
        day_loss = (1 - equity / day_start) * 100 if day_start else 0.0
        if day_loss >= float(self.cfg.get("max_daily_loss_pct", 8.0)):
            return False, (f"Daglig förlustgräns nådd ({day_loss:.1f} %: från {day_start:,.0f} "
                           f"till {equity:,.0f} sedan midnatt New York-tid)")
        return True, "OK"

    def record_trade(self, pnl: float, now: datetime | None = None) -> None:
        now = now or utcnow()
        if pnl < 0:
            self.state.losing_streak += 1
            if self.state.losing_streak >= int(self.cfg.get("losing_streak_n", 4)):
                until = now + timedelta(minutes=int(self.cfg.get("cooldown_minutes", 60)))
                self.state.cooldown_until = until.isoformat()
                self.state.losing_streak = 0
                self._event("cooldown", until=self.state.cooldown_until)
        else:
            self.state.losing_streak = 0
        self.save()

    # ---------- manuell upplåsning ----------
    def unlock(self, current_equity: float | None, reset_baseline: bool) -> str:
        if not self.state.halted and not self.halt_file.exists():
            return "Boten är inte stoppad."
        if reset_baseline:
            if current_equity is None:
                raise RuntimeError("Kan inte nollställa startkapital utan aktuell equity.")
            self.state.start_equity = current_equity
            # Dagens förlust ska räknas från det nya startkapitalet, inte från före stoppet.
            self.state.day_start_equity = current_equity
        elif current_equity is not None and current_equity <= self.kill_level:
            raise RuntimeError(
                "Kontot ligger fortfarande under nödbromsnivån. Välj att använda nuvarande "
                "kontovärde som nytt startkapital (--reset-baseline) om du medvetet vill fortsätta."
            )
        self.state.halted = False
        self.state.halt_reason = None
        self.state.halted_at = None
        self.state.losing_streak = 0
        self.state.cooldown_until = None
        self._event("unlocked", reset_baseline=reset_baseline, equity=current_equity)
        self.save()
        self.halt_file.unlink(missing_ok=True)
        return f"Upplåst. Startkapital: {self.state.start_equity:,.2f}"
