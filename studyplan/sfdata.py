"""Read-only view of the sf-events sub-project's data: which events the user marked going / went, and when they are.

`../sf-events/data/feedback.jsonl` holds the verdicts (append-only, latest wins) and `../sf-events/data/events/<date>.json`
the snapshots with the event details. Nothing here imports sfevents: sub-projects never import each other.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

ATTENDING = ("going", "went")


def snapshots(parent: Path) -> dict[str, dict[str, Any]]:
    """Every event sf-events has seen, the latest snapshot winning per id."""
    seen: dict[str, dict[str, Any]] = {}
    d = Path(parent) / "sf-events" / "data" / "events"
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            doc = json.loads(path.read_text())
        except ValueError:
            continue
        for e in doc.get("events") or []:
            if e.get("id"):
                seen[str(e["id"])] = e
    return seen


def verdicts(parent: Path) -> dict[str, dict[str, Any]]:
    """Latest verdict record per event id."""
    path = Path(parent) / "sf-events" / "data" / "feedback.jsonl"
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    for line in path.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("verdict") and rec.get("event_id"):
                latest[str(rec["event_id"])] = rec
    return latest


def marked(parent: Path, date: str | None = None, which: tuple[str, ...] = ATTENDING) -> list[dict[str, Any]]:
    """Events with one of the `which` verdicts, joined with their snapshot; on `date` only when given."""
    seen = snapshots(parent)
    out: list[dict[str, Any]] = []
    for eid, rec in verdicts(parent).items():
        if rec.get("verdict") not in which:
            continue
        ev = seen.get(eid)
        if not ev or not ev.get("date") or (date is not None and ev.get("date") != date):
            continue
        out.append({**ev, "verdict": rec["verdict"], "verdict_note": rec.get("note", ""), "marked_on": str(rec.get("ts", ""))[:10]})
    out.sort(key=lambda e: (e.get("date") or "", e.get("starts_at") or "", e.get("title") or ""))
    return out


def went_yesterday(parent: Path, today: str) -> list[dict[str, Any]]:
    """The events the user attended the day before `today`: each one owes a debrief."""
    yesterday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    return marked(parent, yesterday, ("went",))
