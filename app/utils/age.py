"""Age calculation — shared across parent/child/account validation paths."""
from datetime import datetime, timezone


def completed_years(dob: datetime, today: datetime) -> int:
    """Age in completed years — birthday-aware.

    Someone born March 1 is not 18 on Feb 28. We compute year delta and subtract one
    if the birthday hasn't occurred yet this year.
    """
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


def aware_utc(dt: datetime) -> datetime:
    """Coerce a naive datetime to UTC-aware. No-op if already tz-aware."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
