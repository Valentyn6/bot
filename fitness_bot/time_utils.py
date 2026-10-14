from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


try:
    APP_TIMEZONE = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:  # Windows installations may not ship a system tz database.
    from dateutil.tz import gettz

    APP_TIMEZONE = gettz("Europe/Kyiv")
    if APP_TIMEZONE is None:
        raise RuntimeError("Часовий пояс Europe/Kyiv недоступний; встановіть tzdata.")


def local_now() -> datetime:
    return datetime.now(APP_TIMEZONE)


def local_today() -> date:
    return local_now().date()


def local_date_range_utc_naive(start_date: str, end_date: str) -> tuple[str, str]:
    """Return a half-open UTC range for local calendar dates.

    The database stores transaction timestamps as naive host-local timestamps;
    on the Google Cloud VM the host timezone is UTC.
    """
    start = datetime.combine(date.fromisoformat(start_date), time.min, APP_TIMEZONE)
    end = datetime.combine(date.fromisoformat(end_date) + timedelta(days=1), time.min, APP_TIMEZONE)
    utc = timezone.utc
    start_utc = start.astimezone(utc).replace(tzinfo=None).isoformat(timespec="seconds")
    end_utc = end.astimezone(utc).replace(tzinfo=None).isoformat(timespec="seconds")
    return start_utc, end_utc
