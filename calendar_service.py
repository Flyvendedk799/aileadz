"""Shared cross-channel calendar helper (pure, stdlib-only, never raises).

Produces RFC 5545 VCALENDAR/VEVENT (.ics) strings the employee ``add_to_calendar``
tool can hand back to the user in any channel (chat, email, download).

Design constraints:
- No Flask app context, no DB, no third-party deps — only ``datetime`` from stdlib.
- Boot-safe: importing this module can never crash ``create_app``.
- Defensive: every public helper returns a safe value ("" / None) on bad input
  instead of raising, so a malformed model argument can't break the tool loop.

Public API:
- ``parse_danish_date(s)``           -> ``date`` | ``None`` (best-effort)
- ``build_ics(*, title, start, ...)`` -> ``str`` (valid .ics, or "" on bad input)
"""

from __future__ import annotations

from datetime import date, datetime, timezone

__all__ = ["build_ics", "build_ics_feed", "parse_danish_date"]


# Danish month names (full + common 3-letter abbreviations), lowercase keys.
_DANISH_MONTHS = {
    "januar": 1, "jan": 1,
    "februar": 2, "feb": 2,
    "marts": 3, "mar": 3,
    "april": 4, "apr": 4,
    "maj": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def parse_danish_date(s):
    """Best-effort parse of a date string into a ``datetime.date``.

    Accepts (in order of attempt):
    - ``date`` / ``datetime`` passed straight through.
    - ISO-ish forms: ``2026-02-17``, ``2026-02-17T09:00``, ``2026/02/17``.
    - Danish prose: ``17. februar 2026``, ``17 feb 2026``, ``17. feb. 2026``.
    - Danish numeric: ``17-02-2026``, ``17/02/2026``, ``17.02.2026``.

    Returns a ``date`` on success, or ``None`` on anything it can't read.
    Never raises.
    """
    try:
        if isinstance(s, datetime):
            return s.date()
        if isinstance(s, date):
            return s
        if not isinstance(s, str):
            return None
        raw = s.strip()
        if not raw:
            return None

        # ISO with optional time component -> keep the date part.
        iso_candidate = raw.replace("/", "-")
        head = iso_candidate.split("T")[0].split(" ")[0]
        parts = head.split("-")
        if len(parts) == 3 and parts[0].isdigit() and len(parts[0]) == 4:
            try:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
            except (ValueError, TypeError):
                pass

        # Tokenise prose / numeric Danish forms.
        cleaned = (
            raw.lower()
            .replace(".", " ")
            .replace(",", " ")
            .replace("/", " ")
            .replace("-", " ")
        )
        tokens = [t for t in cleaned.split() if t]

        day = month = year = None
        for tok in tokens:
            if tok in _DANISH_MONTHS:
                month = _DANISH_MONTHS[tok]
                continue
            if tok.isdigit():
                num = int(tok)
                if len(tok) == 4 and year is None:
                    year = num
                elif day is None and 1 <= num <= 31:
                    day = num
                elif month is None and 1 <= num <= 12:
                    month = num
                elif year is None:
                    year = num

        if day and month and year:
            if year < 100:  # two-digit year -> assume 2000s
                year += 2000
            try:
                return date(year, month, day)
            except (ValueError, TypeError):
                return None
        return None
    except Exception:
        return None


def _to_dt(value):
    """Coerce a value into ``(datetime|date, has_time)``.

    Returns ``(obj, has_time)`` where ``obj`` is a ``datetime`` (timed) or a
    ``date`` (all-day), or ``(None, False)`` if unparseable.
    """
    try:
        if isinstance(value, datetime):
            return value, True
        if isinstance(value, date):
            return value, False
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None, False
            # Try full ISO datetime first (preserves time -> timed event).
            iso = raw.replace("/", "-")
            for candidate in (iso, iso.replace(" ", "T")):
                try:
                    dt = datetime.fromisoformat(candidate)
                    return dt, True
                except (ValueError, TypeError):
                    continue
            d = parse_danish_date(raw)
            if d is not None:
                return d, False
        return None, False
    except Exception:
        return None, False


def _fmt_utc(dt):
    """Format a ``datetime`` as a UTC stamp: ``YYYYMMDDTHHMMSSZ``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _fmt_date(d):
    """Format a ``date`` as ``YYYYMMDD`` (all-day VALUE=DATE)."""
    return d.strftime("%Y%m%d")


def _esc(text):
    """Escape text per RFC 5545 3.3.11 (backslash, comma, semicolon, newline)."""
    if text is None:
        return ""
    out = str(text)
    out = out.replace("\\", "\\\\")
    out = out.replace("\n", "\\n").replace("\r", "")
    out = out.replace(",", "\\,").replace(";", "\\;")
    return out


def _fold(line):
    """Fold a content line to <=75 octets per RFC 5545 3.1 (best-effort, ASCII bytes)."""
    try:
        data = line.encode("utf-8")
    except Exception:
        return line
    if len(data) <= 75:
        return line
    chunks = []
    # First chunk 75 bytes, continuation chunks 74 (1 byte for leading space).
    chunks.append(data[:75])
    rest = data[75:]
    while rest:
        chunks.append(b" " + rest[:74])
        rest = rest[74:]
    try:
        return "\r\n".join(c.decode("utf-8", "ignore") for c in chunks)
    except Exception:
        return line


def _uid(stamp, title):
    """Deterministic-ish UID from stamp + title hash; stable, no external deps."""
    base = "{}-{}".format(stamp, abs(hash(title)) % (10 ** 10))
    return "{}@aileadz".format(base)


def _vevent_lines(*, title, start, end=None, location="", description="", url="",
                  dtstamp=None, uid=None):
    """Build the VEVENT block (list of content lines) for one event.

    Returns a list of lines, or ``[]`` if title/start are unusable. Shared by
    ``build_ics`` (single event) and ``build_ics_feed`` (many events). Never
    raises.
    """
    try:
        if not title or not str(title).strip():
            return []
        start_obj, start_timed = _to_dt(start)
        if start_obj is None:
            return []

        if dtstamp is None:
            dtstamp = _fmt_utc(datetime.now(timezone.utc))
        if uid is None:
            uid = _uid(dtstamp, str(title))

        lines = [
            "BEGIN:VEVENT",
            "UID:" + uid,
            "DTSTAMP:" + dtstamp,
        ]

        if start_timed:
            lines.append("DTSTART:" + _fmt_utc(start_obj))
            end_obj, end_timed = _to_dt(end) if end is not None else (None, False)
            if end_obj is not None and end_timed:
                lines.append("DTEND:" + _fmt_utc(end_obj))
            elif end_obj is not None and not end_timed:
                lines.append(
                    "DTEND:" + _fmt_utc(datetime(end_obj.year, end_obj.month, end_obj.day, tzinfo=timezone.utc))
                )
        else:
            lines.append("DTSTART;VALUE=DATE:" + _fmt_date(start_obj))
            end_obj, end_timed = _to_dt(end) if end is not None else (None, False)
            if end_obj is not None:
                end_date = end_obj.date() if isinstance(end_obj, datetime) else end_obj
                lines.append("DTEND;VALUE=DATE:" + _fmt_date(end_date))
            else:
                try:
                    from datetime import timedelta
                    lines.append("DTEND;VALUE=DATE:" + _fmt_date(start_obj + timedelta(days=1)))
                except Exception:
                    pass

        lines.append("SUMMARY:" + _esc(title))
        if location:
            lines.append("LOCATION:" + _esc(location))
        if description:
            lines.append("DESCRIPTION:" + _esc(description))
        if url:
            lines.append("URL:" + _esc(url))
        lines.append("END:VEVENT")
        return lines
    except Exception:
        return []


def build_ics(*, title, start, end=None, location="", description="", url=""):
    """Build a valid VCALENDAR/VEVENT ``.ics`` string. Never raises.

    Args:
        title:       Event summary (required, non-empty).
        start:       Danish date string, ISO string, ``date`` or ``datetime``.
                     A bare date (no time) yields an all-day event.
        end:         Optional end; same accepted forms as ``start``.
        location:    Optional location text.
        description: Optional description text.
        url:         Optional URL.

    Returns:
        A CRLF-delimited ``.ics`` string, or ``""`` on bad/empty input.
    """
    try:
        vevent = _vevent_lines(
            title=title, start=start, end=end, location=location,
            description=description, url=url,
        )
        if not vevent:
            return ""

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//aileadz//calendar_service//DA",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
        ]
        lines.extend(vevent)
        lines.append("END:VCALENDAR")

        folded = [_fold(ln) for ln in lines]
        return "\r\n".join(folded) + "\r\n"
    except Exception:
        return ""


def build_ics_feed(events, *, cal_name=""):
    """Build a multi-event VCALENDAR ``.ics`` feed. Never raises.

    Args:
        events: iterable of dicts, each with keys matching ``build_ics``:
                ``title`` (required), ``start`` (required), and optional
                ``end`` / ``location`` / ``description`` / ``url`` / ``uid``.
        cal_name: optional X-WR-CALNAME for the subscribed feed.

    Returns:
        A CRLF-delimited ``.ics`` string. An empty/invalid events list yields a
        valid but empty VCALENDAR (so a subscribed feed never 500s).
    """
    try:
        dtstamp = _fmt_utc(datetime.now(timezone.utc))
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//aileadz//calendar_service//DA",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
        ]
        if cal_name:
            lines.append("X-WR-CALNAME:" + _esc(cal_name))

        seq = 0
        for ev in (events or []):
            if not isinstance(ev, dict):
                continue
            seq += 1
            uid = ev.get("uid") or _uid("%s-%d" % (dtstamp, seq), str(ev.get("title") or ""))
            vevent = _vevent_lines(
                title=ev.get("title"),
                start=ev.get("start"),
                end=ev.get("end"),
                location=ev.get("location", "") or "",
                description=ev.get("description", "") or "",
                url=ev.get("url", "") or "",
                dtstamp=dtstamp,
                uid=uid,
            )
            if vevent:
                lines.extend(vevent)

        lines.append("END:VCALENDAR")
        folded = [_fold(ln) for ln in lines]
        return "\r\n".join(folded) + "\r\n"
    except Exception:
        # Last-resort: a minimal valid empty calendar.
        return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
                "PRODID:-//aileadz//calendar_service//DA\r\nEND:VCALENDAR\r\n")


# --- Quick self-test (runs only when executed directly) ----------------------
if __name__ == "__main__":
    timed = build_ics(
        title="Kursusstart: Lederskab",
        start="2026-02-17T09:00",
        end="2026-02-17T11:00",
        location="København",
        description="Intro til lederuddannelsen",
        url="https://aileadz.example/course/1",
    )
    assert "BEGIN:VEVENT" in timed, "missing VEVENT"
    assert "Kursusstart" in timed, "missing title"
    assert "DTSTART:" in timed and "DTEND:" in timed, "missing timed start/end"

    allday = build_ics(title="Frist for tilmelding", start="17. februar 2026")
    assert "BEGIN:VEVENT" in allday and "Frist" in allday
    assert "VALUE=DATE:20260217" in allday, "all-day date not detected"

    assert build_ics(title="", start="2026-02-17") == "", "empty title should yield ''"
    assert build_ics(title="x", start="ikke en dato") == "", "bad date should yield ''"

    assert parse_danish_date("17. februar 2026") == date(2026, 2, 17)
    assert parse_danish_date("2026-02-17") == date(2026, 2, 17)
    assert parse_danish_date("17/02/2026") == date(2026, 2, 17)
    assert parse_danish_date("17 feb 2026") == date(2026, 2, 17)
    assert parse_danish_date("vrøvl") is None

    feed = build_ics_feed(
        [
            {"title": "Kursusstart: Lederskab", "start": "2026-02-17T09:00",
             "end": "2026-02-17T11:00", "location": "København"},
            {"title": "Frist for tilmelding", "start": "17. februar 2026"},
            {"title": "", "start": "2026-02-17"},  # invalid -> skipped
        ],
        cal_name="Mine kurser",
    )
    assert feed.count("BEGIN:VEVENT") == 2, "feed should contain exactly 2 events"
    assert "X-WR-CALNAME:Mine kurser" in feed, "feed should carry the calendar name"
    assert build_ics_feed([]).startswith("BEGIN:VCALENDAR"), "empty feed must still be valid"

    print("calendar_service self-test OK")
    print(allday)
