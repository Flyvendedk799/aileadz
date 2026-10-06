"""When does an ordered course start and end, and has it taken place?

Pure module (no Flask, no DB). It is the ONE answer to those questions for every
order surface: the learner page, HR and vendor consoles, the completion guards in
``order_service`` / ``order_fulfillment`` and the follow-up scheduler.

Sources, in order of trust:

1. the booking's ``start_at`` / ``end_at`` (``course_order_details.booking_json``,
   ISO with offset or a plain date), written when the order is booked;
2. ``course_orders.variant_date``, the human label from the catalogue
   ("3. december 2026"), parsed with ``calendar_service.parse_danish_date``.

Contract:

* All results are timezone-aware datetimes in Europe/Copenhagen. A value without
  an offset is read as Copenhagen wall time; a date without a time is midnight
  (start) or the last instant of that day (end).
* ``course_start`` / ``course_end`` return ``None`` when nothing usable is known.
* ``has_taken_place`` is ``False`` for an order with no known date, and ``True``
  only once the end (or, without an end, the end of the start day) is past.
* ``now()`` is the single clock; tests freeze time by monkeypatching it.

``format_date`` is the one Danish date renderer behind the ``dkdate`` filter.
"""

from __future__ import annotations

import datetime as _dt
import re
from zoneinfo import ZoneInfo

__all__ = ["course_start", "course_end", "has_taken_place", "course_label", "format_date", "now"]

TZ = ZoneInfo("Europe/Copenhagen")

_MONTHS_DA = (
    "januar", "februar", "marts", "april", "maj", "juni",
    "juli", "august", "september", "oktober", "november", "december",
)

# "3.-4. december 2026" / "3 - 4 dec 2026": a same-month range keeps its last day.
_RANGE_RE = re.compile(r"^\s*(\d{1,2})\.?\s*[-–]\s*(\d{1,2})\.?\s+(?=[A-Za-zæøåÆØÅ])(.*)$")


def now():
    """Current time in Copenhagen. The one clock of this module (patch in tests)."""
    return _dt.datetime.now(TZ)


def _end_of_day(day):
    return _dt.datetime.combine(day, _dt.time(23, 59, 59, 999999), tzinfo=TZ)


def _start_of_day(day):
    return _dt.datetime.combine(day, _dt.time(0, 0), tzinfo=TZ)


def _parse(value):
    """Return ``(datetime | None, has_time)`` for an ISO timestamp, date or Danish text."""
    if value is None:
        return None, False
    if isinstance(value, _dt.datetime):
        return (value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)), True
    if isinstance(value, _dt.date):
        return _start_of_day(value), False
    if not isinstance(value, str):
        return None, False
    text = value.strip()
    if not text:
        return None, False
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
        try:
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return (parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)), True
    from calendar_service import parse_danish_date

    day = parse_danish_date(text)
    if day is None:
        return None, False
    return _start_of_day(day), False


def _booking_of(row, booking):
    if isinstance(booking, dict):
        return booking
    candidate = (row or {}).get("booking_json") if isinstance(row, dict) else None
    if isinstance(candidate, dict):
        return candidate
    if isinstance(candidate, str) and candidate.strip():
        import json

        try:
            loaded = json.loads(candidate)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def course_start(row, booking=None):
    """Start of the course as a Copenhagen datetime, or ``None`` when unknown."""
    data = _booking_of(row, booking)
    parsed, _ = _parse(data.get("start_at"))
    if parsed is not None:
        return parsed
    label = (row or {}).get("variant_date") if isinstance(row, dict) else None
    parsed, _ = _parse(label)
    return parsed


def course_end(row, booking=None):
    """Explicit end of the course, or ``None`` when no end is known.

    A booked ``end_at`` without a time means the end of that day; a same-month
    range in ``variant_date`` ("3.-4. december 2026") ends on its last day.
    """
    data = _booking_of(row, booking)
    parsed, has_time = _parse(data.get("end_at"))
    if parsed is not None:
        return parsed if has_time else _end_of_day(parsed.date())
    if _parse(data.get("start_at"))[0] is not None:
        return None  # the booking fixed the start; the label's range no longer applies
    label = (row or {}).get("variant_date") if isinstance(row, dict) else None
    if isinstance(label, str):
        match = _RANGE_RE.match(label)
        if match:
            parsed, _ = _parse("%s %s" % (match.group(2), match.group(3)))
            if parsed is not None:
                return _end_of_day(parsed.date())
    return None


def has_taken_place(row, booking=None, now=None):
    """True once the course is over; ``False`` when its date is unknown."""
    start = course_start(row, booking)
    if start is None:
        return False
    end = course_end(row, booking)
    if end is None or end < start:
        end = _end_of_day(start.date())
    if now is None:
        now = globals()["now"]()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    return now > end


def format_date(value, style="long", with_time=False):
    """Danish date text. ``long`` = "3. december 2026", ``short`` = "03.12.2026".

    ``with_time`` appends " kl. 09.00", but only when the value carries a time
    (a bare date or a midnight timestamp has none worth showing). Aware values are
    shown in Copenhagen time. Empty input gives "", unparseable input is returned
    unchanged.
    """
    if value is None or value == "":
        return ""
    parsed, has_time = _parse(value)
    if parsed is None:
        return value
    if style == "short":
        text = "%02d.%02d.%d" % (parsed.day, parsed.month, parsed.year)
    else:
        text = "%d. %s %d" % (parsed.day, _MONTHS_DA[parsed.month - 1], parsed.year)
    if with_time and has_time and (parsed.hour or parsed.minute):
        text += " kl. %02d.%02d" % (parsed.hour, parsed.minute)
    return text


def course_label(row, booking=None, with_time=True, style="long"):
    """The course date for display: formatted when known, else the raw label or ""."""
    start = course_start(row, booking)
    if start is not None:
        data = _booking_of(row, booking)
        shown = start if _parse(data.get("start_at"))[0] is not None else start.date()
        return format_date(shown, style=style, with_time=with_time)
    label = (row or {}).get("variant_date") if isinstance(row, dict) else ""
    return str(label or "")
