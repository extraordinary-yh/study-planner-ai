"""The completion log: one line per finished block, data/log.jsonl."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

BLOCKS = ("news", "stack", "design", "leetcode", "stories", "debrief")


def append(path: Path, entry: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def net(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop every row an `{"undo": true, "target": <id>}` row reverses, and the undo rows themselves.

    The board never rewrites history: unticking appends an undo row naming the `target` it reverses
    (a block id, or a `<block>-<n>` item id). An undo cancels the most recent still-standing row for that
    target on that date, so tick / untick / tick again nets to one row. `load` still returns the raw file.
    """
    kept: list[dict[str, Any]] = []
    for e in entries:
        if not e.get("undo"):
            kept.append(e)
            continue
        target = e.get("target")
        for i in range(len(kept) - 1, -1, -1):
            if kept[i].get("target") == target and kept[i].get("date") == e.get("date"):
                del kept[i]
                break
    return kept


def done_targets(entries: list[dict[str, Any]], date: str) -> set[str]:
    """The block and item ids ticked on `date` after undo rows are netted out."""
    return {str(e["target"]) for e in net(entries) if e.get("date") == date and e.get("target")}


def week_summary(entries: list[dict[str, Any]], today: str, days: int = 7) -> dict[str, Any]:
    entries = net(entries)
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=days - 1)).isoformat()
    recent = [e for e in entries if start <= e.get("date", "") <= today]
    minutes: dict[str, int] = {b: 0 for b in BLOCKS}
    for e in recent:
        minutes[e.get("block", "other")] = minutes.get(e.get("block", "other"), 0) + int(e.get("minutes") or 0)
    active_days = sorted({e["date"] for e in recent})
    streak, d = 0, dt.date.fromisoformat(today)
    days_with = {e["date"] for e in entries}
    while d.isoformat() in days_with:
        streak += 1
        d -= dt.timedelta(days=1)
    return {"minutes": minutes, "active_days": len(active_days), "streak": streak, "total": sum(minutes.values())}
