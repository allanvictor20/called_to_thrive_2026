from datetime import date, datetime

from django.utils.dateparse import parse_date, parse_datetime


def opt_date(value) -> date | None:
    if not value:
        return None
    try:
        return parse_date(str(value))
    except ValueError:
        return None


def opt_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        return parse_datetime(str(value))
    except ValueError:
        return None


def opt_int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
