"""Google Calendar's secret iCal address -> the same plan events the MCP import produces.

Read-only and credential-free: the "Secret address in iCal format" (Calendar settings -> Integrate calendar) is a
plain HTTPS feed, so the board can refresh itself without an agent and without OAuth. This module is the pure
parser (no network, tested against `tests/fixtures/ical/`); `busy.py` fetches the feeds and merges the result.

It mirrors `sfevents/adapters/ical.py` in approach (line unfolding, TZID handling) but is written here on purpose:
sub-projects never import each other. Output feeds the SAME `plan.events` shape as `gcal.parse_mcp_events`, with
ids derived from the event UID the way Google derives its own ids, so a day may carry both sources and a
re-import updates in place instead of duplicating.

Recurrence: `RRULE` with `FREQ=DAILY` and `FREQ=WEEKLY` (INTERVAL, COUNT, UNTIL, BYDAY) is expanded for the
requested date, honouring `EXDATE` and `RECURRENCE-ID` overrides. Monthly and yearly rules show their master
occurrence only, with a warning; that limit is deliberate (the board is one day at a time).
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .gcal import event_id
from .plan import Event, to_hhmm

DEFAULT_TZ = "America/Los_Angeles"
DAY = 24 * 60
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_MAX_ITER = 5000                     # a daily rule five thousand steps old is not worth expanding further


def unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding: a line starting with a space or tab continues the previous one."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def _zone(name: str | None):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name.strip('"'))
    except Exception:                       # unknown TZID or no tzdata: the caller falls back
        return None


def parse_components(text: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """The VCALENDAR header properties and one dict per VEVENT: NAME -> list of (value, params)."""
    header: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    depth = 0                                 # nested components (VALARM) are skipped, not parsed
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur, depth = {}, 0
            continue
        if line == "END:VEVENT" and cur is not None:
            events.append(cur)
            cur = None
            continue
        if cur is not None and line.startswith("BEGIN:"):
            depth += 1
            continue
        if cur is not None and line.startswith("END:"):
            depth = max(0, depth - 1)
            continue
        if ":" not in line or depth:
            continue
        head, _, value = line.partition(":")
        name, *plist = head.split(";")
        params: dict[str, str] = {}
        for p in plist:
            k, _, v = p.partition("=")
            params[k.upper()] = v
        if cur is None:
            if name.upper().startswith("X-WR-") or name.upper() in ("PRODID", "VERSION"):
                header[name.upper()] = value
            continue
        cur.setdefault(name.upper(), []).append((value, params))
    return header, events


# ---------------------------------------------------------------- times

def parse_stamp(value: str, params: dict[str, str], display_zone) -> tuple[dt.datetime | dt.date, bool]:
    """A DTSTART/DTEND/EXDATE/RECURRENCE-ID value -> (aware datetime | date, all_day).

    `Z` is UTC; `TZID` names the zone; a floating time is read in the display zone (the user's own).
    """
    v = value.strip()
    if params.get("VALUE") == "DATE" or (len(v) == 8 and v.isdigit()):
        return dt.datetime.strptime(v, "%Y%m%d").date(), True
    if v.endswith("Z"):
        return dt.datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc), False
    zone = _zone(params.get("TZID")) or display_zone
    return dt.datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=zone), False


def _parse_rrule(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in value.split(";"):
        k, _, v = part.partition("=")
        if k:
            out[k.upper()] = v
    return out


def _stamps(fields: dict[str, Any], name: str, display_zone) -> list[dt.datetime | dt.date]:
    out: list[dt.datetime | dt.date] = []
    for value, params in fields.get(name, []):
        for piece in value.split(","):
            if piece.strip():
                out.append(parse_stamp(piece, params, display_zone)[0])
    return out


def _key(stamp: dt.datetime | dt.date) -> str:
    """One comparable string per instant: a date, or the UTC minute of an aware datetime."""
    if isinstance(stamp, dt.datetime):
        return stamp.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return stamp.strftime("%Y%m%d")


def occurrences(fields: dict[str, Any], day: dt.date, display_zone,
                warnings: list[str] | None = None) -> list[tuple[dt.datetime | dt.date, dt.datetime | dt.date]]:
    """Every (start, end) of this VEVENT that could touch `day` (the day before counts: overnight events).

    A plain event yields itself once; a DAILY / WEEKLY rule is expanded; other rules yield the master only.
    """
    start, all_day = parse_stamp(*fields["DTSTART"][0], display_zone)
    if "DTEND" in fields:
        end, _ = parse_stamp(*fields["DTEND"][0], display_zone)
    elif "DURATION" in fields:
        end = start + _duration(fields["DURATION"][0][0])
    else:
        end = start + dt.timedelta(days=1) if all_day else start
    length = end - start
    rule = _parse_rrule(fields["RRULE"][0][0]) if "RRULE" in fields else {}
    freq = rule.get("FREQ", "").upper()
    if not rule:
        return [(start, end)]
    title = unescape(fields.get("SUMMARY", [("", {})])[0][0])
    if freq not in ("DAILY", "WEEKLY"):
        if warnings is not None:
            warnings.append(f"{title!r}: {freq.lower() or 'unknown'} recurrence is not expanded; showing its first occurrence only")
        return [(start, end)]

    interval = max(int(rule.get("INTERVAL") or 1), 1)
    count = int(rule["COUNT"]) if rule.get("COUNT") else None
    until = parse_stamp(rule["UNTIL"], {}, display_zone)[0] if rule.get("UNTIL") else None
    exdates = {_key(x) for x in _stamps(fields, "EXDATE", display_zone)}
    horizon = day                                                 # nothing starting after `day` can touch it

    def to_date(s: dt.datetime | dt.date) -> dt.date:
        return s.astimezone(display_zone).date() if isinstance(s, dt.datetime) else s

    def past_until(s: dt.datetime | dt.date) -> bool:
        if until is None:
            return False
        if isinstance(until, dt.datetime) and isinstance(s, dt.datetime):
            return s > until
        return to_date(s) > to_date(until)

    out: list[tuple[dt.datetime | dt.date, dt.datetime | dt.date]] = []
    produced = 0
    if freq == "DAILY":
        step = dt.timedelta(days=interval)
        cur = start
        for _ in range(_MAX_ITER):
            if past_until(cur) or (count is not None and produced >= count) or to_date(cur) > horizon:
                break
            produced += 1
            if _key(cur) not in exdates and to_date(cur) >= day - dt.timedelta(days=1):
                out.append((cur, cur + length))
            cur = cur + step
        return out

    bydays = sorted({WEEKDAYS[d.strip()[-2:]] for d in rule.get("BYDAY", "").split(",") if d.strip()[-2:] in WEEKDAYS}) \
        or [to_date(start).weekday()]
    week_start = start - dt.timedelta(days=to_date(start).weekday())    # the Monday of DTSTART's week, same clock
    for k in range(_MAX_ITER):
        base = week_start + dt.timedelta(weeks=k * interval)
        if to_date(base) > horizon:
            break
        for wd in bydays:
            cur = base + dt.timedelta(days=wd)
            if to_date(cur) < to_date(start):
                continue
            if past_until(cur) or (count is not None and produced >= count):
                return out
            produced += 1
            if _key(cur) not in exdates and day - dt.timedelta(days=1) <= to_date(cur) <= horizon:
                out.append((cur, cur + length))
    return out


def _duration(value: str) -> dt.timedelta:
    m = re.match(r"^(-)?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", value.strip())
    if not m:
        return dt.timedelta(0)
    sign = -1 if m.group(1) else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * dt.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


# ---------------------------------------------------------------- the day's events

def normalize_uid(uid: str) -> str:
    """Google's iCal UIDs are `<event id>@google.com`; its API ids are the bare part. Use the bare part so the
    MCP path and the feed path derive the same board id for the same event."""
    uid = uid.strip()
    return uid[: -len("@google.com")] if uid.endswith("@google.com") else uid


def _declined(fields: dict[str, Any], self_email: str) -> bool:
    if not self_email:
        return False
    me = self_email.lower()
    for value, params in fields.get("ATTENDEE", []):
        if value.lower().replace("mailto:", "") == me and params.get("PARTSTAT", "").upper() == "DECLINED":
            return True
    return False


def parse_ical(text: str, date: str, *, timezone: str | None = None, calendar: str = "",
               self_email: str = "") -> tuple[list[Event], list[str]]:
    """iCalendar text -> (plan events on `date`, warnings). Unparseable entries warn, never crash."""
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("not an iCalendar feed (no BEGIN:VCALENDAR)")
    display_zone = _zone(timezone or DEFAULT_TZ) or dt.timezone.utc
    header, vevents = parse_components(text)
    label = calendar or unescape(header.get("X-WR-CALNAME", ""))
    day = dt.date.fromisoformat(date)
    warnings: list[str] = []

    # overrides (RECURRENCE-ID) replace the master's occurrence at that instant
    overridden: dict[str, set[str]] = {}
    for f in vevents:
        if "RECURRENCE-ID" in f and "UID" in f:
            rid = parse_stamp(*f["RECURRENCE-ID"][0], display_zone)[0]
            overridden.setdefault(f["UID"][0][0].strip(), set()).add(_key(rid))

    events: list[Event] = []
    for f in vevents:
        title = unescape(f.get("SUMMARY", [("(no title)", {})])[0][0]).strip() or "(no title)"
        try:
            if "DTSTART" not in f or "UID" not in f:
                warnings.append(f"skipped {title!r}: no DTSTART or UID")
                continue
            status = f.get("STATUS", [("", {})])[0][0].upper()
            if status == "CANCELLED" or _declined(f, self_email):
                continue
            raw_uid = f["UID"][0][0].strip()
            uid = normalize_uid(raw_uid)
            recurring = "RRULE" in f
            skip = overridden.get(raw_uid, set()) if recurring else set()
            for start, end in occurrences(f, day, display_zone, warnings):
                if recurring and _key(start) in skip:
                    continue
                if isinstance(start, dt.date) and not isinstance(start, dt.datetime):     # all-day: [start, end)
                    last = (end if isinstance(end, dt.date) else end.date()) - dt.timedelta(days=1)
                    if not start <= day <= last:
                        continue
                    begin, length, all_day = 0, DAY, True
                    instance = start.strftime("%Y%m%d")
                else:
                    a = start.astimezone(display_zone).replace(tzinfo=None)
                    b = end.astimezone(display_zone).replace(tzinfo=None) if isinstance(end, dt.datetime) else a
                    lo = max(a, dt.datetime.combine(day, dt.time.min))
                    hi = min(b, dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min))
                    if hi <= lo:
                        continue
                    begin = lo.hour * 60 + lo.minute
                    length = max(int((hi - lo).total_seconds() // 60), 5)
                    all_day = False
                    instance = _key(start)
                if "RECURRENCE-ID" in f:                                     # Google keys a moved instance by its
                    instance = _key(parse_stamp(*f["RECURRENCE-ID"][0], display_zone)[0])   # original start
                gcal_id = f"{uid}_{instance}" if recurring or "RECURRENCE-ID" in f else uid
                events.append(Event(
                    id=event_id(gcal_id), title=title, start=to_hhmm(begin), minutes=min(length, DAY - begin),
                    calendar=label, url=f.get("URL", [("", {})])[0][0].strip(),
                    location=unescape(f.get("LOCATION", [("", {})])[0][0]).strip(), all_day=all_day,
                    busy=f.get("TRANSP", [("OPAQUE", {})])[0][0].upper() != "TRANSPARENT",
                    gcal_id=gcal_id, source="ical"))
        except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as e:
            warnings.append(f"skipped {title!r}: {type(e).__name__}: {e}")
    events.sort(key=lambda e: (not e.all_day, e.start, e.title))
    return events, warnings
