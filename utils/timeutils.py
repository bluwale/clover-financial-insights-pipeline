"""
Time helpers.

ORIGIN: the epoch-millisecond helpers were migrated from the `sandbox/` Clover API
explorer (exploration code, not production). Kept because Clover stores all timestamps
as Unix epoch milliseconds. The analytics layer wants local store time, so we convert
at the ETL boundary — never at query time (ProjectSummary §3.4).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from settings import STORE_TIMEZONE


@lru_cache(maxsize=None)
def _local_tz() -> ZoneInfo:
    # Lazy so a missing tzdata package fails only when local conversion is actually used.
    return ZoneInfo(STORE_TIMEZONE)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def days_ago_ms(n: int) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=n)).timestamp() * 1000)


def day_window(days: int = 7) -> tuple[int, int]:
    """(start_ms, end_ms) for the last n days — Clover requires a window on /orders, /payments."""
    return days_ago_ms(days), now_ms()


def ms_to_iso_utc(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def to_local_iso(ms: int | None) -> str | None:
    """Epoch ms → ISO string in the store's local timezone (ProjectSummary §3.4)."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(_local_tz()).isoformat()


def ms_to_human(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
