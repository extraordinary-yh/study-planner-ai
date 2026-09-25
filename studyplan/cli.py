"""Command-line entry point: uv run studyplan <command>."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from . import log as logmod
from .config import load_catalog, load_curriculum, load_feeds, load_level, load_preferences, load_terms, parent_root, repo_root
from .curriculum import load_progress, mark_done, next_lessons, save_progress, summary
from .jobs import applications, latest_snapshot, upcoming_interviews
from .leetcode import append_attempts, due_reviews, format_stats, load_attempts, parse_file, parse_rows, topic_stats
from .news import fetch_feeds, rank
from .news import format_items as format_news
from .plan import (Block, Event, build_plan, day_slots, load_plan, parse_done_text, place_blocks,
                   quality_issues, save_plan, shrink_to_free, snap, to_hhmm, to_minutes, write_plan)
from .remnote import DEFAULT_ENDPOINT, fetch_catalog, mark_history_coverage, write_catalog
from .stack import count_terms, format_table, scopes_for


def _today(args) -> str:
    return getattr(args, "date", None) or dt.date.today().isoformat()


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def cmd_status(args) -> int:
    root, parent, today = repo_root(), parent_root(repo_root()), _today(args)
    prefs, level = load_preferences(root), load_level(root)
    print(f"assessed: {level.get('assessed_on') or 'never (run the assessment interview)'}")
    snap, jobs = latest_snapshot(parent)
    apps = applications(parent / "data" / "feedback.jsonl")
    print(f"job snapshot: {snap or 'none'} ({len(jobs)} jobs, {len(apps)} applications)")
    for iv in upcoming_interviews(apps, jobs, today, int((prefs.get('modifiers') or {}).get('interview_within_days', 7)),
                                  (level.get("goals") or {}).get("interview_dates")):
        print(f"  interview: {iv['company']} {iv['stage']} {'on' if iv['dated'] else 'stage set'} {iv['when']}")
    attempts = load_attempts(root / "data" / "leetcode.jsonl")
    lc = prefs.get("leetcode") or {}
    due = due_reviews(attempts, lc.get("review_intervals_days") or {}, today)
    print(f"leetcode: {len(attempts)} attempts on file, {len(due)} reviews due")
    nxt = next_lessons(load_curriculum(root), load_progress(root / "data" / "progress.json"), 1)
    print(f"system design: next {nxt[0]['id']} {nxt[0]['title']}" if nxt else "system design: syllabus complete")
    week = logmod.week_summary(logmod.load(root / "data" / "log.jsonl"), today)
    print(f"last 7 days: {week['total']} min over {week['active_days']} days, streak {week['streak']}; "
          + ", ".join(f"{k} {v}" for k, v in week["minutes"].items() if v))
    plan_path = root / "plans" / f"{today}.md"
    print(f"today's plan: {plan_path if plan_path.exists() else 'not written (uv run studyplan plan)'}")
    return 0


def cmd_stack(args) -> int:
    root, parent = repo_root(), parent_root(repo_root())
    prefs, level = load_preferences(root), load_level(root)
    snap, jobs = latest_snapshot(parent)
    if not jobs:
        print("no job snapshot under ../data/jobs; run the job-radar scan first")
        return 1
    apps = applications(parent / "data" / "feedback.jsonl")
    scopes = scopes_for(jobs, set(apps), args.min_score or int((prefs.get("stack") or {}).get("top_min_score", 75)))
    rows = count_terms(load_terms(root), scopes, {k: str(v) for k, v in (level.get("stack") or {}).items()})
    if args.gaps:
        rows = [r for r in rows if r.gap]
    print(f"snapshot {snap}: applied {len(scopes['applied'])}, top (score ≥ {args.min_score or (prefs.get('stack') or {}).get('top_min_score', 75)}) "
          f"{len(scopes['top'])}, all visible {len(scopes['all'])}")
    print(format_table(rows, args.limit or int((prefs.get("stack") or {}).get("max_terms", 25))))
    return 0


def cmd_leetcode(args) -> int:
    root, today = repo_root(), _today(args)
    prefs = load_preferences(root)
    lc = prefs.get("leetcode") or {}
    path = root / "data" / "leetcode.jsonl"
    if args.leetcode_cmd == "import":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        attempts = parse_file(text, lc.get("clean_bar_minutes") or {}, today, load_catalog(root))
        n = append_attempts(path, attempts)
        print(f"parsed {len(attempts)} attempts, appended {n} new to {path}")
        return 0
    if args.leetcode_cmd == "add":
        row = {"problem": args.problem, "url": args.url, "difficulty": args.difficulty, "topics": args.topics, "number": args.number,
               "minutes": args.minutes, "stuck": "yes" if args.stuck else "no", "outcome": args.outcome, "date": args.date, "notes": args.note}
        attempts = parse_rows([row], lc.get("clean_bar_minutes") or {}, today, load_catalog(root))
        n = append_attempts(path, attempts)
        a = attempts[0]
        print(f"{'added' if n else 'already on file'}: {a['title']} ({a['difficulty']}, {', '.join(a['topics']) or 'untagged'}) {a['outcome']} {a.get('minutes') or '?'} min")
        return 0
    attempts = load_attempts(path)
    ratings = ((load_level(root).get("coding") or {}).get("topics") or {})
    stats = topic_stats(attempts, today, int(lc.get("weakness_window_days", 90)), int(lc.get("coverage_min_attempts", 3)), ratings)
    if args.leetcode_cmd == "due":
        rows = due_reviews(attempts, lc.get("review_intervals_days") or {}, today, weakness={s["topic"]: s["weakness"] for s in stats})
        if not rows:
            print("nothing due")
        elif len(rows) > args.limit:
            print(f"{len(rows)} due; showing {args.limit} (failed first, then weakest topics; --limit N for more)")
            rows = rows[:args.limit]
        for r in rows:
            print(f"  {r['difficulty']:<6} {r['title']:<40} last {r['last_outcome']:<8} {r['last_date']}  due {r['due']}  ({', '.join(r['topics']) or 'untagged'})")
        return 0
    print(f"{len(attempts)} attempts, {len({a['slug'] for a in attempts})} problems")
    print(format_stats(stats))
    return 0


def cmd_curriculum(args) -> int:
    root = repo_root()
    modules, progress = load_curriculum(root), load_progress(root / "data" / "progress.json")
    if args.all:
        print(summary(modules, progress))
    else:
        for lesson in next_lessons(modules, progress, 3):
            print(f"{lesson['id']:<6} {lesson['title']} ({lesson.get('minutes', 45)} min) — {lesson['module']}")
    return 0


def cmd_lesson(args) -> int:
    root, today = repo_root(), _today(args)
    modules = load_curriculum(root)
    ids = {lesson["id"] for m in modules for lesson in m.get("lessons", [])}
    if args.lesson_id not in ids:
        print(f"unknown lesson {args.lesson_id!r}; see `uv run studyplan curriculum --all`")
        return 1
    path = root / "data" / "progress.json"
    progress = load_progress(path)
    mark_done(progress, args.lesson_id, today, args.minutes, args.note or "")
    save_progress(path, progress)
    logmod.append(root / "data" / "log.jsonl", {"ts": _now_iso(), "date": today, "block": "design", "minutes": args.minutes or 0,
                                                 "note": args.note or "", "lesson": args.lesson_id})
    nxt = next_lessons(modules, progress, 1)
    print(f"done {args.lesson_id}; next: {nxt[0]['id']} {nxt[0]['title']}" if nxt else f"done {args.lesson_id}; syllabus complete")
    return 0


def _news(root: Path, args):
    prefs = load_preferences(root)
    np = prefs.get("news") or {}
    items, warnings = fetch_feeds(load_feeds(root), fixtures=Path(args.fixtures) if args.fixtures else None,
                                  raw_dir=root / "data" / "raw" / _today(args))
    now = dt.datetime.now(dt.timezone.utc) if not args.now else dt.datetime.fromisoformat(args.now).astimezone(dt.timezone.utc)
    ranked = rank(items, np.get("topics") or {}, now=now, hours=args.hours or int(np.get("hours", 36)),
                  limit=args.limit or int(np.get("max_items", 12)))
    return ranked, warnings


def cmd_news(args) -> int:
    ranked, warnings = _news(repo_root(), args)
    print(format_news(ranked) if ranked else "no items in the window")
    for w in warnings:
        print(f"WARNING: {w}")
    return 0 if ranked or not warnings else 1


def _edit_plan(root: Path, date: str):
    plan = load_plan(root, date)
    if plan is None:
        print(f"no plan for {date}: run `uv run studyplan plan --date {date}` first")
        return None
    return plan


def cmd_plan_set_call(args) -> int:
    root, today = repo_root(), _today(args)
    plan = _edit_plan(root, today)
    if plan is None:
        return 1
    plan.call = args.text
    js, md = save_plan(root, plan)
    print(f"today's call: {plan.call}\nwrote {js} and {md}")
    return 0


def cmd_plan_add_item(args) -> int:
    root, today = repo_root(), _today(args)
    plan = _edit_plan(root, today)
    if plan is None:
        return 1
    block = plan.block(args.block)
    if block is None:
        print(f"unknown block {args.block!r} (have: {', '.join(b.id for b in plan.blocks)})")
        return 1
    item = block.add_item(args.text)
    save_plan(root, plan)
    print(f"added {item.id} to {block.id}: {item.text}")
    return 0


def cmd_plan_set_block(args) -> int:
    root, today = repo_root(), _today(args)
    plan = _edit_plan(root, today)
    if plan is None:
        return 1
    block = plan.block(args.block)
    if block is None:
        if not args.title:
            print(f"unknown block {args.block!r}; pass --title to create it (have: {', '.join(b.id for b in plan.blocks)})")
            return 1
        block = Block(args.block, args.title, args.minutes or 20)
        plan.blocks.append(block)
    if args.title:
        block.title = args.title
    if args.minutes is not None:
        block.minutes = max(snap(args.minutes), 5)
    if args.start:
        try:
            start = snap(to_minutes(args.start))
        except ValueError:
            print(f"bad --start {args.start!r} (expected HH:MM)")
            return 1
        block.start = f"{start // 60:02d}:{start % 60:02d}"
    if args.start or args.minutes is not None:
        plan.arranged = True                       # a calendar refresh will not re-lay what the user placed himself
    if args.first_action:
        block.first_action = args.first_action
    save_plan(root, plan)
    print(f"{block.id}: {block.title} · {block.start or '(unplaced)'}–{block.end or '?'} · {block.minutes} min")
    return 0


def cmd_plan_import_events(args) -> int:
    """Take what the agent's Google Calendar MCP returned and put the day's events on the plan."""
    from .gcal import parse_mcp_events
    root, today = repo_root(), _today(args)
    plan = _edit_plan(root, today)
    if plan is None:
        return 1
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
    try:
        events, warnings = parse_mcp_events(text, today, timezone=args.timezone, calendar=args.calendar or "")
    except (ValueError, json.JSONDecodeError) as e:
        print(f"cannot read the calendar payload: {e}")
        return 1
    kept = [e for e in plan.events if e.source != "gcal"] if args.replace else list(plan.events)
    by_key = {e.gcal_id or e.id: e for e in kept}
    for e in events:
        by_key[e.gcal_id or e.id] = e                       # a re-import updates in place, never duplicates
    plan.events = sorted(by_key.values(), key=lambda e: (not e.all_day, e.start, e.title))
    if not args.keep_times:
        place_blocks(plan.blocks, day_slots(load_level(root)), plan.busy)
    save_plan(root, plan)
    for w in warnings:
        print(f"WARNING: {w}")
    print(f"{len(events)} event(s) from the calendar; {len(plan.events)} on the plan for {today}")
    for e in plan.events:
        print(f"  {'all day ' if e.all_day else e.start + '–' + e.end} {e.title}" + (f" · {e.calendar}" if e.calendar else ""))
    if not args.keep_times:
        print("blocks re-laid around them:")
        for b in plan.blocks:
            print(f"  {b.start}–{b.end} {b.id} ({b.minutes} min)")
    return 0


def cmd_plan_refresh(args) -> int:
    """Re-fetch the iCal feeds and sf-events into today's plan; re-lay the blocks only if a busy range changed."""
    from . import busy
    root, parent, today = repo_root(), parent_root(repo_root()), _today(args)
    plan = _edit_plan(root, today)
    if plan is None:
        return 1
    if args.keep_times:
        plan.arranged = True
    changed, warnings = busy.refresh(root, parent, plan)
    save_plan(root, plan)
    for w in warnings:
        print(f"WARNING: {w}")
    outside = [e for e in plan.events if e.source in busy.REFRESHED_SOURCES]
    print(f"{len(outside)} outside event(s) on the plan for {today} ({len(plan.events)} in all); "
          + ("busy ranges changed" if changed else "nothing changed")
          + (", blocks left where the user put them" if changed and plan.arranged else ""))
    for e in plan.events:
        print(f"  {'all day ' if e.all_day else e.start + '–' + e.end} {e.title} · {e.source}")
    return 0


def cmd_plan_add_event(args) -> int:
    """One commitment by hand, for when it is not on the calendar (or the MCP is not connected)."""
    from .gcal import event_id
    root, today = repo_root(), _today(args)
    plan = _edit_plan(root, today)
    if plan is None:
        return 1
    try:
        start = to_hhmm(snap(to_minutes(args.start)))
    except ValueError:
        print(f"bad --start {args.start!r} (expected HH:MM)")
        return 1
    event = Event(event_id(f"{args.title}@{today}@{start}"), args.title, start, max(snap(args.minutes), 5),
                  args.calendar or "manual", args.url or "", args.location or "", False, not args.free)
    plan.events = sorted([e for e in plan.events if e.id != event.id] + [event],
                         key=lambda e: (not e.all_day, e.start, e.title))
    if not args.keep_times:
        place_blocks(plan.blocks, day_slots(load_level(root)), plan.busy)
    save_plan(root, plan)
    print(f"added {event.id}: {event.start}–{event.end} {event.title}" + ("" if event.busy else " (free)"))
    return 0


def cmd_plan_promote_event(args) -> int:
    """Put a calendar event on the task list as a real block: tickable, logged, with its own items."""
    root, today = repo_root(), _today(args)
    plan = _edit_plan(root, today)
    if plan is None:
        return 1
    event = plan.event(args.event)
    if event is None:
        print(f"unknown event {args.event!r} (have: {', '.join(e.id + ' ' + e.title for e in plan.events) or 'none'})")
        return 1
    block_id = args.id or event.id
    if plan.block(block_id) is not None:
        print(f"block {block_id!r} already exists")
        return 1
    block = Block(block_id, args.title or event.title, event.minutes, event.start)
    if event.url:
        block.links.append({"label": "calendar event", "url": event.url})
    if event.location:
        block.description.append(f"Where: {event.location}")
    if args.first_action:
        block.first_action = args.first_action
    plan.blocks.append(block)
    plan.blocks.sort(key=lambda b: to_minutes(b.start or "23:59"))
    event.busy = False                          # the block now occupies that time; don't reserve it twice
    save_plan(root, plan)
    print(f"{block.id}: {block.title} · {block.start}–{block.end} · {block.minutes} min (from {event.id})")
    print(f"add its steps with: uv run studyplan plan add-item {block.id} \"...\"")
    return 0


def cmd_hub(args) -> int:
    """Render the hub site (index.json, month / week / day pages, copied files) into site/hub/."""
    from .hub import build_site
    root, parent, today = repo_root(), parent_root(repo_root()), _today(args)
    out = Path(args.out) if args.out else root / "site" / "hub"
    info = build_site(root, parent, out, today, report_base=args.report_base, events_base=args.events_base,
                      write=False)
    print(f"hub: {info['days']} day(s) ({info['planned']} with a plan), {info['weeks']} week(s), {info['months']} month(s), "
          f"{info['files']} file page(s) -> {out}")
    print(f"  open: {out / 'index.html'} (this week: {out / 'week' / (info['current_week'] + '.html')})")
    return 0


def cmd_serve(args) -> int:
    from .board import serve
    root, today = repo_root(), _today(args)
    if load_plan(root, today) is None:
        print(f"no plan for {today}: run `uv run studyplan --date {today} plan` first")
        return 1
    try:
        return serve(root, today, port=args.port, host=args.host)
    except ValueError as e:
        print(e)
        return 1


def cmd_plan(args) -> int:
    root, parent, today = repo_root(), parent_root(repo_root()), _today(args)
    items, warnings = ([], [])
    if not args.no_news:
        try:
            items, warnings = _news(root, args)
        except Exception as e:  # noqa: BLE001 - news is optional
            warnings = [f"{type(e).__name__}: {e}"]
    from . import busy
    existing = load_plan(root, today)                # a rewrite keeps the day's calendar events
    events = list(existing.events) if existing else []
    outside, busy_warnings = busy.outside_events(root, parent, today)      # iCal feeds + sf-events, if configured
    events = busy.merge_events(events, outside)
    plan = build_plan(root, parent, today, minutes=args.minutes, news_items=items, news_warnings=warnings,
                      events=events)
    plan.warnings.extend(busy_warnings)
    shrink_to_free(plan, day_slots(load_level(root)))
    try:
        path = write_plan(root, plan, force=args.force)
    except FileExistsError as e:
        print(e)
        return 2
    print(path.read_text())
    print(f"Plan: {path} (structured: {path.with_suffix('.json')})")
    print(f"Board: uv run studyplan serve --date {today}")
    return 0


def cmd_plan_check(args) -> int:
    """Lint the finished board before the user sees it or it is committed."""
    root, today = repo_root(), _today(args)
    plan = load_plan(root, today)
    if plan is None:
        print(f"no plan for {today}: run `uv run studyplan --date {today} plan` first")
        return 1
    issues = quality_issues(plan)
    if issues:
        print(f"Plan clarity check: FAIL ({len(issues)} issue(s))")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    print(f"Plan clarity check: PASS — {len(plan.blocks)} blocks have a direct first action, task-level time boxes, and a stated purpose.")
    return 0


def cmd_done(args) -> int:
    root, today = repo_root(), _today(args)
    if args.block == "-":
        entries = parse_done_text(sys.stdin.read())
    else:
        if args.block not in logmod.BLOCKS:
            print(f"unknown block {args.block!r} (expected one of {logmod.BLOCKS})")
            return 1
        entries = [{"block": args.block, "minutes": args.minutes or 0, "note": args.note or ""}]
        if args.term:
            entries[0]["term"] = args.term
    for e in entries:
        logmod.append(root / "data" / "log.jsonl", {"ts": _now_iso(), "date": today, **e})
        print(f"logged {e['block']} {e['minutes']} min" + (f" · {e['note']}" if e.get("note") else ""))
    return 0


def cmd_assess(args) -> int:
    root = repo_root()
    level = load_level(root)
    missing = []

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{prefix}{k}.")
        elif node is None:
            missing.append(prefix.rstrip("."))
    walk({k: v for k, v in level.items() if k != "assessed_on"})
    print(f"assessed_on: {level.get('assessed_on') or 'never'}")
    print(f"{len(missing)} unanswered fields" + (": " + ", ".join(missing[:40]) if missing else ""))
    print(f"questions: {root / 'profile' / 'assessment.md'}; answers go to {root / 'profile' / 'level.yaml'}")
    return 0


def cmd_remnote(args) -> int:
    root = repo_root()
    authorization = os.environ.get("REMNOTE_MCP_AUTH")
    if not authorization:
        print("REMNOTE_MCP_AUTH is required (set it to the full Authorization header value, including 'Bearer ')")
        return 2
    output = Path(args.output) if args.output else root / "data" / "remnote-problem-catalog.jsonl"
    from .config import _yaml
    records = fetch_catalog(args.endpoint, authorization, _yaml(root / "config" / "remnote.yaml").get("collections"))
    mark_history_coverage(records, load_attempts(root / "data" / "leetcode.jsonl"))
    write_catalog(output, records)
    mapped = sum(bool(row.get("remnote_doc_id")) for row in records)
    covered = sum(bool(row.get("in_imported_history")) for row in records)
    print(f"wrote {len(records)} note-only problems to {output}; {mapped} have RemNote document ids, "
          f"{covered} appear in imported history")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="studyplan", description="daily interview-prep coach")
    p.add_argument("--date", help="pretend today is this ISO date")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="level, interviews, due reviews, next lesson, last week").set_defaults(func=cmd_status)

    st = sub.add_parser("stack", help="tech-stack GCD over applied / top / all postings")
    st.add_argument("--min-score", type=int, help="score floor for the 'top' scope")
    st.add_argument("--limit", type=int)
    st.add_argument("--gaps", action="store_true", help="only terms below working depth")
    st.set_defaults(func=cmd_stack)

    lc = sub.add_parser("leetcode", help="import history, list due reviews, per-topic stats")
    lcs = lc.add_subparsers(dest="leetcode_cmd", required=True)
    li = lcs.add_parser("import", help="CSV or JSON export; '-' for stdin")
    li.add_argument("file")
    la = lcs.add_parser("add", help="log one attempt")
    la.add_argument("--problem", required=True)
    la.add_argument("--url")
    la.add_argument("--number", type=int, help="LeetCode problem number; fills difficulty and topics from config/leetcode_catalog.yaml")
    la.add_argument("--difficulty", choices=["easy", "medium", "hard"], help="default: the catalog entry, else medium")
    la.add_argument("--topics", default="", help="comma-separated LeetCode tags")
    la.add_argument("--minutes", type=int)
    la.add_argument("--stuck", action="store_true")
    la.add_argument("--outcome", choices=["clean", "slow", "hint", "solution", "gave_up"], help="derived from minutes and --stuck when omitted")
    la.add_argument("--note", default="")
    ldu = lcs.add_parser("due", help="problems to redo today")
    ldu.add_argument("--limit", type=int, default=20)
    lcs.add_parser("stats", help="per-topic attempts, clean share, weakness")
    lc.set_defaults(func=cmd_leetcode)

    cu = sub.add_parser("curriculum", help="next system-design lessons (--all for the whole ladder)")
    cu.add_argument("--all", action="store_true")
    cu.set_defaults(func=cmd_curriculum)
    le = sub.add_parser("lesson", help="record a lesson: lesson done <id>")
    les = le.add_subparsers(dest="lesson_cmd", required=True)
    ld = les.add_parser("done")
    ld.add_argument("lesson_id")
    ld.add_argument("--minutes", type=int)
    ld.add_argument("--note")
    le.set_defaults(func=cmd_lesson)

    nw = sub.add_parser("news", help="fetch the feeds and rank by topic keywords")
    nw.add_argument("--hours", type=int)
    nw.add_argument("--limit", type=int)
    nw.add_argument("--fixtures", help="directory of <key>.xml files instead of the network")
    nw.add_argument("--now", help="ISO timestamp to rank against (tests)")
    nw.set_defaults(func=cmd_news)

    pl = sub.add_parser("plan", help="write plans/<date>.md (the skeleton the agent finishes)")
    pl.add_argument("--minutes", type=int, help="override the day's budget")
    pl.add_argument("--no-news", action="store_true", help="skip the feed fetch")
    pl.add_argument("--force", action="store_true", help="overwrite an existing plan")
    pl.add_argument("--fixtures")
    pl.add_argument("--now")
    pl.add_argument("--hours", type=int)
    pl.add_argument("--limit", type=int)
    pl.set_defaults(func=cmd_plan)
    pls = pl.add_subparsers(dest="plan_cmd")          # optional: `plan` alone still writes the skeleton
    sc = pls.add_parser("set-call", help="write the one-sentence 'Today's call'")
    sc.add_argument("text")
    sc.set_defaults(func=cmd_plan_set_call)
    ai = pls.add_parser("add-item", help="append a checkbox item to a block")
    ai.add_argument("block")
    ai.add_argument("text")
    ai.set_defaults(func=cmd_plan_add_item)
    sb = pls.add_parser("set-block", help="move, resize, retitle or create a block (--title creates it)")
    sb.add_argument("block")
    sb.add_argument("--start", help="HH:MM, snapped to 5 minutes")
    sb.add_argument("--minutes", type=int, help="snapped to 5 minutes")
    sb.add_argument("--title")
    sb.add_argument("--first-action", dest="first_action", help="the two-minute opener shown at the top of the block")
    sb.set_defaults(func=cmd_plan_set_block)
    ie = pls.add_parser("import-events", help="load the day's Google Calendar events (the agent pipes the MCP result in)")
    ie.add_argument("file", nargs="?", default="-", help="a file of MCP/Calendar JSON, or '-' for stdin (default)")
    ie.add_argument("--timezone", help="IANA zone to show the events in; default: the payload's own timeZone")
    ie.add_argument("--calendar", help="label for these events; default: the payload's calendar name")
    ie.add_argument("--no-replace", dest="replace", action="store_false",
                    help="merge into the events already on the plan instead of replacing the imported ones")
    ie.add_argument("--keep-times", action="store_true", help="do not re-lay the study blocks around the events")
    ie.set_defaults(func=cmd_plan_import_events)
    rf = pls.add_parser("refresh", help="re-fetch the iCal feeds (STUDYPLAN_ICAL_URLS) and sf-events into the plan")
    rf.add_argument("--keep-times", action="store_true", help="mark the day as arranged by hand: never re-lay the blocks")
    rf.set_defaults(func=cmd_plan_refresh)
    ae = pls.add_parser("add-event", help="add one commitment by hand")
    ae.add_argument("title")
    ae.add_argument("--start", required=True, help="HH:MM")
    ae.add_argument("--minutes", type=int, required=True)
    ae.add_argument("--url")
    ae.add_argument("--location")
    ae.add_argument("--calendar")
    ae.add_argument("--free", action="store_true", help="show it but let study blocks be placed over it")
    ae.add_argument("--keep-times", action="store_true")
    ae.set_defaults(func=cmd_plan_add_event)
    pe = pls.add_parser("promote-event", help="turn a calendar event into a tickable block on the task list")
    pe.add_argument("event", help="event id (ev-…) or the Google event id")
    pe.add_argument("--id", help="block id to create; default: the event's id")
    pe.add_argument("--title")
    pe.add_argument("--first-action", dest="first_action")
    pe.set_defaults(func=cmd_plan_promote_event)
    ck = pls.add_parser("check", help="verify that every board block has a direct first action and clear time boxes")
    ck.set_defaults(func=cmd_plan_check)

    hb = sub.add_parser("hub", help="the static study hub: hub build -> site/hub/")
    hbs = hb.add_subparsers(dest="hub_cmd", required=True)
    hbb = hbs.add_parser("build", help="write index.json and the month / week / day pages")
    hbb.add_argument("--out", help="output directory (default: site/hub under the sub-project)")
    hbb.add_argument("--read-only", dest="read_only", action="store_true",
                     help="build without the hosted write path (no GitHub token drawer, check-off disabled)")
    hbb.add_argument("--report-base", dest="report_base", default="../../archive",
                     help="where the day's job report lives relative to day/ pages (default: the published site's archive/)")
    hbb.add_argument("--events-base", dest="events_base", default="../../events",
                     help="where the day's event report lives relative to day/ pages (default: the published site's events/)")
    hb.set_defaults(func=cmd_hub)

    sv = sub.add_parser("serve", help="open the day board at http://127.0.0.1:8765/ (loopback only)")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--host", default="127.0.0.1", help="loopback addresses only; anything else is refused")
    sv.set_defaults(func=cmd_serve)

    dn = sub.add_parser("done", help="log a finished block: done <block> --minutes N; '-' reads 'done <block> <min> <note>' lines from stdin")
    dn.add_argument("block")
    dn.add_argument("--minutes", type=int)
    dn.add_argument("--note")
    dn.add_argument("--term", help="stack term studied (keeps it out of the next two weeks of plans)")
    dn.set_defaults(func=cmd_done)

    sub.add_parser("assess", help="which level.yaml fields are still unanswered").set_defaults(func=cmd_assess)

    rn = sub.add_parser("remnote", help="refresh source-linked context from the local RemNote MCP server")
    rns = rn.add_subparsers(dest="remnote_cmd", required=True)
    rs = rns.add_parser("sync", help="write the note-only company problem catalog")
    rs.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    rs.add_argument("--output")
    rn.set_defaults(func=cmd_remnote)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
