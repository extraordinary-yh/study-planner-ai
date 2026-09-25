"""The secret iCal address as a calendar source: parsing, recurrence, and interchangeability with the MCP path."""
from pathlib import Path

import pytest

from studyplan.gcal import event_id, parse_mcp_events
from studyplan.ical import normalize_uid, occurrences, parse_components, parse_ical, parse_stamp, unfold
from studyplan.plan import Plan, to_dict

FIX = Path(__file__).resolve().parent / "fixtures"
ICS = (FIX / "ical" / "calendar.ics").read_text()


def by_title(events):
    out = {}
    for e in events:
        out.setdefault(e.title, e)
    return out


def test_unfold_and_components():
    header, vevents = parse_components(ICS)
    assert header["X-WR-CALNAME"] == "alex@example.com"
    assert len(vevents) == 14                                          # the VTIMEZONE and VALARM blocks are not events
    call = next(v for v in vevents if v["UID"][0][0] == "_synthetic_calendar_event@google.com")
    assert call["DTSTART"][0][1] == {"TZID": "America/Los_Angeles"}
    assert "ACTION" not in call                                        # nested VALARM properties stay out
    declined = next(v for v in vevents if v["UID"][0][0] == "declined-1@google.com")
    assert declined["ATTENDEE"][0][1]["CN"] == "alex@example.com"       # a folded line was joined
    assert unfold("A:b\n c\nD:e") == ["A:bc", "D:e"]


def test_stamps_in_every_form():
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")
    assert parse_stamp("20260907", {"VALUE": "DATE"}, la) == (__import__("datetime").date(2026, 9, 7), True)
    utc, _ = parse_stamp("20260907T203000Z", {}, la)
    assert utc.utcoffset().total_seconds() == 0 and utc.astimezone(la).hour == 13
    ny, _ = parse_stamp("20260907T010000", {"TZID": "America/New_York"}, la)
    assert ny.astimezone(la).strftime("%H:%M") == "22:00" and ny.astimezone(la).day == 6
    floating, _ = parse_stamp("20260907T150000", {}, la)
    assert floating.tzinfo is la and floating.hour == 15               # floating: the user's own zone


def test_parse_ical_keeps_what_the_board_needs():
    events, warnings = parse_ical(ICS, "2026-09-07", self_email="alex@example.com")
    titles = by_title(events)
    assert set(titles) == {"Call with a recruiter", "Team sync", "Labor Day", "Red-eye flight", "Gym", "Weekly 1:1 (moved)",
                           "Rent reminder"}, set(titles)
    assert [w for w in warnings if "No start at all" in w]
    assert [w for w in warnings if "Rent reminder" in w and "monthly" in w]      # the limit is stated, not silent

    call = titles["Call with a recruiter"]
    assert (call.start, call.minutes, call.end, call.busy, call.all_day) == ("09:00", 25, "09:25", True, False)
    assert call.location == "Microsoft Teams Meeting" and call.calendar == "alex@example.com" and call.source == "ical"
    assert call.gcal_id == "_synthetic_calendar_event" and call.id == event_id("_synthetic_calendar_event")

    assert (titles["Team sync"].start, titles["Team sync"].minutes) == ("13:30", 30)      # UTC -> Pacific
    assert (titles["Labor Day"].all_day, titles["Labor Day"].busy) == (True, False)
    assert (titles["Red-eye flight"].start, titles["Red-eye flight"].minutes) == ("00:00", 390)   # NY time, clipped
    assert events[0].all_day and events[1].start <= events[2].start


def test_declined_needs_the_self_address():
    assert "A meeting Alex declined" in by_title(parse_ical(ICS, "2026-09-07")[0])
    assert "A meeting Alex declined" not in by_title(parse_ical(ICS, "2026-09-07", self_email="ALEX@example.com")[0])


def test_daily_rule_expands_with_count_and_exdate():
    gym = by_title(parse_ical(ICS, "2026-09-07")[0])["Gym"]
    assert (gym.start, gym.minutes, gym.busy) == ("07:30", 30, False)  # TRANSP:TRANSPARENT -> shown, not busy
    assert gym.gcal_id == "daily-gym_20260907T143000Z"               # Google's own instance-id convention
    assert "Gym" not in by_title(parse_ical(ICS, "2026-09-06")[0])     # EXDATE
    assert "Gym" in by_title(parse_ical(ICS, "2026-09-10")[0])         # the 10th and last occurrence
    assert "Gym" not in by_title(parse_ical(ICS, "2026-09-11")[0])     # COUNT=10 exhausted


def test_weekly_rule_byday_until_and_recurrence_id_override():
    mon = by_title(parse_ical(ICS, "2026-09-07")[0])                   # a Monday
    assert "Weekly 1:1" not in mon and mon["Weekly 1:1 (moved)"].start == "18:00"   # RECURRENCE-ID replaced it
    assert mon["Weekly 1:1 (moved)"].gcal_id == "weekly-1on1_20260908T000000Z"    # keyed by its original instant
    wed = by_title(parse_ical(ICS, "2026-09-09")[0])
    assert (wed["Weekly 1:1"].start, wed["Weekly 1:1"].minutes) == ("17:00", 30)
    assert "Weekly 1:1" not in by_title(parse_ical(ICS, "2026-09-08")[0])       # Tuesday: not in BYDAY
    assert "Weekly 1:1" not in by_title(parse_ical(ICS, "2027-01-04")[0])       # past UNTIL
    # INTERVAL=2 with COUNT=3 from Tue 08-25: 08-25, 09-08, 09-22
    assert "Fortnightly on a Tuesday" in by_title(parse_ical(ICS, "2026-09-08")[0])
    assert "Fortnightly on a Tuesday" not in by_title(parse_ical(ICS, "2026-09-01")[0])
    assert "Fortnightly on a Tuesday" not in by_title(parse_ical(ICS, "2026-10-06")[0])


def test_other_rules_show_the_master_only():
    assert "Monthly from another day" not in by_title(parse_ical(ICS, "2026-09-20")[0])   # not expanded
    assert "Monthly from another day" in by_title(parse_ical(ICS, "2026-08-20")[0])        # its first occurrence


def test_occurrences_bounded_and_duration_form():
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")
    fields = {"DTSTART": [("20260101T090000", {"TZID": "America/Los_Angeles"})], "DURATION": [("PT45M", {})],
              "RRULE": [("FREQ=DAILY", {})], "SUMMARY": [("Standup", {})]}
    occ = occurrences(fields, __import__("datetime").date(2026, 9, 7), la)
    assert len(occ) == 2 and (occ[-1][1] - occ[-1][0]).total_seconds() == 45 * 60     # yesterday (overnight check) + today


def test_ical_and_mcp_paths_are_interchangeable():
    """The same Google event reaches the board with the same id, time and busy state from either source."""
    ical_events, _ = parse_ical(ICS, "2026-09-07")
    mcp_events, _ = parse_mcp_events((FIX / "gcal_day.json").read_text(), "2026-09-07")
    a, b = by_title(ical_events), by_title(mcp_events)
    for title in ("Call with a recruiter", "Team sync", "Labor Day", "Red-eye flight"):
        x, y = a[title], b[title]
        assert (x.id, x.gcal_id, x.start, x.minutes, x.busy, x.all_day) == (y.id, y.gcal_id, y.start, y.minutes, y.busy, y.all_day), title
    assert normalize_uid("abc@google.com") == "abc" and normalize_uid("keep@example.org") == "keep@example.org"
    # merging both into one plan by key leaves one entry per event, the later source winning in place
    plan = Plan("2026-09-07", 240, [], [], events=[e for e in mcp_events])
    by_key = {e.gcal_id or e.id: e for e in plan.events}
    for e in ical_events:
        by_key[e.gcal_id or e.id] = e
    merged = sorted(by_key.values(), key=lambda e: (not e.all_day, e.start, e.title))
    assert len(merged) == len(ical_events) and all(e.source == "ical" for e in merged if e.title in b)
    assert to_dict(Plan("2026-09-07", 240, [], [], events=merged))["events"]


def test_not_a_feed():
    with pytest.raises(ValueError):
        parse_ical("hello", "2026-09-07")
