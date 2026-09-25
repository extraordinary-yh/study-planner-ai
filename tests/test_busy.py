"""The calendar that refreshes itself: iCal feeds and sf-events as busy time, the cache, and the no-relayout rule."""
import json
import urllib.request
from pathlib import Path

import yaml

from studyplan import busy
from studyplan.board import api_busy, serve_in_thread
from studyplan.cli import main
from studyplan.gcal import parse_mcp_events
from studyplan.plan import Block, Plan, build_plan, free_minutes, load_plan, parse_md, plan_paths, render_md, save_plan, shrink_to_free, to_dict

FIX = Path(__file__).resolve().parent / "fixtures"
ICS = FIX / "ical" / "calendar.ics"
SLOTS = [{"start": "13:00", "minutes": 180}, {"start": "19:00", "minutes": 120}]


def with_slots(root):
    level = yaml.safe_load((root / "profile" / "level.yaml").read_text())
    level["constraints"]["slots"] = SLOTS
    (root / "profile" / "level.yaml").write_text(yaml.safe_dump(level))


# ---------------------------------------------------------------- configuration and fetching

def test_urls_come_from_the_environment_or_the_repo_root_dotenv(roots, monkeypatch):
    root, parent = roots
    monkeypatch.delenv(busy.ENV_URLS, raising=False)
    assert busy.ical_urls(parent) == []
    (parent / ".env").write_text("JOBRADAR_PASSPHRASE='x y'\nSTUDYPLAN_ICAL_URLS=\"https://a/basic.ics, https://b/basic.ics\"\n")
    assert busy.ical_urls(parent) == ["https://a/basic.ics", "https://b/basic.ics"]
    monkeypatch.setenv(busy.ENV_URLS, str(ICS))
    assert busy.ical_urls(parent) == [str(ICS)]                       # the environment wins


def test_no_feed_configured_is_silent(roots, monkeypatch):
    root, parent = roots
    monkeypatch.delenv(busy.ENV_URLS, raising=False)
    assert busy.calendar_events(root, parent, "2026-09-07") == ([], [])
    assert not (root / "data" / busy.CACHE).exists()


def test_feed_from_a_path_fills_the_cache(roots, monkeypatch):
    root, parent = roots
    events, warnings = busy.calendar_events(root, parent, "2026-09-07", urls=[str(ICS)], now="2026-09-07T08:00:00")
    assert not warnings or all("monthly" in w or "No start" in w for w in warnings)
    titles = {e.title for e in events}
    assert {"Call with a recruiter", "Team sync", "Labor Day"} <= titles and all(e.source == "ical" for e in events)
    cache = json.loads((root / "data" / busy.CACHE).read_text())
    assert cache[str(ICS)]["fetched"] == "2026-09-07T08:00:00" and "BEGIN:VCALENDAR" in cache[str(ICS)]["text"]
    assert busy.calendar_events(root, parent, "2026-09-07", urls=["file://" + str(ICS)])[0]


def test_feed_failure_warns_once_and_uses_the_cache(roots, monkeypatch):
    root, parent = roots
    url = "https://calendar.google.com/calendar/ical/secret-abc123/basic.ics"

    def boom(u, timeout=15.0):
        raise OSError("no network in tests")
    monkeypatch.setattr(busy, "_http_get", boom)
    events, warnings = busy.calendar_events(root, parent, "2026-09-07", urls=[url])
    assert events == [] and len(warnings) == 1 and "no cached copy yet" in warnings[0]
    assert "secret-abc123" not in warnings[0]                         # the secret address is never echoed in full

    busy.save_cache(root, {url: {"fetched": "2026-09-06T07:00:00", "text": ICS.read_text()}})
    events, warnings = busy.calendar_events(root, parent, "2026-09-07", urls=[url])
    assert {e.title for e in events} >= {"Call with a recruiter"}
    fetch_warnings = [w for w in warnings if "no network in tests" in w]
    assert len(fetch_warnings) == 1 and "using the copy fetched 2026-09-06T07:00:00" in fetch_warnings[0]

    monkeypatch.setattr(busy, "_http_get", lambda u, timeout=15.0: "<html>login</html>")
    _, warnings = busy.calendar_events(root, parent, "2026-09-07", urls=[url])
    assert warnings and "not an iCalendar feed" in warnings[0]        # a login page is not a calendar


# ---------------------------------------------------------------- sf-events going -> busy

def test_going_items_become_busy_entries_with_the_event_url(roots):
    root, parent = roots
    sat = busy.going_events(parent, "2026-09-12")
    by_title = {e.title: e for e in sat}
    assert set(by_title) == {"Agents hackathon", "Founders dinner"}    # `skip` stays off the board
    hack = by_title["Agents hackathon"]
    assert (hack.start, hack.minutes, hack.busy, hack.source, hack.calendar) == ("10:00", 600, True, "sf-events", "sf-events")
    assert hack.url == "https://lu.ma/agents-hack" and hack.location == "Frontier Tower, 995 Market St, San Francisco"
    assert hack.id.startswith("ev-") and hack.gcal_id == ""
    dinner = by_title["Founders dinner"]
    assert dinner.all_day and not dinner.busy                          # unknown time: a chip, never a block
    assert [e.title for e in busy.going_events(parent, "2026-09-05")] == ["Agents meetup"]   # `went` counts on its day
    assert busy.going_events(parent, "2026-09-07") == []


# ---------------------------------------------------------------- merging, refreshing, the budget

def test_merge_replaces_refreshed_sources_and_keeps_the_rest(roots):
    root, parent = roots
    mcp, _ = parse_mcp_events((FIX / "gcal_day.json").read_text(), "2026-09-07")
    manual = busy.Event("ev-manual", "Gym", "07:00", 60, "manual", busy=False)
    old = busy.Event("ev-old", "Stale feed entry", "10:00", 30, source="ical")
    fresh, _ = busy.calendar_events(root, parent, "2026-09-07", urls=[str(ICS)])
    merged = busy.merge_events(mcp + [manual, old], fresh)
    titles = [e.title for e in merged]
    assert "Stale feed entry" not in titles and "Gym" in titles
    assert titles.count("Call with a recruiter") == 1                  # same Google id from both paths: one entry
    assert next(e for e in merged if e.title == "Call with a recruiter").source == "ical"


def test_refresh_relays_only_when_busy_changed_and_not_arranged(roots, monkeypatch):
    root, parent = roots
    with_slots(root)
    plan = build_plan(root, parent, "2026-09-07")
    before = [(b.id, b.start) for b in plan.blocks]
    changed, _ = busy.refresh(root, parent, plan, urls=[])            # nothing configured, nothing to do
    assert changed is False and [(b.id, b.start) for b in plan.blocks] == before

    changed, _ = busy.refresh(root, parent, plan, urls=[str(ICS)])    # 13:30–14:00 Team sync lands in the slot
    assert changed is True and (810, 840) in plan.busy
    laid = [(b.id, b.start) for b in plan.blocks]
    assert laid != before
    for b in plan.blocks:
        assert not (b.start < "14:00" and b.end > "13:30"), (b.id, b.start, b.end)

    changed, _ = busy.refresh(root, parent, plan, urls=[str(ICS)])    # same feed again: no change, no relayout
    assert changed is False and [(b.id, b.start) for b in plan.blocks] == laid

    plan.arranged = True
    plan.blocks[0].start = "13:35"                                     # the user dragged News onto the meeting on purpose
    changed, _ = busy.refresh(root, parent, plan, urls=[])            # feed gone: busy changes, layout stays
    assert changed is True and plan.blocks[0].start == "13:35"


def test_budget_shrinks_to_the_free_slots(roots):
    root, parent = roots
    with_slots(root)
    assert free_minutes(SLOTS, []) == 300 and free_minutes(SLOTS, [(13 * 60 + 30, 14 * 60)]) == 270
    hack = busy.going_events(parent, "2026-09-12")
    plan = build_plan(root, parent, "2026-09-12", events=hack)         # a Saturday: 240 min budget
    assert plan.minutes == 240
    cut = shrink_to_free(plan, SLOTS)                                  # 10:00–20:00 leaves 20:00–21:00 of the evening slot
    assert cut == 240 - 60 and plan.minutes == 60 and sum(b.minutes for b in plan.blocks) == 60
    assert all(b.minutes >= 5 and b.minutes % 5 == 0 for b in plan.blocks)
    assert any("budget cut by 180 min" in r for r in plan.radar)
    for b in plan.blocks:
        assert b.start >= "20:00", (b.id, b.start)
    assert shrink_to_free(build_plan(root, parent, "2026-09-07"), SLOTS) == 0    # nothing busy: nothing cut


def test_arranged_flag_round_trips(roots):
    root, parent = roots
    plan = build_plan(root, parent, "2026-09-07")
    plan.arranged = True
    md = render_md(plan)
    assert md.startswith("# Study plan — 2026-09-07 (Mon) · 240 min · arranged by hand")
    assert to_dict(parse_md(md)) == to_dict(plan) and to_dict(plan)["arranged"] is True
    save_plan(root, plan)
    assert load_plan(root, "2026-09-07").arranged is True


# ---------------------------------------------------------------- the CLI and the board

def test_plan_fetches_the_feed_when_configured(roots, capsys, monkeypatch):
    root, parent = roots
    with_slots(root)
    monkeypatch.setenv(busy.ENV_URLS, str(ICS))
    assert main(["--date", "2026-09-07", "plan", "--no-news"]) == 0
    capsys.readouterr()
    plan = load_plan(root, "2026-09-07")
    assert {e.title for e in plan.events} >= {"Call with a recruiter", "Team sync", "Labor Day"}
    assert all(e.source == "ical" for e in plan.events)
    assert (root / "data" / busy.CACHE).exists()

    # a day with a hackathon: the plan shrinks to the free evening and says so
    assert main(["--date", "2026-09-12", "plan", "--no-news"]) == 0
    out = capsys.readouterr().out
    assert "Agents hackathon" in out and "budget cut by" in out
    assert load_plan(root, "2026-09-12").minutes < 240

    # set-block by hand marks the day arranged; refresh then leaves the layout alone
    assert main(["--date", "2026-09-07", "plan", "set-block", "news", "--start", "13:35"]) == 0
    assert load_plan(root, "2026-09-07").arranged is True
    assert main(["--date", "2026-09-07", "plan", "refresh"]) == 0
    out = capsys.readouterr().out
    assert "outside event(s) on the plan for 2026-09-07" in out and "nothing changed" in out
    assert load_plan(root, "2026-09-07").block("news").start == "13:35"
    assert main(["--date", "2026-09-09", "plan", "refresh"]) == 1     # no plan that day


def test_api_busy_refreshes_and_reports_changes(roots, monkeypatch):
    root, parent = roots
    with_slots(root)
    monkeypatch.delenv(busy.ENV_URLS, raising=False)
    save_plan(root, build_plan(root, parent, "2026-09-07"))
    payload = api_busy(root, "2026-09-07")
    assert payload["changed"] is False and payload["busy_warnings"] == []

    monkeypatch.setenv(busy.ENV_URLS, str(ICS))
    server, base = serve_in_thread(root, "2026-09-07")
    try:
        with urllib.request.urlopen(base + "/api/busy?date=2026-09-07", timeout=5) as r:
            data = json.loads(r.read())
        assert data["changed"] is True and {e["title"] for e in data["events"]} >= {"Team sync"}
        assert load_plan(root, "2026-09-07").events                    # persisted
        with urllib.request.urlopen(base + "/api/busy?date=2026-09-07", timeout=5) as r:
            assert json.loads(r.read())["changed"] is False
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            html = r.read().decode()
        assert "refreshBusy" in html and "5 * 60 * 1000" in html
    finally:
        server.shutdown()
        server.server_close()
