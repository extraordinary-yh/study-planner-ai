"""System-design syllabus progress: data/progress.json over config/curriculum.yaml."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_progress(path: Path) -> dict[str, Any]:
    if Path(path).exists():
        return json.loads(Path(path).read_text())
    return {"lessons": {}}


def save_progress(path: Path, progress: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(progress, indent=1, ensure_ascii=False) + "\n")


def flat_lessons(modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in modules:
        for lesson in m.get("lessons", []):
            out.append({**lesson, "module_id": m["id"], "module": m["title"]})
    return out


def next_lessons(modules: list[dict[str, Any]], progress: dict[str, Any], n: int = 1) -> list[dict[str, Any]]:
    done = progress.get("lessons", {})
    return [lesson for lesson in flat_lessons(modules) if lesson["id"] not in done][:n]


def mark_done(progress: dict[str, Any], lesson_id: str, today: str, minutes: int | None, note: str) -> None:
    progress.setdefault("lessons", {})[lesson_id] = {"done_on": today, "minutes": minutes, "note": note}


def summary(modules: list[dict[str, Any]], progress: dict[str, Any]) -> str:
    done = progress.get("lessons", {})
    lines = []
    for m in modules:
        lessons = m.get("lessons", [])
        n_done = sum(1 for lesson in lessons if lesson["id"] in done)
        lines.append(f"{m['id']:<4} {m['title']:<45} {n_done}/{len(lessons)}")
        for lesson in lessons:
            mark = "x" if lesson["id"] in done else " "
            lines.append(f"  [{mark}] {lesson['id']:<6} {lesson['title']} ({lesson.get('minutes', 45)} min)")
    return "\n".join(lines)
