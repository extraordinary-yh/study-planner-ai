"""Google Calendar events on the plan: parsing the MCP payload, laying study around meetings, the board lane."""
import json
import urllib.request
from pathlib import Path

import pytest

from studyplan.board import plan_payload, serve_in_thread
from studyplan.cli import main
from studyplan.gcal import event_id, parse_mcp_events
from studyplan.plan import (Block, Event, build_plan, day_slots, load_plan, parse_md, place_blocks, plan_paths,
                            render_md, save_plan, to_dict, to_minutes)

FIX = Path(__file__).resolve().parent / "fixtures"
PAYLOAD = (FIX / "gcal_day.json").read_text()


# ---------------------------------------------------------------- parsing what the MCP returns

def test_parse_mcp_payload():
    events, warnings = parse_mcp_events(PAYLOAD, "2026-09-07")
    by_title = {e.title: e for e in events}
    assert set(by_title) == {"Labor Day", "Red-eye flight", "Call with a recruiter", "Team sync"}, by_title.keys()
    assert [w for w in warnings if "No start at all" in w]            # unparseable events warn, never crash

    call = by_title["Call with a recruiter"]
    assert (call.start, call.minutes, call.end) == ("09:00", 25, "09:25")
    assert call.busy and not call.all_day and call.source == "gcal"
    assert call.location == "Microsoft Teams Meeting" and call.url.endswith("eid=abc123")
    assert call.calendar == "alex@example.com"                        # the payload's own calendar name
    assert call.id == event_id("_synthetic_calendar_event") and call.id.startswith("ev-")

    assert (by_title["Labor Day"].all_day, by_title["Labor Day"].busy) == (True, False)   # all-day never blocks the day
    flight = by_title["Red-eye flight"]
    assert (flight.start, flight.minutes) == ("00:00", 390)           # overnight, clipped to this day
    assert events[0].all_day and events[1].start <= events[2].start   # all-day first, then by start

    assert parse_mcp_events(json.loads(PAYLOAD)["events"], "2026-09-07")[0]                # a bare list works too
    assert parse_mcp_events('{"events": []}', "2026-09-07") == ([], [])
    with pytest.raises(ValueError):
        parse_mcp_events("12", "2026-09-07")


def test_ids_are_stable_so_a_reimport_updates_in_place():
    a, _ = parse_mcp_events(PAYLOAD, "2026-09-07")
    b, _ = parse_mcp_events(PAYLOAD.replace("09:25:00", "09:45:00"), "2026-09-07")
    call_a = next(e for e in a if e.title == "Call with a recruiter")
    call_b = next(e for e in b if e.title == "Call with a recruiter")
    assert call_a.id == call_b.id and call_a.gcal_id == call_b.gcal_id and call_b.minutes == 45


# ---------------------------------------------------------------- placement around meetings

def test_blocks_are_laid_around_busy_events():
    blocks = [Block("a", "A", 60), Block("b", "B", 60)]
    place_blocks(blocks, [{"start": "13:00", "minutes": 240}], [(13 * 60 + 30, 14 * 60)])
    assert [b.start for b in blocks] == ["14:00", "15:00"]            # pushed past the 13:30–14:00 meeting

    blocks = [Block("a", "A", 25), Block("b", "B", 60)]
    place_blocks(blocks, [{"start": "13:00", "minutes": 240}], [(13 * 60 + 30, 14 * 60)])
    assert [b.start for b in blocks] == ["13:00", "14:00"]            # a short block still fits before it

    blocks = [Block("a", "A", 90)]                                     # the meeting eats the slot: next slot
    place_blocks(blocks, [{"start": "13:00", "minutes": 120}, {"start": "19:00", "minutes": 120}],
                 [(13 * 60, 14 * 60 + 30)])
    assert blocks[0].start == "19:00"


def test_plan_places_study_around_the_day_s_calendar(roots):
    root, parent = roots
    events, _ = parse_mcp_events(PAYLOAD, "2026-09-07")
    plan = build_plan(root, parent, "2026-09-07", events=events)
    assert plan.busy == [(0, 390), (540, 565), (810, 840)]             # merged, all-day excluded
    for b in plan.blocks:
        for lo, hi in plan.busy:
            assert to_minutes(b.start) >= hi or to_minutes(b.start) + b.minutes <= lo, (b.id, b.start, lo, hi)
    for a, b in zip(plan.blocks, plan.blocks[1:]):                     # and still never on top of each other
        assert to_minutes(a.start) + a.minutes <= to_minutes(b.start)
    assert any("Calendar: 4 event(s) today" in r for r in plan.radar)


def test_events_round_trip_through_the_markdown(roots):
    root, parent = roots
    events, _ = parse_mcp_events(PAYLOAD, "2026-09-07")
    events.append(Event("ev-manual", "Gym", "07:00", 60, "manual", busy=False))
    plan = build_plan(root, parent, "2026-09-07", events=events)
    md = render_md(plan)
    assert "**Calendar** (from Google Calendar; the blocks below are laid around these)" in md
    assert "- 09:00–09:25 · [Call with a recruiter](https://www.google.com/calendar/event?eid=abc123) · " \
           "Microsoft Teams Meeting · alex@example.com <!--ev:" in md
    assert "- all day · Labor Day · alex@example.com <!--ev:" in md
    assert "- 07:00–08:00 · Gym · manual · free <!--ev:ev-manual|-->" in md
    assert to_dict(parse_md(md)) == to_dict(plan)
    save_plan(root, plan)
    assert to_dict(load_plan(root, "2026-09-07")) == to_dict(plan)


# ---------------------------------------------------------------- the CLI

def test_import_events_cli(roots, capsys, monkeypatch):
    root, parent = roots
    save_plan(root, build_plan(root, parent, "2026-09-07"))
    before = load_plan(root, "2026-09-07").block("news").start

    assert main(["--date", "2026-09-07", "plan", "import-events", str(FIX / "gcal_day.json")]) == 0
    out = capsys.readouterr().out
    assert "4 event(s) from the calendar; 4 on the plan for 2026-09-07" in out
    assert "09:00–09:25 Call with a recruiter · alex@example.com" in out
    assert "WARNING: skipped 'No start at all'" in out
    assert "blocks re-laid around them:" in out

    plan = load_plan(root, "2026-09-07")
    assert len(plan.events) == 4 and plan.block("news").start != before      # moved off the meetings
    md = plan_paths(root, "2026-09-07")[1].read_text()
    assert "**Calendar**" in md and "Call with a recruiter" in md

    # re-importing the same day replaces rather than duplicates, and stdin works
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(PAYLOAD))
    assert main(["--date", "2026-09-07", "plan", "import-events", "-", "--keep-times"]) == 0
    capsys.readouterr()
    assert len(load_plan(root, "2026-09-07").events) == 4

    # a hand-added commitment survives a re-import; `plan --force` keeps the calendar
    assert main(["--date", "2026-09-07", "plan", "add-event", "Gym", "--start", "07:03", "--minutes", "62"]) == 0
    assert "07:05–08:05 Gym" in capsys.readouterr().out                       # start and minutes both snapped
    assert main(["--date", "2026-09-07", "plan", "import-events", str(FIX / "gcal_day.json")]) == 0
    capsys.readouterr()
    assert len(load_plan(root, "2026-09-07").events) == 5
    assert main(["--date", "2026-09-07", "plan", "--no-news", "--force"]) == 0
    capsys.readouterr()
    assert len(load_plan(root, "2026-09-07").events) == 5

    assert main(["--date", "2026-09-07", "plan", "add-event", "x", "--start", "nope", "--minutes", "10"]) == 1
    assert main(["--date", "2026-09-09", "plan", "import-events", str(FIX / "gcal_day.json")]) == 1
    capsys.readouterr()


def test_promote_event_puts_it_on_the_task_list(roots, capsys):
    root, parent = roots
    events, _ = parse_mcp_events(PAYLOAD, "2026-09-07")
    save_plan(root, build_plan(root, parent, "2026-09-07", events=events))
    call = next(e for e in events if e.title == "Call with a recruiter")

    assert main(["--date", "2026-09-07", "plan", "promote-event", call.id, "--id", "recruiter",
                 "--first-action", "open the Teams link two minutes early"]) == 0
    assert "recruiter: Call with a recruiter · 09:00–09:25 · 25 min" in capsys.readouterr().out

    plan = load_plan(root, "2026-09-07")
    block = plan.block("recruiter")
    assert block.start == "09:00" and block.minutes == 25 and block.first_action.startswith("open the Teams link")
    assert block.links == [{"label": "calendar event", "url": call.url}]
    assert "Where: Microsoft Teams Meeting" in block.description
    assert plan.event(call.id).busy is False                            # the block owns that time now, not the event
    assert main(["--date", "2026-09-07", "plan", "add-item", "recruiter", "ask for their timeline"]) == 0
    assert "- [ ] ask for their timeline" in plan_paths(root, "2026-09-07")[1].read_text()

    assert main(["--date", "2026-09-07", "plan", "promote-event", call.id, "--id", "recruiter"]) == 1
    assert main(["--date", "2026-09-07", "plan", "promote-event", "nope"]) == 1
    capsys.readouterr()


# ---------------------------------------------------------------- the board

def test_board_shows_the_calendar_lane(roots):
    root, parent = roots
    events, _ = parse_mcp_events(PAYLOAD, "2026-09-07")
    save_plan(root, build_plan(root, parent, "2026-09-07", events=events))
    server, base = serve_in_thread(root, "2026-09-07")
    try:
        payload = plan_payload(root, "2026-09-07")
        assert [e["title"] for e in payload["events"]][0] == "Labor Day"
        assert all("end" in e for e in payload["events"])
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            html = r.read().decode()
        for e in payload["events"]:
            assert e["title"] in html, e["title"]
        assert "eventNode" in html and "class=\"allday\"" in html
    finally:
        server.shutdown()
        server.server_close()
