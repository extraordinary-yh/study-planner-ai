"""The day board: the structured plan, slot placement, the loopback-only server's API, the two report routes."""
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from studyplan import log as logmod
from studyplan.board import make_server, plan_payload, render_html, report_for, reports_payload, serve_in_thread
from studyplan.cli import main
from studyplan.plan import (Block, build_plan, day_slots, from_dict, load_plan, parse_md, place_blocks, plan_paths,
                            render_md, save_plan, to_dict, to_minutes)


# ---------------------------------------------------------------- the structured plan

def test_json_markdown_round_trip(roots):
    root, parent = roots
    plan = build_plan(root, parent, "2026-09-07")
    plan.call = "Kill the binary-search freeze first: it is the one concrete failure you named."
    plan.blocks[0].first_action = "open https://example.com/news and read the first screen."
    plan.blocks[0].items[0].done = True
    plan.blocks[3].done = True
    md = render_md(plan)
    assert "**Today's call:** Kill the binary-search freeze" in md
    assert "- [x] " in md and "<!--id:leetcode-->" in md and " · done <!--id:leetcode-->" in md
    assert to_dict(parse_md(md)) == to_dict(plan)                    # markdown is a faithful rendering of the JSON
    assert to_dict(from_dict(to_dict(plan))) == to_dict(plan)        # and the JSON round-trips through itself


def test_write_plan_writes_both_files_and_markdown_follows_the_json(roots):
    root, parent = roots
    plan = build_plan(root, parent, "2026-09-07")
    js, md = plan_paths(root, "2026-09-07")
    save_plan(root, plan)
    assert json.loads(js.read_text())["blocks"][0]["id"] == "news"
    assert md.read_text() == render_md(plan)
    loaded = load_plan(root, "2026-09-07")                           # the JSON wins when both exist
    assert to_dict(loaded) == to_dict(plan)
    md.unlink()
    assert to_dict(load_plan(root, "2026-09-07")) == to_dict(plan)
    js.unlink()
    md.write_text(render_md(plan))                                   # markdown-only days (written before the board)
    assert to_dict(load_plan(root, "2026-09-07")) == to_dict(plan)


def test_slots_come_from_best_time_and_blocks_never_overlap(roots):
    root, parent = roots
    assert day_slots({"constraints": {"best_time": "afternoon 3 h + evening 2 h; mornings are for the gym"}}) == [
        {"start": "13:00", "minutes": 180}, {"start": "19:00", "minutes": 120}]
    assert day_slots({"constraints": {"best_time": "afternoon 3 h", "day_start": "14:30"}})[0]["start"] == "14:30"
    assert day_slots({}) == [{"start": "09:00", "minutes": 1440}]    # nothing on file: one open day

    level = yaml.safe_load((root / "profile" / "level.yaml").read_text())
    level["constraints"]["slots"] = [{"start": "13:00", "minutes": 180}, {"start": "19:00", "minutes": 120}]
    (root / "profile" / "level.yaml").write_text(yaml.safe_dump(level))
    plan = build_plan(root, parent, "2026-09-07")
    assert plan.blocks[0].start == "13:00"                           # first block at the first slot's start
    for a, b in zip(plan.blocks, plan.blocks[1:]):
        assert to_minutes(a.start) + a.minutes <= to_minutes(b.start), (a.id, b.id)
    assert any(to_minutes(b.start) >= to_minutes("19:00") for b in plan.blocks)   # spills into the evening slot

    # a block that does not fit the remaining slot moves to the next one rather than overrunning
    blocks = [Block("a", "A", 100), Block("b", "B", 100), Block("c", "C", 30)]
    place_blocks(blocks, [{"start": "13:00", "minutes": 120}, {"start": "19:00", "minutes": 120}])
    assert [b.start for b in blocks] == ["13:00", "19:00", "20:40"]


def test_gcal_link_per_block(roots):
    root, parent = roots
    save_plan(root, build_plan(root, parent, "2026-09-07"))
    payload = plan_payload(root, "2026-09-07")
    design = next(b for b in payload["blocks"] if b["id"] == "design")
    assert design["gcal"].startswith("https://calendar.google.com/calendar/render?action=TEMPLATE")
    assert f"dates=20260907T{design['start'].replace(':', '')}00%2F20260907T{design['end'].replace(':', '')}00" in design["gcal"]


# ---------------------------------------------------------------- the server

@pytest.fixture
def board(roots):
    root, parent = roots
    save_plan(root, build_plan(root, parent, "2026-09-07"))
    server, base = serve_in_thread(root, "2026-09-07")
    yield root, base
    server.shutdown()
    server.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read())


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_refuses_non_loopback_binds(roots):
    root, _ = roots
    for host in ("0.0.0.0", "192.168.1.10", "::", "example.com"):
        with pytest.raises(ValueError, match="loopback-only"):
            make_server(root, "2026-09-07", 0, host)


def test_html_contains_every_block_and_its_calendar_link(board):
    root, base = board
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        html = r.read().decode()
    payload = plan_payload(root, "2026-09-07")
    for b in payload["blocks"]:
        assert f'"id": "{b["id"]}"'.replace(" ", "") in html.replace(" ", ""), b["id"]
        assert b["gcal"].replace("&", "&") in html, b["id"]
    assert "calendar.google.com/calendar/render" in html and "prefers-color-scheme: dark" in html
    assert "TimelineLayout" in html and "root.TimelineLayout = TimelineLayout" in html      # the layout module, inlined
    assert 'src="http' not in html and 'href="https://cdn' not in html                        # one file, no CDN
    assert '"jobs": {"date": "2026-09-05", "url": "/reports/jobs/2026-09-05.html"}' in html  # newest report on or before the day
    assert '"events": null' in html                                                          # the fixture parent has no event report
    for placeholder in ("__PLAN__", "__HUB__", "__REPORTS__", "__TIMELINE_JS__", "__NAV__", "__PANELS__", "__SCRIPTS__"):
        assert placeholder not in render_html(payload), placeholder
    try:
        urllib.request.urlopen(base + "/?date=1999-01-01", timeout=5)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404 and "No plan for 1999-01-01" in e.read().decode()          # a page, not JSON, for the browser


# ---------------------------------------------------------------- the two daily reports on the page

def test_report_lookup_exact_then_newest_on_or_before_then_none(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    for name in ("2026-09-01.html", "2026-09-04.html", "2026-09-07.html", "index.html", "notes.html"):
        (folder / name).write_text("<p>r</p>")
    assert report_for(folder, "2026-09-07") == "2026-09-07"                     # the day's own report
    assert report_for(folder, "2026-09-06") == "2026-09-04"                     # the newest one before it
    assert report_for(folder, "2026-09-30") == "2026-09-07"
    assert report_for(folder, "2026-08-31") is None                             # nothing on or before
    assert report_for(tmp_path / "missing", "2026-09-07") is None
    (folder / "2026-09-07.html").unlink()
    assert report_for(folder, "2026-09-07") == "2026-09-04"


def test_reports_payload_for_the_server_and_for_the_hub(roots):
    root, parent = roots
    (parent / "sf-events" / "reports").mkdir(parents=True)
    (parent / "sf-events" / "reports" / "2026-09-06.html").write_text("<p>events</p>")
    live = reports_payload(parent, "2026-09-07")
    assert live["jobs"] == {"date": "2026-09-05", "url": "/reports/jobs/2026-09-05.html"}
    assert live["events"] == {"date": "2026-09-06", "url": "/reports/events/2026-09-06.html"}
    assert live["folders"]["events"].endswith("/tree/main/sf-events/reports")
    hub = reports_payload(parent, "2026-09-07", {"jobs": "../../archive/{date}.html", "events": "../../events/{date}.html"})
    assert hub["jobs"]["url"] == "../../archive/2026-09-05.html" and hub["events"]["url"] == "../../events/2026-09-06.html"
    assert reports_payload(parent, "2026-01-01") == {"jobs": None, "events": None, "folders": live["folders"]}


def test_report_routes_serve_only_the_two_dated_folders(board):
    root, base = board
    parent = Path(root).parent / "job-radar"
    (parent / "sf-events" / "reports").mkdir(parents=True)
    (parent / "sf-events" / "reports" / "2026-09-06.html").write_text("<!doctype html><p id='ev'>event report</p>")
    (parent / "reports" / "secret.html").write_text("not a dated report")
    (parent / "data" / "seen.json").write_text("{}")

    def fetch(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode()

    status, ctype, body = fetch("/reports/jobs/2026-09-05.html")
    assert status == 200 and ctype.startswith("text/html") and "fixture report" in body
    status, ctype, body = fetch("/reports/events/2026-09-06.html")
    assert status == 200 and "event report" in body
    for path in ("/reports/jobs/2026-09-06.html",                       # no such day: the page does the fallback, not the route
                 "/reports/jobs/secret.html", "/reports/jobs/index.html", "/reports/index.html", "/reports/",
                 "/reports/jobs/", "/reports/jobs/../secret.html", "/reports/jobs/../../data/seen.json",
                 "/reports/events/../../reports/2026-09-05.html", "/reports/data/seen.json", "/reports/jobs/2026-09-05.html/",
                 "/reports/JOBS/2026-09-05.html", "/reports/jobs/2026-09-05.HTML", "/reports/jobs/2026-9-5.html"):
        status, ctype, body = fetch(path)
        assert status == 404 and ctype.startswith("application/json"), path


def test_repo_file_routes_render_markdown_and_reject_unsafe_paths(board):
    root, base = board
    (root / "profile" / "notes.md").write_text("# Coach notes\n\nA concrete repair.")

    def fetch(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode()

    for path in ("/files/study-plan/profile/notes.md", "/profile/notes.md"):
        status, ctype, body = fetch(path)
        assert status == 200 and ctype.startswith("text/html") and "Coach notes" in body
    for path in ("/files/../.env", "/files/.env", "/files/data/seen.json"):
        status, ctype, _ = fetch(path)
        assert status == 404 and ctype.startswith("application/json"), path


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_timeline_layout_rules_under_node():
    here = Path(__file__).resolve().parent
    out = subprocess.run([NODE, str(here / "timeline.test.mjs")], capture_output=True, text=True, cwd=str(here.parent))
    assert out.returncode == 0, out.stderr
    assert "timeline: ok" in out.stdout


def test_api_done_appends_a_log_row_and_the_markdown_shows_the_tick(board):
    root, base = board
    status, before = get(base + "/api/plan?date=2026-09-07")
    assert status == 200 and before["blocks"][0]["items"][0]["done"] is False
    item_id = before["blocks"][0]["items"][0]["id"]

    status, after = post(base, "/api/done", {"date": "2026-09-07", "block": "news", "item": item_id})
    assert status == 200 and after["blocks"][0]["items"][0]["done"] is True
    rows = logmod.load(root / "data" / "log.jsonl")
    assert len(rows) == 1 and rows[0]["block"] == "news" and rows[0]["target"] == item_id and rows[0]["date"] == "2026-09-07"
    assert rows[0]["ts"] and rows[0]["source"] == "board"
    js, md = plan_paths(root, "2026-09-07")
    assert "- [x] " in md.read_text()                                # the server re-renders; no `plan --force` needed
    assert json.loads(js.read_text())["blocks"][0]["items"][0]["done"] is True

    status, after = post(base, "/api/done", {"date": "2026-09-07", "block": "leetcode", "note": "two DP problems"})
    assert status == 200
    row = logmod.load(root / "data" / "log.jsonl")[-1]
    assert row["block"] == "leetcode" and row["minutes"] == next(b["minutes"] for b in after["blocks"] if b["id"] == "leetcode")
    assert row["note"] == "two DP problems"
    assert " · done <!--id:leetcode-->" in md.read_text()
    status, log = get(base + "/api/log?date=2026-09-07")
    assert status == 200 and len(log["rows"]) == 2


def test_api_undo_reverses_the_entry(board):
    root, base = board
    _, plan = get(base + "/api/plan?date=2026-09-07")
    item_id = plan["blocks"][0]["items"][0]["id"]
    post(base, "/api/done", {"date": "2026-09-07", "block": "news", "item": item_id})
    post(base, "/api/done", {"date": "2026-09-07", "block": "leetcode", "minutes": 45})
    status, after = post(base, "/api/undo", {"date": "2026-09-07", "block": "leetcode"})
    assert status == 200 and next(b for b in after["blocks"] if b["id"] == "leetcode")["done"] is False

    rows = logmod.load(root / "data" / "log.jsonl")
    assert len(rows) == 3 and rows[-1] == {**rows[-1], "undo": True, "target": "leetcode"}   # append-only: nothing rewritten
    netted = logmod.net(rows)
    assert [e["target"] for e in netted] == [item_id]                # the done row and its undo row cancel out
    assert logmod.week_summary(rows, "2026-09-07")["minutes"]["leetcode"] == 0
    assert " · done <!--id:leetcode-->" not in plan_paths(root, "2026-09-07")[1].read_text()


def test_api_block_persists_and_snaps_to_five_minutes(board):
    root, base = board
    status, after = post(base, "/api/block", {"date": "2026-09-07", "block": "design", "start": "14:07", "minutes": 63})
    assert status == 200
    design = next(b for b in after["blocks"] if b["id"] == "design")
    assert design["start"] == "14:05" and design["minutes"] == 65 and design["end"] == "15:10"
    js, md = plan_paths(root, "2026-09-07")
    assert json.loads(js.read_text())["blocks"][2]["start"] == "14:05"
    assert "14:05–15:10 · 65 min" in md.read_text()
    assert load_plan(root, "2026-09-07").block("design").start == "14:05"     # survives a reload
    assert "dates=20260907T140500%2F20260907T151000" in design["gcal"]        # the calendar link follows

    assert post(base, "/api/block", {"date": "2026-09-07", "block": "design", "minutes": 1})[1]["blocks"][2]["minutes"] == 5
    assert post(base, "/api/block", {"date": "2026-09-07", "block": "nope"})[0] == 400
    assert post(base, "/api/block", {"date": "2026-09-07", "block": "design", "start": "nope"})[0] == 400
    assert post(base, "/api/done", {"date": "2026-09-07", "block": "news", "item": "news-99"})[0] == 400
    try:
        get(base + "/api/plan?date=1999-01-01")
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400 and "no plan" in json.loads(e.read())["error"]


# ---------------------------------------------------------------- the CLI helpers

def test_plan_editing_cli(roots, capsys):
    root, parent = roots
    save_plan(root, build_plan(root, parent, "2026-09-07"))
    assert main(["--date", "2026-09-07", "plan", "set-call", "Binary search first."]) == 0
    assert main(["--date", "2026-09-07", "plan", "add-item", "leetcode", "review Koko Eating Bananas, 25 min cap"]) == 0
    assert "added leetcode-" in capsys.readouterr().out
    assert main(["--date", "2026-09-07", "plan", "set-block", "design", "--start", "13:33", "--minutes", "88"]) == 0
    assert main(["--date", "2026-09-07", "plan", "set-block", "chores", "--title", "Chores box", "--minutes", "20",
                 "--start", "21:00"]) == 0
    md = plan_paths(root, "2026-09-07")[1].read_text()
    assert "**Today's call:** Binary search first." in md
    assert "review Koko Eating Bananas, 25 min cap" in md
    assert "13:35–15:05 · 90 min" in md                              # both snapped to five minutes
    assert "## 6. Chores box · 21:00–21:20 · 20 min <!--id:chores-->" in md
    assert main(["--date", "2026-09-07", "plan", "add-item", "nope", "x"]) == 1
    assert main(["--date", "2026-09-09", "plan", "set-call", "x"]) == 1
    assert "no plan for 2026-09-09" in capsys.readouterr().out
    assert main(["--date", "2026-09-09", "serve"]) == 1              # serve refuses a day with no plan
