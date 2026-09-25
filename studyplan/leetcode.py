"""LeetCode history: tolerant import, spaced-repetition due list, per-topic weakness."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from pathlib import Path
from typing import Any

OUTCOMES = ("clean", "slow", "hint", "solution", "gave_up")
DIFFICULTIES = ("easy", "medium", "hard")

# LeetCode tag names -> the topic keys used in profile/level.yaml
TOPIC_MAP = {
    "array": "arrays_hashing", "hash table": "arrays_hashing", "hashing": "arrays_hashing", "string": "arrays_hashing",
    "prefix sum": "arrays_hashing", "matrix": "arrays_hashing", "counting": "arrays_hashing",
    "two pointers": "two_pointers_sliding_window", "sliding window": "two_pointers_sliding_window",
    "binary search": "binary_search",
    "stack": "stack_monotonic", "monotonic stack": "stack_monotonic", "monotonic queue": "stack_monotonic", "queue": "stack_monotonic",
    "linked list": "linked_list",
    "tree": "trees_bfs_dfs", "binary tree": "trees_bfs_dfs", "binary search tree": "trees_bfs_dfs",
    "depth-first search": "trees_bfs_dfs", "breadth-first search": "trees_bfs_dfs", "dfs": "trees_bfs_dfs", "bfs": "trees_bfs_dfs",
    "recursion": "trees_bfs_dfs", "divide and conquer": "trees_bfs_dfs",
    "heap (priority queue)": "heaps", "heap": "heaps", "priority queue": "heaps",
    "graph": "graphs_topo_union_find", "topological sort": "graphs_topo_union_find", "union find": "graphs_topo_union_find",
    "shortest path": "graphs_topo_union_find", "minimum spanning tree": "graphs_topo_union_find",
    "dynamic programming": "dynamic_programming", "dp": "dynamic_programming", "memoization": "dynamic_programming",
    "backtracking": "backtracking",
    "greedy": "intervals_greedy", "intervals": "intervals_greedy", "sorting": "intervals_greedy", "interval": "intervals_greedy",
    "trie": "tries",
    "bit manipulation": "bit_manipulation", "bitmask": "bit_manipulation",
    "design": "design", "shortest path": "graphs_topo_union_find", "counting": "arrays_hashing",
}
# Tags that say nothing about an interview topic (LeetCode's SQL / JS tracks, niche structures, catch-all labels).
IGNORED_TAGS = {"math", "database", "javascript", "interactive", "reservoir sampling", "rolling hash", "segment tree",
                "binary indexed tree", "ordered set", "string matching", "simulation", "geometry", "enumeration",
                "iterator", "brainteaser", "number theory", "game theory", "randomized", "hash function", "line sweep"}
_COLS = {
    "number": ("number", "num", "id", "no", "#", "problem id", "question id", "frontend id"),
    "problem": ("problem", "title", "name", "question", "question title"),
    "slug": ("slug", "title slug", "titleslug", "question slug"),
    "url": ("url", "link", "href"),
    "difficulty": ("difficulty", "level"),
    "topics": ("topics", "tags", "topic", "tag", "category", "categories", "topic tags"),
    "date": ("date", "day", "solved_at", "solved at", "timestamp", "submitted", "attempted"),
    "minutes": ("minutes", "time", "duration", "time (min)", "mins", "time_minutes"),
    "stuck": ("stuck", "got stuck", "blocked"),
    "outcome": ("outcome", "result", "status", "verdict"),
    "notes": ("notes", "note", "comment", "comments"),
}
_TRUE = ("1", "true", "yes", "y", "stuck")
_SLUG_RE = re.compile(r"leetcode\.com/problems/([a-z0-9-]+)")


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def topic_key(tag: str) -> str | None:
    """LeetCode tag -> level.yaml topic key; None for tags that carry no interview-topic signal."""
    t = tag.strip().lower()
    if t in IGNORED_TAGS:
        return None
    return TOPIC_MAP.get(t, re.sub(r"[^a-z0-9]+", "_", t).strip("_"))


def topic_keys(tags) -> list[str]:
    keys = [topic_key(t) for t in tags]
    return sorted({k for k in keys if k})


def _pick(row: dict[str, Any], field: str) -> Any:
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for name in _COLS[field]:
        if name in lower and lower[name] not in (None, ""):
            return lower[name]
    return None


def _minutes(v: Any) -> int | None:
    if v in (None, ""):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hour)", str(v), re.IGNORECASE)
    if m:
        return int(float(m.group(1)) * 60)
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    return int(float(m.group(0))) if m else None


def _date(v: Any) -> str | None:
    if v in (None, ""):
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{9,13}", s):                       # unix seconds or millis
        secs = int(s) / (1000 if len(s) > 10 else 1)
        return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc).date().isoformat()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def derive_outcome(raw: Any, minutes: int | None, stuck: bool, difficulty: str, clean_bar: dict[str, int]) -> str:
    s = str(raw or "").strip().lower()
    if s in OUTCOMES:
        return s
    if any(w in s for w in ("gave", "fail", "unsolved", "wrong", "tle", "timeout", "not solved", "no")) and "solution" not in s:
        return "gave_up"
    if any(w in s for w in ("solution", "editorial", "answer")):
        return "solution"
    if "hint" in s:
        return "hint"
    bar = clean_bar.get(difficulty, 30)
    if stuck or (minutes is not None and minutes > bar):
        return "slow"
    return "clean"


_HISTORY_RE = re.compile(r"(\d{4}\.\d{2}\.\d{2}|[A-Z][a-z]{2}\.? \d{1,2})\s*[｜|]\s*(\d+)\.\s*(.+?)(?=\s+(?:\d{4}\.\d{2}\.\d{2}|[A-Z][a-z]{2}\.? \d{1,2})\s*[｜|]|\s*$)",
                         re.MULTILINE | re.DOTALL)


def parse_history_text(text: str, today: str) -> list[dict[str, Any]]:
    """Rows from a bare history list: `2026.02.01｜2. Add Two Numbers`, one per line or run together on one line.
    A month-day without a year (`Sep 3｜69. Sqrt(x)`) is this year, or last year if that would be in the future."""
    t = dt.date.fromisoformat(today)
    rows = []
    for m in _HISTORY_RE.finditer(text):
        when, number, title = m.group(1), int(m.group(2)), m.group(3).strip()
        if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", when):
            day = when.replace(".", "-")
        else:
            parsed = dt.datetime.strptime(f"{when.replace('.', '')} {t.year}", "%b %d %Y").date()
            if parsed > t:
                parsed = parsed.replace(year=t.year - 1)
            day = parsed.isoformat()
        rows.append({"date": day, "number": number, "problem": title, "notes": "history import; outcome not recorded"})
    return rows


def parse_rows(rows: list[dict[str, Any]], clean_bar: dict[str, int], today: str,
               catalog: dict[int, tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """Rows -> attempts. `catalog` (number -> (difficulty, tags)) fills difficulty and topics a source leaves blank."""
    out = []
    for row in rows:
        title = _pick(row, "problem")
        url = _pick(row, "url")
        slug = _pick(row, "slug")
        if not slug and url:
            m = _SLUG_RE.search(str(url))
            slug = m.group(1) if m else None
        if not slug and title:
            slug = slugify(str(title))
        if not slug:
            continue
        number = _minutes(_pick(row, "number"))
        cat = (catalog or {}).get(number) if number else None
        difficulty = str(_pick(row, "difficulty") or (cat[0] if cat else "medium")).strip().lower()
        if difficulty not in DIFFICULTIES:
            difficulty = {"e": "easy", "m": "medium", "h": "hard"}.get(difficulty[:1], "medium")
        raw_topics = _pick(row, "topics") or (cat[1] if cat else None)
        if isinstance(raw_topics, list):
            topics = topic_keys(raw_topics)
        else:
            topics = topic_keys(t for t in re.split(r"[;,|/]", str(raw_topics or "")) if t.strip())
        minutes = _minutes(_pick(row, "minutes"))
        stuck_raw = _pick(row, "stuck")
        stuck = str(stuck_raw).strip().lower() in _TRUE if stuck_raw is not None else False
        attempt = {
            "date": _date(_pick(row, "date")) or today,
            "slug": str(slug), "title": str(title or slug), "url": str(url or f"https://leetcode.com/problems/{slug}/"),
            "difficulty": difficulty, "topics": topics, "minutes": minutes, "stuck": stuck,
            "outcome": derive_outcome(_pick(row, "outcome"), minutes, stuck, difficulty, clean_bar),
            "notes": str(_pick(row, "notes") or ""),
        }
        if number:
            attempt["number"] = number
        out.append(attempt)
    out.sort(key=lambda a: a["date"])
    return out


def parse_file(text: str, clean_bar: dict[str, int], today: str,
               catalog: dict[int, tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        doc = json.loads(stripped)
        rows = doc if isinstance(doc, list) else doc.get("attempts") or doc.get("submissions") or doc.get("rows") or []
    elif _HISTORY_RE.search(stripped) and "," not in stripped.splitlines()[0]:
        rows = parse_history_text(stripped, today)
    else:
        rows = list(csv.DictReader(io.StringIO(text)))
    return parse_rows(rows, clean_bar, today, catalog)


def append_attempts(path: Path, attempts: list[dict[str, Any]]) -> int:
    """Append attempts not already on file (same slug, date, outcome, minutes)."""
    existing = {(a["slug"], a["date"], a["outcome"], a.get("minutes")) for a in load_attempts(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with Path(path).open("a") as f:
        for a in attempts:
            key = (a["slug"], a["date"], a["outcome"], a.get("minutes"))
            if key in existing:
                continue
            existing.add(key)
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
            n += 1
    return n


def load_attempts(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    out = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    out.sort(key=lambda a: a["date"])
    return out


def _add(day: str, days: int) -> str:
    return (dt.date.fromisoformat(day) + dt.timedelta(days=days)).isoformat()


def schedule(attempts: list[dict[str, Any]], intervals: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per problem: last attempt, consecutive clean count, next due date."""
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for a in attempts:
        by_slug.setdefault(a["slug"], []).append(a)
    clean_ladder = list(intervals.get("clean", [7, 21, 60]))
    rows = []
    for slug, hist in by_slug.items():
        hist.sort(key=lambda a: a["date"])
        streak = 0
        for a in hist:
            streak = streak + 1 if a["outcome"] == "clean" else 0
        last = hist[-1]
        if last["outcome"] == "clean":
            days = clean_ladder[min(streak, len(clean_ladder)) - 1]
        else:
            days = int(intervals.get(last["outcome"], 3))
        rows.append({"slug": slug, "title": last["title"], "url": last["url"], "difficulty": last["difficulty"],
                     "topics": last["topics"], "attempts": len(hist), "clean_streak": streak,
                     "last_date": last["date"], "last_outcome": last["outcome"], "last_minutes": last.get("minutes"),
                     "due": _add(last["date"], days)})
    return rows


def due_reviews(attempts: list[dict[str, Any]], intervals: dict[str, Any], today: str, limit: int | None = None,
                weakness: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Problems whose next review date has passed. Failed ones first; then, when `weakness` (topic -> score) is given,
    problems in the weakest topics, so a 300-problem backlog surfaces the useful reviews rather than the oldest ones."""
    rows = [r for r in schedule(attempts, intervals) if r["due"] <= today]
    for r in rows:
        r["overdue_days"] = (dt.date.fromisoformat(today) - dt.date.fromisoformat(r["due"])).days
        r["weakness"] = max((weakness or {}).get(t, 0.0) for t in r["topics"]) if r["topics"] and weakness else 0.0
    order = {"gave_up": 0, "solution": 0, "hint": 1, "slow": 1, "clean": 2}
    rows.sort(key=lambda r: (order.get(r["last_outcome"], 1), -r["weakness"], -r["overdue_days"], r["slug"]))
    return rows[:limit] if limit else rows


def topic_stats(attempts: list[dict[str, Any]], today: str, window_days: int, coverage_min: int,
                ratings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Per topic: attempts, clean share in the window, weakness 0–1 (higher = practice first)."""
    cutoff = _add(today, -window_days)
    per: dict[str, dict[str, Any]] = {}
    for a in attempts:
        for t in a["topics"] or ["untagged"]:
            s = per.setdefault(t, {"topic": t, "attempts": 0, "recent": 0, "recent_clean": 0, "last_date": ""})
            s["attempts"] += 1
            s["last_date"] = max(s["last_date"], a["date"])
            if a["date"] >= cutoff:
                s["recent"] += 1
                s["recent_clean"] += a["outcome"] == "clean"
    for t, rating in (ratings or {}).items():
        per.setdefault(t, {"topic": t, "attempts": 0, "recent": 0, "recent_clean": 0, "last_date": ""})
    out = []
    for s in per.values():
        rating = (ratings or {}).get(s["topic"])
        if s["recent"]:
            weakness = 1 - s["recent_clean"] / s["recent"]
        elif isinstance(rating, (int, float)):
            weakness = (5 - float(rating)) / 4
        else:
            weakness = 0.6
        if s["attempts"] < coverage_min:
            weakness += 0.5 * (coverage_min - s["attempts"]) / coverage_min
        s["rating"] = rating
        s["weakness"] = round(min(weakness, 1.5), 2)
        out.append(s)
    out.sort(key=lambda s: (-s["weakness"], s["topic"]))
    return out


def format_stats(stats: list[dict[str, Any]]) -> str:
    head = f"{'topic':<30} {'rating':>6} {'attempts':>8} {'recent':>7} {'clean%':>7} {'last':>10}  weakness"
    lines = [head, "-" * len(head)]
    for s in stats:
        pct = f"{100 * s['recent_clean'] / s['recent']:.0f}%" if s["recent"] else "-"
        lines.append(f"{s['topic']:<30} {str(s['rating'] or '-'):>6} {s['attempts']:>8} {s['recent']:>7} {pct:>7} {s['last_date'] or '-':>10}  {s['weakness']:.2f}")
    return "\n".join(lines)
