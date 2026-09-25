"""Busy time from outside the plan, refreshed without an agent.

Two read-only sources feed `plan.events` next to the MCP import:

- the calendar's secret iCal address(es): `STUDYPLAN_ICAL_URLS` (comma-separated) in the repo-root `.env` or the
  environment; parsed by `ical.py`. A feed that fails to load warns once and the last successful copy in
  `data/calendar-cache.json` (gitignored) is used instead. No feed configured: nothing happens, silently.
- sf-events items the user marked `going` (or `went`) for the day, from `../sf-events/data/feedback.jsonl` and the
  event snapshots under `../sf-events/data/events/`, as busy entries with `source: "sf-events"` and the event URL.

Both produce the same `Event` shape and are merged by id, so a re-fetch updates in place. `refresh` re-lays the
study blocks only when a busy range actually changed and the user has not arranged the day by hand (`plan.arranged`,
the persistent form of `--keep-times`). Nothing is ever written to Google or to sf-events.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shlex
from pathlib import Path
from typing import Any

from .config import load_level
from .gcal import event_id
from .ical import DEFAULT_TZ, parse_ical
from .sfdata import marked
from .plan import Event, Plan, day_slots, place_blocks, shrink_to_free, to_hhmm

ENV_URLS = "STUDYPLAN_ICAL_URLS"
ENV_TZ = "STUDYPLAN_TIMEZONE"
ENV_SELF = "STUDYPLAN_SELF_EMAIL"
CACHE = "calendar-cache.json"
REFRESHED_SOURCES = ("ical", "sf-events")      # what a refresh replaces; "gcal" and "manual" entries are kept
GOING = ("going", "went")
DEFAULT_EVENT_MINUTES = 120                    # sf-events' own default when an event has no end time


# ---------------------------------------------------------------- configuration

def read_dotenv(path: Path) -> dict[str, str]:
    """KEY=value lines (quotes allowed, `#` comments ignored). Never mutates the environment."""
    out: dict[str, str] = {}
    path = Path(path)
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            out[key.strip()] = " ".join(shlex.split(value.strip())) if value.strip() else ""
        except ValueError:
            out[key.strip()] = value.strip()
    return out


def setting(parent: Path, key: str, default: str = "") -> str:
    """The environment first, then the repo-root `.env` (the file jobradar's own passphrase lives in)."""
    if os.environ.get(key):
        return os.environ[key]
    return read_dotenv(Path(parent) / ".env").get(key, default)


def ical_urls(parent: Path) -> list[str]:
    return [u.strip() for u in setting(parent, ENV_URLS).split(",") if u.strip()]


# ---------------------------------------------------------------- fetching, with the cache as fallback

def _http_get(url: str, timeout: float = 15.0) -> str:
    import httpx
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "studyplan/0.1"}) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def load_cache(root: Path) -> dict[str, Any]:
    path = Path(root) / "data" / CACHE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {}


def save_cache(root: Path, cache: dict[str, Any]) -> None:
    path = Path(root) / "data" / CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1) + "\n")


def fetch_feed(url: str, cache: dict[str, Any], *, now: str | None = None) -> tuple[str | None, str | None]:
    """(feed text, warning). A local path or file:// URL is read directly (fixtures, tests, a downloaded copy)."""
    stamp = now or dt.datetime.now().replace(microsecond=0).isoformat()
    try:
        if url.startswith("file://"):
            text = Path(url[len("file://"):]).read_text()
        elif url.startswith(("http://", "https://")):
            text = _http_get(url)
        else:
            text = Path(url).read_text()
        if "BEGIN:VCALENDAR" not in text:
            raise ValueError("not an iCalendar feed")
        cache[url] = {"fetched": stamp, "text": text}
        return text, None
    except Exception as e:                                        # noqa: BLE001 - one warning, then the cache
        cached = cache.get(url)
        label = _short(url)
        if cached:
            return cached["text"], f"calendar {label}: {type(e).__name__}: {e}; using the copy fetched {cached['fetched']}"
        return None, f"calendar {label}: {type(e).__name__}: {e}; no cached copy yet"


def _short(url: str) -> str:
    """Never echo a secret address in full: the host and the last path piece are enough to recognise it."""
    if "://" in url:
        host = url.split("://", 1)[1].split("/", 1)[0]
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        return f"{host}/…/{tail[:12]}" if tail and tail != host else host
    return Path(url).name


def calendar_events(root: Path, parent: Path, date: str, *, urls: list[str] | None = None,
                    now: str | None = None) -> tuple[list[Event], list[str]]:
    """The day's events from every configured feed (empty and silent when none is configured)."""
    urls = ical_urls(parent) if urls is None else urls
    if not urls:
        return [], []
    cache = load_cache(root)
    zone = setting(parent, ENV_TZ, DEFAULT_TZ)
    me = setting(parent, ENV_SELF)
    events: list[Event] = []
    warnings: list[str] = []
    for i, url in enumerate(urls, 1):
        text, warning = fetch_feed(url, cache, now=now)
        if warning:
            warnings.append(warning)
        if text is None:
            continue
        try:
            found, warns = parse_ical(text, date, timezone=zone, self_email=me)
        except ValueError as e:
            warnings.append(f"calendar {_short(url)}: {e}")
            continue
        for e in found:
            e.calendar = e.calendar or f"calendar {i}"
        events.extend(found)
        warnings.extend(f"calendar {_short(url)}: {w}" for w in warns)
    save_cache(root, cache)
    return events, warnings


# ---------------------------------------------------------------- sf-events: what the user is going to

def going_events(parent: Path, date: str, *, timezone: str | None = None) -> list[Event]:
    """sf-events `going` / `went` items dated `date` as busy entries (all-day chips when the time is unknown)."""
    zone = None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(timezone or setting(parent, ENV_TZ, DEFAULT_TZ))
    except Exception:                                             # noqa: BLE001
        zone = None
    out: list[Event] = []
    for ev in marked(parent, date, GOING):
        eid = str(ev["id"])
        title = str(ev.get("title") or eid)
        where = ", ".join(x for x in (ev.get("venue"), ev.get("address")) if x)
        start_iso, end_iso = ev.get("starts_at"), ev.get("ends_at")
        try:
            begin = dt.datetime.fromisoformat(start_iso) if start_iso else None
            end = dt.datetime.fromisoformat(end_iso) if end_iso else None
        except ValueError:
            begin = end = None
        if begin is not None and zone is not None and begin.tzinfo is not None:
            begin = begin.astimezone(zone)
            end = end.astimezone(zone) if end is not None and end.tzinfo is not None else end
        unknown_time = begin is None or (begin.hour == 0 and begin.minute == 0 and end is None)
        if unknown_time:
            start_min, minutes, all_day = 0, 24 * 60, True
        else:
            start_min = begin.hour * 60 + begin.minute
            minutes = int((end - begin).total_seconds() // 60) if end is not None and end > begin else DEFAULT_EVENT_MINUTES
            minutes = max(5, min(minutes, 24 * 60 - start_min))
            all_day = False
        out.append(Event(id=event_id(f"sf-events:{eid}"), title=title, start=to_hhmm(start_min), minutes=minutes,
                         calendar="sf-events", url=str(ev.get("url") or ""), location=where, all_day=all_day,
                         busy=True, gcal_id="", source="sf-events"))
    out.sort(key=lambda e: (not e.all_day, e.start, e.title))
    return out


# ---------------------------------------------------------------- merging and refreshing

def merge_events(existing: list[Event], fresh: list[Event], replace: tuple[str, ...] = REFRESHED_SOURCES) -> list[Event]:
    """Drop the old entries of the refreshed sources, then merge by key so a re-fetch updates in place."""
    kept = [e for e in existing if e.source not in replace]
    by_key = {e.gcal_id or e.id: e for e in kept}
    for e in fresh:
        by_key[e.gcal_id or e.id] = e
    return sorted(by_key.values(), key=lambda e: (not e.all_day, e.start, e.title))


def outside_events(root: Path, parent: Path, date: str, *, urls: list[str] | None = None,
                   now: str | None = None) -> tuple[list[Event], list[str]]:
    """Everything a refresh brings in: the feeds plus sf-events."""
    events, warnings = calendar_events(root, parent, date, urls=urls, now=now)
    return events + going_events(parent, date), warnings


def refresh(root: Path, parent: Path, plan: Plan, *, urls: list[str] | None = None,
            now: str | None = None) -> tuple[bool, list[str]]:
    """Re-fetch the outside sources into `plan`. Returns (busy ranges changed, warnings).

    The study blocks are re-laid only when a busy range actually changed AND the day was not arranged by hand.
    """
    fresh, warnings = outside_events(root, parent, plan.date, urls=urls, now=now)
    before_busy, before_events = plan.busy, sorted((e.id, e.start, e.minutes, e.busy, e.title) for e in plan.events)
    plan.events = merge_events(plan.events, fresh)
    after_events = sorted((e.id, e.start, e.minutes, e.busy, e.title) for e in plan.events)
    changed = plan.busy != before_busy
    if changed and not plan.arranged:
        place_blocks(plan.blocks, day_slots(load_level(root)), plan.busy)
    return changed or after_events != before_events, warnings
