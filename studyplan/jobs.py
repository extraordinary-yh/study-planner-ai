"""Read-only view of the parent job-radar data: the latest snapshot, applied jobs, interview stages."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

INTERVIEW_STAGES = ("oa", "phone", "onsite")
DONE_STAGES = ("offer", "rejected", "withdrawn")


def latest_snapshot(parent: Path) -> tuple[str, list[dict[str, Any]]]:
    jobs_dir = parent / "data" / "jobs"
    files = sorted(jobs_dir.glob("*.json")) if jobs_dir.exists() else []
    if not files:
        return "", []
    doc = json.loads(files[-1].read_text())
    jobs = doc.get("jobs", doc) if isinstance(doc, dict) else doc
    return files[-1].stem, list(jobs)


def visible(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [j for j in jobs if not j.get("excluded") and not j.get("hidden")]


def job_text(job: dict[str, Any]) -> str:
    parts = [job.get("title", ""), job.get("description_text", ""), job.get("qualifications_min", ""),
             job.get("qualifications_pref", ""), job.get("responsibilities", "")]
    return "\n".join(p for p in parts if p)


def applications(feedback_path: Path) -> dict[str, dict[str, Any]]:
    """Latest application state per job id from feedback.jsonl: {job_id: {stage, stage_on, applied_on}}."""
    out: dict[str, dict[str, Any]] = {}
    if not Path(feedback_path).exists():
        return out
    for line in Path(feedback_path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("kind"):
            continue
        job_id, verdict = str(rec.get("job_id", "")), str(rec.get("verdict", ""))
        day = str(rec.get("ts", ""))[:10]
        if verdict == "applied":
            cur = out.setdefault(job_id, {"stage": "applied", "stage_on": day, "applied_on": day})
            if rec.get("stage"):
                cur["stage"], cur["stage_on"] = str(rec["stage"]).lower(), day
            elif cur["stage"] == "applied":
                cur["stage_on"] = day
        elif verdict in ("bad", "ignore"):
            out.pop(job_id, None)
    return out


def upcoming_interviews(apps: dict[str, dict[str, Any]], jobs: list[dict[str, Any]], today: str,
                        within_days: int, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Jobs at an interview stage set within `within_days`, plus dated entries from level.yaml goals.interview_dates."""
    by_id = {j.get("id"): j for j in jobs}
    t = dt.date.fromisoformat(today)
    out: list[dict[str, Any]] = []
    for job_id, a in apps.items():
        if a["stage"] not in INTERVIEW_STAGES or not a.get("stage_on"):
            continue
        age = (t - dt.date.fromisoformat(a["stage_on"])).days
        if 0 <= age <= within_days:
            j = by_id.get(job_id, {})
            out.append({"job_id": job_id, "company": j.get("company") or job_id.split(":")[0], "stage": a["stage"],
                        "when": a["stage_on"], "title": j.get("title", ""), "days": -age, "dated": False})
    for e in extra or []:
        try:
            when = dt.date.fromisoformat(str(e.get("date")))
        except (TypeError, ValueError):
            continue
        days = (when - t).days
        if 0 <= days <= within_days:
            out.append({"job_id": "", "company": str(e.get("company", "")), "stage": str(e.get("stage", "interview")),
                        "when": when.isoformat(), "title": "", "days": days, "dated": True})
    out.sort(key=lambda r: (not r["dated"], abs(r["days"])))
    return out
