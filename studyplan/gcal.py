"""Normalize Google Calendar events into the plan's `events` list.

The CLI never talks to Google: the agent has the Google Calendar MCP connected and pipes what it got into
`studyplan plan import-events -`. This module is the deterministic half — it takes the MCP payload (or a bare
list of Google Calendar API event resources, which is the same shape) and turns it into plan events, so the
parsing is testable offline like the rest of the CLI.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from .plan import Event, to_hhmm

# Event types that are noise on a study board: where you are working from, and whose birthday it is.
SKIP_TYPES = {"WORKING_LOCATION", "BIRTHDAY"}
DAY = 24 * 60


def event_id(gcal_id: str) -> str:
    """A short, stable, DOM-safe id derived from Google's own event id, so re-importing updates in place."""
    return "ev-" + hashlib.sha1(gcal_id.encode()).hexdigest()[:10]


def _zone(name: str | None):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                       # no tzdata on this machine: fall back to the offsets as sent
        return None


def _wall(stamp: str, zone) -> dt.datetime:
    """The wall-clock time to show. Convert into `zone` when we have one, else trust the offset as sent."""
    moment = dt.datetime.fromisoformat(stamp)
    if zone is not None and moment.tzinfo is not None:
        moment = moment.astimezone(zone)
    return moment.replace(tzinfo=None)


def _declined(event: dict[str, Any]) -> bool:
    return any(a.get("self") and a.get("responseStatus") == "declined" for a in event.get("attendees") or [])


def parse_mcp_events(payload: Any, date: str, *, timezone: str | None = None,
                     calendar: str = "") -> tuple[list[Event], list[str]]:
    """Google Calendar MCP output -> (events on `date`, warnings). Anything unparseable becomes a warning."""
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    if isinstance(payload, list):
        payload = {"events": payload}
    if not isinstance(payload, dict):
        raise ValueError("expected the MCP result object or a list of events")
    raw = payload.get("events") or payload.get("items") or []
    zone = _zone(timezone or payload.get("timeZone"))
    label = calendar or str(payload.get("summary") or "")
    day = dt.date.fromisoformat(date)
    events: list[Event] = []
    warnings: list[str] = []

    for item in raw:
        title = str(item.get("summary") or "(no title)")
        try:
            if item.get("status") == "cancelled" or item.get("eventType") in SKIP_TYPES or _declined(item):
                continue
            start, end = item.get("start") or {}, item.get("end") or {}
            if start.get("date"):                                    # all-day: [start.date, end.date)
                first = dt.date.fromisoformat(start["date"])
                last = dt.date.fromisoformat(end.get("date") or start["date"]) - dt.timedelta(days=1)
                if not first <= day <= last:
                    continue
                begin, length, all_day = 0, DAY, True
            else:
                if not start.get("dateTime"):
                    warnings.append(f"skipped {title!r}: no start time")
                    continue
                a = _wall(start["dateTime"], zone)
                b = _wall(end.get("dateTime") or start["dateTime"], zone)
                # clip to the day so an overnight event still shows as the part that lands on it
                lo = max(a, dt.datetime.combine(day, dt.time.min))
                hi = min(b, dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min))
                if hi <= lo:
                    continue
                begin = lo.hour * 60 + lo.minute
                length = max(int((hi - lo).total_seconds() // 60), 5)
                all_day = False
        except (ValueError, TypeError, AttributeError) as e:
            warnings.append(f"skipped {title!r}: {type(e).__name__}: {e}")
            continue
        gcal_id = str(item.get("id") or f"{title}@{date}")
        events.append(Event(
            id=event_id(gcal_id), title=title, start=to_hhmm(begin), minutes=min(length, DAY - begin),
            calendar=label, url=str(item.get("htmlLink") or ""), location=str(item.get("location") or ""),
            all_day=all_day, busy=item.get("transparency") != "transparent",   # all-day is forced non-busy by Event
            gcal_id=gcal_id, source="gcal"))
    events.sort(key=lambda e: (not e.all_day, e.start, e.title))
    return events, warnings
