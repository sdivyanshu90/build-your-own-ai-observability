"""Time handling.

Distributed tracing is a timestamp-comparison problem, so time deserves its own
module rather than scattered ``datetime.utcnow()`` calls.

Rules enforced here:

* **Everything is UTC and timezone-aware.** Naive datetimes are rejected at
  construction, not silently interpreted as local time.
* **Wire timestamps are integer nanoseconds.** Floats lose precision above
  ~2^53 nanoseconds (roughly 104 days after the epoch in ns resolution), which
  is exactly the range trace timestamps live in.
* **The clock is injectable.** Every component takes a :class:`Clock`, so tests
  can advance time deterministically instead of sleeping.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Final

__all__ = [
    "Clock",
    "FrozenClock",
    "SystemClock",
    "datetime_to_unix_nano",
    "duration_ms",
    "ensure_utc",
    "parse_rfc3339",
    "to_rfc3339",
    "unix_nano_to_datetime",
    "utcnow",
]

NANOS_PER_SECOND: Final = 1_000_000_000
NANOS_PER_MILLI: Final = 1_000_000
#: Sanity bounds for ingested timestamps: 2000-01-01 .. 2100-01-01 in ns.
MIN_PLAUSIBLE_UNIX_NANO: Final = 946_684_800 * NANOS_PER_SECOND
MAX_PLAUSIBLE_UNIX_NANO: Final = 4_102_444_800 * NANOS_PER_SECOND


class Clock(ABC):
    """Source of the current time.

    Injected rather than called globally so that retention sweeps, token
    expiry, rate-limit windows and trace-completion timeouts can all be tested
    without ``sleep``.
    """

    @abstractmethod
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""

    def now_unix_nano(self) -> int:
        """Return the current time as integer Unix nanoseconds."""
        return datetime_to_unix_nano(self.now())

    def now_unix_seconds(self) -> float:
        return self.now().timestamp()


class SystemClock(Clock):
    """The real wall clock."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock(Clock):
    """A clock that only moves when a test tells it to."""

    __slots__ = ("_now",)

    def __init__(self, start: datetime | None = None) -> None:
        self._now = ensure_utc(start) if start else datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0.0, **kwargs: float) -> datetime:
        """Move the clock forward and return the new time."""
        self._now = self._now + timedelta(seconds=seconds, **kwargs)
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = ensure_utc(moment)


def utcnow() -> datetime:
    """Current UTC time. Prefer an injected :class:`Clock` in testable code."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime, rejecting naive input.

    Naive datetimes are rejected rather than assumed-UTC because the two most
    common producers (a developer's laptop and a container with ``TZ`` unset)
    disagree, and a silent multi-hour offset in a trace timeline is far worse
    than a loud error at the boundary.
    """
    if value.tzinfo is None:
        raise ValueError(
            "naive datetime rejected: attach a timezone explicitly "
            "(datetime.now(timezone.utc), not datetime.now())"
        )
    return value.astimezone(timezone.utc)


def datetime_to_unix_nano(value: datetime) -> int:
    """Convert an aware datetime to integer Unix nanoseconds."""
    aware = ensure_utc(value)
    seconds = int(aware.timestamp())
    return seconds * NANOS_PER_SECOND + aware.microsecond * 1_000


def unix_nano_to_datetime(value: int) -> datetime:
    """Convert integer Unix nanoseconds to an aware UTC datetime.

    Microsecond precision is the best ``datetime`` can carry; the original
    nanosecond value is retained separately wherever sub-microsecond ordering
    matters (span start/end are stored as int64 nanoseconds in ClickHouse).
    """
    seconds, remainder = divmod(int(value), NANOS_PER_SECOND)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=remainder // 1_000)


def is_plausible_unix_nano(value: int) -> bool:
    """Whether ``value`` falls inside the sane range for a trace timestamp.

    Catches the classic unit mix-ups: seconds (~1.7e9) and milliseconds
    (~1.7e12) both fall far below the nanosecond floor, so a producer sending
    the wrong unit is rejected instead of writing spans dated 1970.
    """
    return MIN_PLAUSIBLE_UNIX_NANO <= value <= MAX_PLAUSIBLE_UNIX_NANO


def duration_ms(start_unix_nano: int, end_unix_nano: int) -> float:
    """Return the duration between two nanosecond timestamps in milliseconds."""
    return (end_unix_nano - start_unix_nano) / NANOS_PER_MILLI


def to_rfc3339(value: datetime) -> str:
    """Serialise a datetime as RFC 3339 with millisecond precision and a Z suffix."""
    aware = ensure_utc(value)
    return aware.strftime("%Y-%m-%dT%H:%M:%S.") + f"{aware.microsecond // 1000:03d}Z"


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC 3339 timestamp, accepting both ``Z`` and numeric offsets."""
    text = value.strip()
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {value!r} is missing a UTC offset")
    return parsed.astimezone(timezone.utc)
