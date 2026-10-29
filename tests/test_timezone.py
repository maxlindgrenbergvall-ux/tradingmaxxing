from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.risk import _USEastern


def test_fallback_matches_tz_database():
    # Reservzonen används på Windows om tzdata saknas – måste ge samma börsdatum.
    f, z = _USEastern(), ZoneInfo("America/New_York")
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    while t < datetime(2028, 1, 1, tzinfo=timezone.utc):
        assert t.astimezone(f).replace(tzinfo=None) == t.astimezone(z).replace(tzinfo=None), t
        t += timedelta(minutes=30)
