"""Assemble today's plan skeleton: budget, weights with modifiers, one block per study lane.

The structured plan (`plans/<date>.json`) is the source of truth; the markdown (`plans/<date>.md`) is rendered
from it by `render_md` and parsed back by `parse_md`, so an agent, the CLI helpers and the board all edit the
same object.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from . import log as logmod
from .coaching import apply_coaching, load_coaching
from .config import load_curriculum, load_level, load_preferences, load_resources, load_terms
from .curriculum import load_progress, next_lessons
from .jobs import applications, latest_snapshot, upcoming_interviews
from .leetcode import due_reviews, load_attempts, topic_stats
from .news import format_items
from .sfdata import went_yesterday
from .stack import count_terms, job_terms, scopes_for, top_gaps

DEBRIEF_MINUTES = 15
DEBRIEF_QUESTIONS = ("What did you do?", "What did you learn?", "What belongs in a story?")
STACK_TASK = ("Read the official 'getting started' page (20 min); build a 30-minute toy or read one real config; write one "
              "paragraph on where it would sit in the Example Studio / BGM pipeline and what it would replace.")
CALL_PLACEHOLDER = "_(agent: one sentence on the single highest-utility thing today and why)_"
SNAP = 5                       # every start and duration on the board snaps to five minutes
DEFAULT_SLOT_STARTS = {"morning": "09:00", "afternoon": "13:00", "evening": "19:00", "night": "21:00"}
_TIMEBOX = re.compile(r"\b(\d+)\s*min(?:ute)?s?\b", re.IGNORECASE)
_ACTION = re.compile(r"^(?:open|read|watch|write|solve|review|draw|complete|listen)\b", re.IGNORECASE)
_DIRECT_RESOURCE = re.compile(r"\[[^\]]+\]\(https?://[^)\s]+\)|https?://[^\s<)`]+|(?:\.\.?/)[^\s`]+")
_VAGUE_RESOURCE_COUNT = re.compile(r"\b(?:two|several|multiple)\b[^.]*\b(?:resources|frameworks)\b", re.IGNORECASE)


@dataclass
class Item:
    id: str
    text: str
    done: bool = False
    refs: list[str] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    first_action: str = ""
    action_url: str = ""

    @property
    def not_applicable(self) -> bool:
        return bool(self.decisions and self.decisions[-1]["state"] == "not_applicable")



@dataclass
class Block:
    id: str
    title: str
    minutes: int
    start: str = ""                                          # "HH:MM"; filled by `place_blocks`
    first_action: str = ""                                   # the two-minute, no-decision opener
    description: list[str] = field(default_factory=list)     # prose and fenced snippets, rendered above the items
    items: list[Item] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    log_hint: str = ""
    done: bool = False

    @property
    def lines(self) -> list[str]:
        """The markdown body of the block (kept as a property so older callers and tests still read `.lines`)."""
        out = list(self.description)
        if self.first_action:
            out.append(f"**First action (2 min):** {self.first_action}")
        for i in self.items:
            out.append(f"- [{'x' if i.done else ' '}] {i.text}" + (f" <!--refs:{'|'.join(i.refs)}-->" if i.refs else ""))
            if i.first_action or i.action_url:
                out.append("<!--action:" + json.dumps({"first_action": i.first_action, "action_url": i.action_url}, ensure_ascii=True) + "-->")
            if i.decisions:
                out.append("<!--decisions:" + json.dumps(i.decisions, ensure_ascii=True) + "-->")
                if i.not_applicable:
                    out.append("<!--decision-label--> **暂不做 / N/A:** " + i.decisions[-1]["reason"].replace("\n", " "))
        if self.links:
            out.append("Open: " + " · ".join(f"[{l['label']}]({l['url']})" for l in self.links))
        if self.log_hint:
            out.append(f"Log: `{self.log_hint}`")
        return out

    def add_item(self, text: str, done: bool = False, refs: list[str] | None = None) -> Item:
        item = Item(f"{self.id}-{len(self.items) + 1}", text, done, list(refs or []))
        self.items.append(item)
        return item

    @property
    def end(self) -> str:
        return to_hhmm(to_minutes(self.start) + self.minutes) if self.start else ""


@dataclass
class Event:
    """A commitment already on the user's calendar. Not a study block: nothing to tick, nothing logged.

    It is on the board so the day is the real day, and `place_blocks` lays study around it instead of on top.
    """
    id: str
    title: str
    start: str
    minutes: int
    calendar: str = ""
    url: str = ""
    location: str = ""
    all_day: bool = False
    busy: bool = True                    # False: shown for context, blocks may still be placed over it
    gcal_id: str = ""                    # Google's own id, so a re-import updates in place
    source: str = "manual"               # "gcal" once imported from the calendar

    def __post_init__(self) -> None:
        if self.all_day:
            self.busy = False      # an all-day event ("Labor Day") never reserves the clock; it is context

    @property
    def end(self) -> str:
        return to_hhmm(to_minutes(self.start) + self.minutes)


@dataclass
class Plan:
    date: str
    minutes: int
    radar: list[str]
    blocks: list[Block]
    warnings: list[str] = field(default_factory=list)
    call: str = CALL_PLACEHOLDER
    events: list[Event] = field(default_factory=list)
    arranged: bool = False               # the user moved blocks by hand: a calendar refresh must not re-lay them

    def block(self, block_id: str) -> Block | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    def event(self, event_id: str) -> Event | None:
        return next((e for e in self.events if e.id == event_id or e.gcal_id == event_id), None)

    @property
    def busy(self) -> list[tuple[int, int]]:
        """Minute ranges the calendar has already spoken for, merged. All-day events never block the day."""
        spans = sorted((to_minutes(e.start), to_minutes(e.start) + e.minutes) for e in self.events if e.busy)
        merged: list[tuple[int, int]] = []
        for lo, hi in spans:
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        return merged


def quality_issues(plan: Plan) -> list[str]:
    """Return actionable clarity failures for a finished daily board.

    This is deliberately a content lint rather than a subjective score. It checks
    the promises the board makes to the user: every block says what to open first,
    where it is, how long the block and each task take, and what to do next.
    """
    issues: list[str] = []
    for block in plan.blocks:
        label = block.id or block.title or "unnamed block"
        if not block.title.strip():
            issues.append(f"{label}: add a specific block title")
        if block.minutes < 5 or block.minutes % SNAP:
            issues.append(f"{label}: give the block a positive five-minute time box")
        if not block.description:
            issues.append(f"{label}: explain the purpose of this block in one sentence")
        action = block.first_action.strip()
        if not action:
            issues.append(f"{label}: add a two-minute first action")
        else:
            if not _ACTION.match(action):
                issues.append(f"{label}: first action must start with a concrete verb such as 'open' or 'solve'")
            if not _DIRECT_RESOURCE.search(action):
                issues.append(f"{label}: first action must name and link the exact page or file to open")
            if re.search(r"\bsearch\s+(?:for|in|the|online|google)\b", action, re.IGNORECASE) or action.lower().startswith("search "):
                issues.append(f"{label}: replace search instructions with the exact page or file")
        if not block.items:
            issues.append(f"{label}: add at least one concrete task")
            continue
        item_minutes = 0
        explicit_actions = any(i.first_action or i.action_url for i in block.items)
        for item in block.items:
            if explicit_actions and (not item.first_action or not item.action_url):
                issues.append(f"{item.id}: give every subtask an action label and action_url")
            matches = _TIMEBOX.findall(item.text)
            if not matches:
                issues.append(f"{label}: time-box '{item.text}'")
            item_minutes += sum(int(value) for value in matches)
            if _VAGUE_RESOURCE_COUNT.search(item.text):
                issues.append(f"{label}: replace vague resource counts with named, linked resources")
        if item_minutes and item_minutes != block.minutes:
            issues.append(f"{label}: task time boxes total {item_minutes} min, not the block's {block.minutes} min")
    return issues


# ---------------------------------------------------------------- times and slots

def to_minutes(hhmm: str) -> int:
    h, _, m = str(hhmm).partition(":")
    return int(h) * 60 + int(m or 0)


def to_hhmm(minutes: int) -> str:
    minutes = max(0, min(24 * 60 - 1, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def snap(minutes: int, step: int = SNAP) -> int:
    return int(round(float(minutes) / step)) * step


def day_slots(level: dict[str, Any]) -> list[dict[str, Any]]:
    """When the user studies. `constraints.slots` wins; otherwise parse `constraints.best_time`."""
    cons = level.get("constraints") or {}
    slots = cons.get("slots")
    if slots:
        return [{"start": str(s["start"]), "minutes": int(s["minutes"])} for s in slots]
    out: list[dict[str, Any]] = []
    text = str(cons.get("best_time") or "")
    for period, hours in re.findall(r"(morning|afternoon|evening|night)\s+(\d+(?:\.\d+)?)\s*h", text, re.IGNORECASE):
        out.append({"start": DEFAULT_SLOT_STARTS[period.lower()], "minutes": int(float(hours) * 60)})
    if not out:
        out = [{"start": str(cons.get("day_start") or "09:00"), "minutes": 24 * 60}]
    elif cons.get("day_start"):
        out[0]["start"] = str(cons["day_start"])
    return sorted(out, key=lambda s: to_minutes(s["start"]))


def place_blocks(blocks: list[Block], slots: list[dict[str, Any]], busy: list[tuple[int, int]] | None = None) -> None:
    """Lay the blocks into the slots in plan order, back to back, never overlapping each other or a meeting.

    `busy` is a merged list of (start, end) minute ranges from the day's calendar events: a block that would
    land on one is pushed past it, so the plan never schedules study on top of a call.
    """
    if not slots:
        slots = [{"start": "09:00", "minutes": 24 * 60}]
    busy = busy or []

    def clear(at: int, length: int) -> int:
        """The first minute at or after `at` where a block of `length` fits between the meetings."""
        moved = True
        while moved:
            moved = False
            for lo, hi in busy:
                if at < hi and lo < at + length:
                    at, moved = hi, True
        return at

    index, cursor, left = 0, to_minutes(slots[0]["start"]), int(slots[0]["minutes"])
    for b in blocks:
        placed = clear(cursor, b.minutes)
        spent = placed - cursor + b.minutes                  # the meeting we skipped still eats the slot
        if spent > left and index + 1 < len(slots):
            index += 1
            cursor, left = max(cursor, to_minutes(slots[index]["start"])), int(slots[index]["minutes"])
            placed = clear(cursor, b.minutes)
            spent = placed - cursor + b.minutes
        b.start = to_hhmm(placed)
        cursor, left = placed + b.minutes, left - spent


def gcal_url(date: str, block: Block) -> str:
    """An 'add to Google Calendar' template link, floating local time (same shape as sf-events' gcal_url)."""
    if not block.start:
        return ""
    day = date.replace("-", "")
    fmt = lambda hhmm: f"{day}T{hhmm.replace(':', '')}00"
    details = "\n".join(x for x in ([block.first_action] if block.first_action else []) + [i.text for i in block.items])
    q = {"action": "TEMPLATE", "text": block.title, "dates": f"{fmt(block.start)}/{fmt(block.end)}", "details": details}
    return "https://calendar.google.com/calendar/render?" + urlencode(q, quote_via=quote)


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def allocate(budget: int, weights: dict[str, float]) -> dict[str, int]:
    total = sum(weights.values()) or 1.0
    mins = {k: int(round(budget * w / total / 5.0)) * 5 for k, w in weights.items()}
    diff = budget - sum(mins.values())
    if diff:
        biggest = max(mins, key=mins.get)
        mins[biggest] += diff
    return mins


def _interview_map_questions(parent: Path) -> list[str]:
    path = parent / "profile" / "context" / "98-interview-map.md"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.startswith("| Design") or line.startswith("| Add "):
            out.append(line.split("|")[1].strip())
    return out


def _stories(parent: Path) -> list[str]:
    d = parent / "profile" / "stories"
    return sorted(p.stem for p in d.glob("*.md") if not p.stem.startswith("_")) if d.exists() else []


def _recent_stack_terms(entries: list[dict[str, Any]], today: str, days: int = 14) -> set[str]:
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    return {e["term"] for e in entries if e.get("block") == "stack" and e.get("term") and e.get("date", "") >= start}


def build_plan(root: Path, parent: Path, today: str, *, minutes: int | None = None,
               news_items: list[dict[str, Any]] | None = None, news_warnings: list[str] | None = None,
               events: list[Event] | None = None) -> Plan:
    prefs = load_preferences(root)
    coaching = load_coaching(root)
    level = load_level(root)
    mods = prefs.get("modifiers", {})
    entries = logmod.load(root / "data" / "log.jsonl")
    day = dt.date.fromisoformat(today)
    weekend = day.weekday() >= 5
    cons = level.get("constraints") or {}
    budget = minutes or _num(cons.get("weekend_minutes" if weekend else "weekday_minutes")) \
        or prefs.get("weekend_minutes" if weekend else "daily_minutes", 180)
    budget = int(budget)
    if events and coaching:
        busy_plan = Plan(today, budget, [], [], events=events)
        budget = min(budget, free_minutes(day_slots(level), busy_plan.busy))

    snap_date, jobs = latest_snapshot(parent)
    apps = applications(parent / "data" / "feedback.jsonl")
    interviews = upcoming_interviews(apps, jobs, today, int(mods.get("interview_within_days", 7)),
                                     (level.get("goals") or {}).get("interview_dates"))
    weights = {k: float(v) for k, v in (prefs.get("weights") or {}).items()}
    if coaching:
        # Focus on the current interview gaps instead of filling every lane by quota.
        policy = coaching.get("planning", {})
        weights.update({k: float(v) for k, v in policy.get("focus_weights", {}).items()})
        if policy.get("suppress_stories"):
            weights["stories"] = 0
        if budget <= int(policy.get("short_day_minutes", 90)):
            weights["stories"] = 0
    radar: list[str] = []
    warnings: list[str] = []
    pinned_job = None
    if interviews:
        weights["stories"] = weights.get("stories", 0.05) * float(mods.get("interview_stories_factor", 3))
        weights["system_design"] = weights.get("system_design", 0.3) * float(mods.get("interview_system_design_factor", 1.5))
        by_id = {j.get("id"): j for j in jobs}
        for iv in interviews:
            when = f"on {iv['when']}" if iv["dated"] else f"stage set {iv['when']}"
            radar.append(f"{iv['company']} {iv['stage']} {when}" + (f" · {iv['title']}" if iv["title"] else ""))
            if pinned_job is None and iv["job_id"] in by_id:
                pinned_job = by_id[iv["job_id"]]
    coding = _num((level.get("coding") or {}).get("overall"))
    design = _num((level.get("system_design") or {}).get("overall"))
    if coding is not None and coding < float(mods.get("weak_coding_below", 3)):
        weights["leetcode"] = weights.get("leetcode", 0.4) * float(mods.get("weak_coding_factor", 1.5))
    if design is not None and design < float(mods.get("weak_design_below", 2)):
        weights["system_design"] = weights.get("system_design", 0.3) * float(mods.get("weak_design_factor", 1.5))
    if coaching.get("planning", {}).get("focus_weights_final"):
        # A current coach decision already accounts for readiness and round uncertainty.
        # Do not silently amplify it again from an old stage marker or broad self-rating.
        weights.update({k: float(v) for k, v in coaching["planning"]["focus_weights"].items()})
    if not level.get("assessed_on"):
        radar.append("No assessment on file: run the assessment interview (profile/assessment.md) before trusting the split.")
    week = logmod.week_summary(entries, today)
    if week["total"]:
        radar.append(f"Last 7 days: {week['total']} min over {week['active_days']} days; streak {week['streak']}.")
    debriefs = went_yesterday(parent, today)
    mins = allocate(max(budget - DEBRIEF_MINUTES * len(debriefs), 0), weights)
    blocks: list[Block] = []

    # 0. debrief: the day after an event the user went to, before anything else, so the coach learns from it
    for ev in debriefs:
        title = str(ev.get("title") or ev.get("id"))
        db = Block("debrief" if len(debriefs) == 1 else f"debrief-{len(blocks) + 1}", f"Debrief · {title}", DEBRIEF_MINUTES)
        db.description.append(f"You went to {title} yesterday. Three answers, one or two sentences each; the coach turns them "
                              "into a notes entry and, when there is interview value, a story draft for your approval.")
        db.first_action = "open the Debrief panel on the hub page for today (or answer the three questions in chat)."
        for q in DEBRIEF_QUESTIONS:
            db.add_item(q, refs=[str(ev["url"])] if ev.get("url") else [])
        if ev.get("url"):
            db.links.append({"label": "the event", "url": str(ev["url"])})
        db.log_hint = f"uv run studyplan done debrief --minutes {DEBRIEF_MINUTES}"
        blocks.append(db)
        radar.append(f"Debrief due: {title} (went yesterday); it is the first block.")

    # 1. news
    news = Block("news", "News", mins.get("news", 0))
    if news_items:
        news.description.append("Pick three; one line each on why it matters for your interviews (hot area, a target company's tool, a change to your stories).")
        news.description.append("```")
        news.description.extend(format_items(news_items).splitlines())
        news.description.append("```")
        news.first_action = f"open [{news_items[0]['title']}]({news_items[0]['url']}) and read the first screen."
        news.links = [{"label": i.get("source") or i["title"][:30], "url": i["url"]} for i in news_items[:3] if i.get("url")]
    else:
        news.add_item("`uv run studyplan news` (or the agent's web search); pick three items and say why each matters for your interviews.")
    news.log_hint = f"uv run studyplan done news --minutes {news.minutes}"
    for w in news_warnings or []:
        warnings.append(f"news: {w}")
    blocks.append(news)

    # 2. stack gap
    terms = load_terms(root)
    overrides = {k: str(v) for k, v in (level.get("stack") or {}).items()}
    scopes = scopes_for(jobs, set(apps), int((prefs.get("stack") or {}).get("top_min_score", 75)))
    rows = count_terms(terms, scopes, overrides)
    recent = _recent_stack_terms(entries, today)
    chosen, why = None, ""
    if pinned_job is not None:
        for name, depth in job_terms(terms, pinned_job, overrides):
            if name not in recent and depth in ("none", "limited", "familiar"):
                chosen, why = name, f"named by the {pinned_job.get('company')} posting you are interviewing for (depth {depth})"
                break
    if chosen is None:
        gaps = top_gaps(rows, limit=10, exclude=recent)
        if gaps:
            g = gaps[0]
            chosen = g.name
            why = (f"applied {g.counts.get('applied', 0)}/{g.totals.get('applied', 0)}, top {g.counts.get('top', 0)}/{g.totals.get('top', 0)}, "
                   f"all {g.counts.get('all', 0)}/{g.totals.get('all', 0)}; depth {g.depth}")
    stack = Block("stack", f"Stack gap · {chosen}" if chosen else "Stack gap", mins.get("stack", 0))
    if chosen:
        stack.description.append(f"Why: {why}.")
        stack.first_action = f"search for \"{chosen} getting started\" and open the official docs page."
        stack.add_item(STACK_TASK)
        stack.log_hint = f"uv run studyplan done stack --minutes {stack.minutes} --term \"{chosen}\""
    else:
        stack.add_item("No gap term found (no snapshot, or every named term is already at working depth). Run `uv run studyplan stack`.")
        stack.log_hint = f"uv run studyplan done stack --minutes {stack.minutes}"
    if not snap_date:
        warnings.append("stack: no job snapshot under ../data/jobs; run the job-radar scan first")
    blocks.append(stack)

    # 3. system design
    modules = load_curriculum(root)
    progress = load_progress(root / "data" / "progress.json")
    nxt = next_lessons(modules, progress, 1)
    if nxt:
        lesson = nxt[0]
        sd = Block("design", f"System design · {lesson['id']} {lesson['title']}", mins.get("system_design", 0))
        sd.description.append(f"Module: {lesson['module']} · planned {lesson.get('minutes', 45)} min")
        resources = load_resources(root)
        for key, resource in resources.items():
            if re.search(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", lesson.get("read", ""), re.IGNORECASE):
                sd.links.append(dict(resource))
        start_resource = resources.get(str(lesson.get("start_resource") or ""))
        if start_resource:
            sd.first_action = f"open [{start_resource['label']}]({start_resource['url']}) and read the first screen."
        elif sd.links:
            sd.first_action = f"open [{sd.links[0]['label']}]({sd.links[0]['url']}) and read the first screen."
        entry = f"study-plan/config/curriculum.yaml#{lesson['id']}"
        sd.add_item(f"Read: {lesson.get('read', '')}", refs=[entry, *[l["url"] for l in sd.links]])
        sd.add_item(f"Do: {lesson.get('do', '')}", refs=[entry])
        if lesson.get("own_system"):
            sd.add_item(f"Own system: `../profile/context/{lesson['own_system']}.md` — end by telling it as yours, with numbers.",
                        refs=[f"profile/context/{lesson['own_system']}.md"])
        sd.log_hint = f"uv run studyplan lesson done {lesson['id']} --minutes {sd.minutes}"
    else:
        sd = Block("design", "System design · syllabus complete", mins.get("system_design", 0))
        sd.add_item("Mock round from the interview map (see Stories block).")
        sd.log_hint = f"uv run studyplan done design --minutes {sd.minutes}"
    blocks.append(sd)

    # 4. leetcode
    lc_prefs = prefs.get("leetcode") or {}
    attempts = load_attempts(root / "data" / "leetcode.jsonl")
    ratings = ((level.get("coding") or {}).get("topics") or {})
    stats = topic_stats(attempts, today, int(lc_prefs.get("weakness_window_days", 90)), int(lc_prefs.get("coverage_min_attempts", 3)), ratings)
    weakness = {s["topic"]: s["weakness"] for s in stats}
    due = due_reviews(attempts, lc_prefs.get("review_intervals_days") or {}, today, int(lc_prefs.get("max_reviews_per_day", 4)), weakness)
    lc = Block("leetcode", "LeetCode", mins.get("leetcode", 0))
    per_review = int(lc_prefs.get("minutes_per_review", 12))
    left = lc.minutes
    for r in due:
        if left < per_review:
            break
        lc.add_item(f"review: [{r['title']}]({r['url']}) ({r['difficulty']}, {', '.join(r['topics']) or 'untagged'}) — "
                    f"last {r['last_outcome']} on {r['last_date']}" + (f", {r['overdue_days']} days overdue" if r["overdue_days"] else ""),
                    refs=[r["url"], f"study-plan/data/leetcode.jsonl#{r['slug']}"])
        lc.links.append({"label": r["title"], "url": r["url"]})
        left -= per_review
    per_new = lc_prefs.get("minutes_per_new") or {"easy": 15, "medium": 30, "hard": 45}
    difficulty = "easy" if (coding is not None and coding < 2) else "medium"
    n_new = max(0, min(int(lc_prefs.get("max_new_per_day", 3)), left // int(per_new.get(difficulty, 30))))
    weak = [s for s in stats if s["topic"] != "untagged"][:max(n_new, 1)]
    for i in range(n_new):
        s = weak[i % len(weak)] if weak else None
        topic = s["topic"] if s else "any"
        reason = (f"clean {s['recent_clean']}/{s['recent']} in 90 days" if s and s["recent"] else
                  f"{s['attempts']} attempts, rating {s['rating'] or '-'}" if s else "no data")
        lc.add_item(f"new {difficulty}: topic **{topic}** ({reason}); pick from the NeetCode 150 list for that topic; time it.")
    if due:
        lc.first_action = f"open {due[0]['url']} and write the pattern from memory before reading the problem."
    if not attempts:
        lc.description.append("No history yet: `uv run studyplan leetcode import <export.csv>` once you have the export; until then log each problem with `leetcode add`.")
    remnote_catalog = root / "data" / "remnote-problem-catalog.jsonl"
    if remnote_catalog.exists():
        count = sum(1 for line in remnote_catalog.read_text().splitlines() if line.strip())
        lc.description.append(
            f"RemNote candidate pool: {count} note-only company problems in `data/remnote-problem-catalog.jsonl`; "
            "solve blind, then use the linked note for comparison. The imported history remains the coverage record."
        )
    lc.log_hint = "uv run studyplan leetcode add --problem \"<title>\" --difficulty medium --topics \"<tags>\" --minutes N --outcome clean|slow|hint|solution|gave_up"
    blocks.append(lc)

    # 5. stories / mock
    stories = _stories(parent)
    questions = _interview_map_questions(parent)
    st = Block("stories", "Stories and mock", mins.get("stories", 0))
    idx = day.toordinal()
    if stories:
        story = stories[idx % len(stories)]
        st.first_action = f"open `../profile/stories/{story}.md` and read the headline and the Result section."
        st.add_item(f"Tell `../profile/stories/{story}.md` aloud, end to end, with numbers; then answer two of its 'If asked about a bullet' follow-ups.",
                    refs=[f"profile/stories/{story}.md"])
    if questions:
        q = questions[idx % len(questions)]
        st.add_item(f"Mock (35 min on the clock when an interview is near): \"{q}\" — see `../profile/context/98-interview-map.md`.",
                    refs=["profile/context/98-interview-map.md"])
    if interviews:
        st.add_item(f"Interview prep: re-read the {interviews[0]['company']} posting and its fit review; list the three things they will probe.")
    st.log_hint = f"uv run studyplan done stories --minutes {st.minutes}"
    blocks.append(st)
    plan = Plan(today, budget, radar, blocks, warnings, events=list(events or []))
    if coaching:
        apply_coaching(plan, root, prefs, level, coaching)
    place_blocks(plan.blocks, day_slots(level), plan.busy)
    if plan.events:
        booked = sum(hi - lo for lo, hi in plan.busy)
        radar.append(f"Calendar: {len(plan.events)} event(s) today"
                     + (f", {booked} min booked; study is laid around them." if booked else "."))
    return plan


def shrink_to_free(plan: Plan, slots: list[dict[str, Any]]) -> int:
    """The budget cannot exceed the study slots' free minutes once the busy ranges are subtracted.

    Returns the minutes taken off. The blocks keep their proportions (`allocate` over the block minutes), so a
    hackathon that eats the afternoon leaves a short evening plan instead of five blocks that cannot fit.
    """
    free = free_minutes(slots, plan.busy)
    if free >= plan.minutes or not plan.blocks:
        return 0
    cut = plan.minutes - free
    weights = {b.id: float(b.minutes) for b in plan.blocks}
    mins = allocate(free, weights) if free > 0 else {b.id: 0 for b in plan.blocks}
    floor = 5 if free >= 5 * len(plan.blocks) else 0            # every block keeps at least five minutes when it can
    for b in plan.blocks:
        b.minutes = max(mins.get(b.id, 0), floor)
    over = sum(b.minutes for b in plan.blocks) - free
    while over > 0:                                              # the floor was paid for by the biggest blocks
        biggest = max(plan.blocks, key=lambda b: b.minutes)
        if biggest.minutes <= floor:
            break
        biggest.minutes -= 5
        over -= 5
    plan.minutes = free
    plan.radar.append(f"Calendar leaves {free} free min in your study slots; budget cut by {cut} min to fit.")
    place_blocks(plan.blocks, slots, plan.busy)
    return cut


def free_minutes(slots: list[dict[str, Any]], busy: list[tuple[int, int]]) -> int:
    """Minutes of the study slots that no busy range covers: what the budget can really be."""
    total = 0
    for s in slots:
        lo = to_minutes(s["start"])
        hi = min(lo + int(s["minutes"]), 24 * 60)
        covered = sum(max(0, min(hi, b) - max(lo, a)) for a, b in busy)
        total += max(0, hi - lo - covered)
    return total


# ---------------------------------------------------------------- JSON <-> markdown

EVENING = ("**Evening:** `uv run studyplan done <block> --minutes N --note \"...\"` per block, or paste "
           "`done leetcode 70 two DP problems, stuck on LIS` lines to the agent.")
_HEAD_RE = re.compile(r"^## \d+\. (?P<title>.*?)(?: · (?P<start>\d{2}:\d{2})–\d{2}:\d{2})? · (?P<minutes>\d+) min"
                      r"(?P<doneflag> · done)? <!--id:(?P<id>[^>]+)-->$")
_ITEM_RE = re.compile(r"^- \[(?P<done>[ x])\] (?P<text>.*?)(?: <!--refs:(?P<refs>[^>]*)-->)?$")
_EVENT_RE = re.compile(r"^- (?P<when>all day|\d{2}:\d{2}–\d{2}:\d{2}) · (?P<rest>.*?)"
                       r" <!--ev:(?P<id>[^|]*)\|(?P<gcal>[^>]*)-->$")


def render_event(e: Event) -> str:
    when = "all day" if e.all_day else f"{e.start}–{e.end}"
    parts = [f"[{e.title}]({e.url})" if e.url else e.title]
    parts += [p for p in (e.location, e.calendar) if p]
    if not e.busy and not e.all_day:
        parts.append("free")
    return f"- {when} · {' · '.join(parts)} <!--ev:{e.id}|{e.gcal_id}-->"


def parse_event(line: str) -> Event | None:
    m = _EVENT_RE.match(line)
    if not m:
        return None
    when, rest = m.group("when"), m.group("rest")
    all_day = when == "all day"
    start, minutes = ("00:00", 24 * 60) if all_day else (when[:5], to_minutes(when[6:]) - to_minutes(when[:5]))
    busy = True
    if not all_day and rest.endswith(" · free"):
        rest, busy = rest[: -len(" · free")], False
    head, _, tail = rest.partition(" · ")
    link = re.match(r"^\[(?P<title>.*)\]\((?P<url>[^)]*)\)$", head)
    title, url = (link.group("title"), link.group("url")) if link else (head, "")
    extras = tail.split(" · ") if tail else []
    calendar = extras.pop() if extras else ""
    return Event(m.group("id"), title, start, minutes, calendar, url, " · ".join(extras), all_day, busy,
                 m.group("gcal"), "gcal" if m.group("gcal") else "manual")


def to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "date": plan.date, "minutes": plan.minutes, "call": plan.call, "radar": list(plan.radar),
        "blocks": [{"id": b.id, "title": b.title, "start": b.start, "minutes": b.minutes, "done": b.done,
                    "first_action": b.first_action, "description": list(b.description),
                    "links": [dict(l) for l in b.links],
                    "items": [{"id": i.id, "text": i.text, "done": i.done, "refs": list(i.refs), "decisions": list(i.decisions), "first_action": i.first_action, "action_url": i.action_url} for i in b.items],
                    "log_hint": b.log_hint} for b in plan.blocks],
        "events": [{"id": e.id, "title": e.title, "start": e.start, "minutes": e.minutes, "calendar": e.calendar,
                    "url": e.url, "location": e.location, "all_day": e.all_day, "busy": e.busy,
                    "gcal_id": e.gcal_id, "source": e.source} for e in plan.events],
        "warnings": list(plan.warnings),
        "arranged": bool(plan.arranged),
    }


def from_dict(data: dict[str, Any]) -> Plan:
    blocks = []
    for b in data.get("blocks") or []:
        block = Block(str(b["id"]), str(b.get("title", "")), int(b.get("minutes", 0)), str(b.get("start", "")),
                      str(b.get("first_action", "")), list(b.get("description") or []),
                      [Item(str(i["id"]), str(i["text"]), bool(i.get("done")), [str(r) for r in (i.get("refs") or [])], list(i.get("decisions") or []), str(i.get("first_action") or ""), str(i.get("action_url") or ""))
                       for i in (b.get("items") or [])],
                      [{"label": str(l["label"]), "url": str(l["url"])} for l in (b.get("links") or [])],
                      str(b.get("log_hint", "")), bool(b.get("done")))
        blocks.append(block)
    events = [Event(str(e["id"]), str(e.get("title", "")), str(e.get("start", "")), int(e.get("minutes", 0)),
                    str(e.get("calendar", "")), str(e.get("url", "")), str(e.get("location", "")),
                    bool(e.get("all_day")), bool(e.get("busy", True)), str(e.get("gcal_id", "")),
                    str(e.get("source") or "manual")) for e in (data.get("events") or [])]
    return Plan(str(data["date"]), int(data.get("minutes", 0)), list(data.get("radar") or []), blocks,
                list(data.get("warnings") or []), str(data.get("call") or CALL_PLACEHOLDER), events,
                bool(data.get("arranged")))


def render_md(plan: Plan | dict[str, Any]) -> str:
    plan = from_dict(plan) if isinstance(plan, dict) else plan
    day = dt.date.fromisoformat(plan.date)
    out = [f"# Study plan — {plan.date} ({day.strftime('%a')}) · {plan.minutes} min"
           + (" · arranged by hand" if plan.arranged else ""), ""]
    if plan.radar:
        out.append("**Radar**")
        out.extend(f"- {r}" for r in plan.radar)
        out.append("")
    out.append(f"**Today's call:** {plan.call}")
    out.append("")
    if plan.events:
        out.append("**Calendar** (from Google Calendar; the blocks below are laid around these)")
        out.extend(render_event(e) for e in plan.events)
        out.append("")
    for i, b in enumerate(plan.blocks, 1):
        when = f" · {b.start}–{b.end}" if b.start else ""
        out.append(f"## {i}. {b.title}{when} · {b.minutes} min{' · done' if b.done else ''} <!--id:{b.id}-->")
        out.append("")
        out.extend(b.lines)
        out.append("")
    if plan.warnings:
        out.append("**Warnings**")
        out.extend(f"- {w}" for w in plan.warnings)
        out.append("")
    out.append(EVENING)
    return "\n".join(out) + "\n"


render = render_md          # the old name: `render(plan)` still returns the markdown


def parse_md(text: str) -> Plan:
    """Read a plan back out of the markdown `render_md` produced (the round trip the board relies on)."""
    head = re.match(r"^# Study plan — (\d{4}-\d{2}-\d{2}) \(\w+\) · (\d+) min", text)
    if not head:
        raise ValueError("not a study plan: missing the '# Study plan — <date> (<day>) · <n> min' heading")
    plan = Plan(head.group(1), int(head.group(2)), [], [])
    plan.arranged = text.splitlines()[0].endswith(" · arranged by hand")
    section, in_fence = None, False
    for line in text.splitlines():
        m = _HEAD_RE.match(line)
        if m:
            plan.blocks.append(Block(m.group("id"), m.group("title"), int(m.group("minutes")),
                                     m.group("start") or "", done=bool(m.group("doneflag"))))
            section, in_fence = "block", False
            continue
        if line == "**Radar**":
            section = "radar"
            continue
        if line == "**Warnings**":
            section = "warnings"
            continue
        if line.startswith("**Calendar**"):
            section = "calendar"
            continue
        if section == "calendar":
            event = parse_event(line)
            if event is not None:
                plan.events.append(event)
            continue
        if line.startswith("**Today's call:**"):
            plan.call = line[len("**Today's call:**"):].strip()
            section = None
            continue
        if line.startswith("**Evening:**"):
            section = None
            continue
        if section == "radar" and line.startswith("- "):
            plan.radar.append(line[2:])
        elif section == "warnings" and line.startswith("- "):
            plan.warnings.append(line[2:])
        elif section == "block":
            b = plan.blocks[-1]
            item = None if in_fence else _ITEM_RE.match(line)
            if item:
                b.add_item(item.group("text"), item.group("done") == "x",
                           [r for r in (item.group("refs") or "").split("|") if r])
            elif not in_fence and line.startswith("<!--action:") and b.items:
                action = json.loads(line[len("<!--action:"):-3])
                b.items[-1].first_action = action.get("first_action", "")
                b.items[-1].action_url = action.get("action_url", "")
            elif not in_fence and line.startswith("<!--decisions:") and b.items:
                b.items[-1].decisions = json.loads(line[len("<!--decisions:"):-3])
            elif not in_fence and line.startswith("<!--decision-label-->"):
                pass
            elif not in_fence and line.startswith("**First action (2 min):** "):
                b.first_action = line[len("**First action (2 min):** "):]
            elif not in_fence and line.startswith("Open: "):
                b.links = [{"label": la, "url": u} for la, u in re.findall(r"\[([^\]]*)\]\(([^)]*)\)", line[6:])]
            elif not in_fence and line.startswith("Log: `") and line.endswith("`"):
                b.log_hint = line[6:-1]
            elif line or b.description:
                if line.startswith("```"):
                    in_fence = not in_fence
                b.description.append(line)
    for b in plan.blocks:
        while b.description and not b.description[-1]:
            b.description.pop()
    return plan


# ---------------------------------------------------------------- done state from the log

def apply_log(plan: Plan, targets: set[str]) -> Plan:
    """the user items and blocks whose id is a netted `done` target in the log. The hosted page (hub) appends log rows
    without touching the plan JSON, so the board and the hub both read the log at render time."""
    for b in plan.blocks:
        if b.id in targets:
            b.done = True
        for i in b.items:
            if i.id in targets:
                i.done = True
    return plan


# ---------------------------------------------------------------- reading and writing the day

def plan_paths(root: Path, date: str) -> tuple[Path, Path]:
    return root / "plans" / f"{date}.json", root / "plans" / f"{date}.md"


def load_plan(root: Path, date: str) -> Plan | None:
    """The JSON is the source of truth; fall back to parsing the markdown for days written before the board."""
    js, md = plan_paths(root, date)
    if js.exists():
        return from_dict(json.loads(js.read_text()))
    if md.exists():
        return parse_md(md.read_text())
    return None


def save_plan(root: Path, plan: Plan) -> tuple[Path, Path]:
    """Write both files. The markdown is always rendered from the structured plan."""
    js, md = plan_paths(root, plan.date)
    js.parent.mkdir(parents=True, exist_ok=True)
    js.write_text(json.dumps(to_dict(plan), ensure_ascii=False, indent=2) + "\n")
    md.write_text(render_md(plan))
    return js, md


def write_plan(root: Path, plan: Plan, *, force: bool = False) -> Path:
    js, md = plan_paths(root, plan.date)
    if md.exists() and not force:
        raise FileExistsError(f"{md} exists (finished by the agent?); pass --force to overwrite")
    save_plan(root, plan)
    return md


def parse_done_text(text: str) -> list[dict[str, Any]]:
    """Lines like `done leetcode 70 two DP problems` or `done stack 25 term=Kafka` -> log entries."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(?:done\s+)?(\w+)\s+(\d+)\s*(.*)$", line, re.IGNORECASE)
        if not m:
            raise ValueError(f"cannot parse done line: {line!r}")
        block, minutes, note = m.group(1).lower(), int(m.group(2)), m.group(3)
        if block not in logmod.BLOCKS:
            raise ValueError(f"unknown block {block!r} (expected one of {logmod.BLOCKS})")
        entry: dict[str, Any] = {"block": block, "minutes": minutes, "note": note}
        t = re.search(r"term=(\S+)", note)
        if t:
            entry["term"] = t.group(1)
        out.append(entry)
    return out
