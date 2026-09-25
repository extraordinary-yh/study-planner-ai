"""The debrief loop: the day after a `went` event the plan opens with a Debrief block; the hub's day page and
form carry it; the Job list panel links the report and the application folder."""
import json
import shutil
from pathlib import Path

from studyplan import log as logmod
from studyplan.cli import main
from studyplan.hub import build_site, debrief_form_html
from studyplan.plan import DEBRIEF_QUESTIONS, build_plan, load_plan, parse_done_text, parse_md, render_md, save_plan, to_dict
from studyplan.sfdata import marked, snapshots, verdicts, went_yesterday

FIX = Path(__file__).resolve().parent / "fixtures"


def test_sfdata_reads_the_sub_project(roots):
    root, parent = roots
    assert "luma_sf:meetup-0" in snapshots(parent) and verdicts(parent)["luma_sf:meetup-0"]["verdict"] == "went"
    assert [e["title"] for e in marked(parent)] == ["Agents meetup", "Founders dinner", "Agents hackathon"]   # by date, then start
    assert [e["verdict"] for e in marked(parent, "2026-09-05")] == ["went"]
    assert [e["title"] for e in went_yesterday(parent, "2026-09-06")] == ["Agents meetup"]
    assert went_yesterday(parent, "2026-09-07") == [] and went_yesterday(parent, "2026-09-13") == []   # going != went


def test_plan_opens_with_a_debrief_the_day_after_a_went_event(roots):
    root, parent = roots
    plan = build_plan(root, parent, "2026-09-06")                          # the meetup was on the 5th
    first = plan.blocks[0]
    assert first.id == "debrief" and first.title == "Debrief · Agents meetup" and first.minutes == 15
    assert [i.text for i in first.items] == list(DEBRIEF_QUESTIONS)
    assert all(i.refs == ["https://lu.ma/agents-meetup"] for i in first.items)
    assert first.links == [{"label": "the event", "url": "https://lu.ma/agents-meetup"}]
    assert first.first_action.startswith("open the Debrief panel")
    assert any(r.startswith("Debrief due: Agents meetup") for r in plan.radar)
    assert [b.id for b in plan.blocks[1:]] == ["news", "stack", "design", "leetcode", "stories"]
    assert sum(b.minutes for b in plan.blocks) == plan.minutes == 240      # carved out of the budget, not added to it
    assert plan.blocks[0].start and plan.blocks[0].start <= plan.blocks[1].start   # laid first

    md = render_md(plan)
    assert "## 1. Debrief · Agents meetup" in md and "- [ ] What did you do?" in md
    assert to_dict(parse_md(md)) == to_dict(plan)
    assert [b.id for b in build_plan(root, parent, "2026-09-07").blocks] == ["news", "stack", "design", "leetcode", "stories"]


def test_debrief_block_is_logged_like_any_other(roots, capsys):
    root, parent = roots
    save_plan(root, build_plan(root, parent, "2026-09-06"))
    assert main(["--date", "2026-09-06", "done", "debrief", "--minutes", "15", "--note", "met two founders"]) == 0
    assert logmod.load(root / "data" / "log.jsonl")[-1]["block"] == "debrief"
    assert parse_done_text("debrief 10 short")[0]["block"] == "debrief"
    assert logmod.week_summary(logmod.load(root / "data" / "log.jsonl"), "2026-09-06")["minutes"]["debrief"] == 15
    capsys.readouterr()


def test_hub_day_page_carries_the_debrief_and_the_job_links(roots):
    root, parent = roots
    shutil.copy(FIX / "notes.md", root / "profile" / "notes.md")
    save_plan(root, build_plan(root, parent, "2026-09-06"))
    out = root / "site" / "hub"
    build_site(root, parent, out, "2026-09-06")
    index = json.loads((out / "index.json").read_text())
    d6 = index["days"]["2026-09-06"]
    assert [e["title"] for e in d6["debrief_due"]] == ["Agents meetup"]
    assert d6["plan"]["blocks"][0]["id"] == "debrief"
    day = (out / "day" / "2026-09-06.html").read_text()
    assert "Debrief due: <a href=\"https://lu.ma/agents-meetup\"" in day
    assert '<option value="Agents meetup" selected>Agents meetup (yesterday)</option>' in day   # the form preselects it
    assert 'id="blk-debrief"' in day or '"id": "debrief"' in day

    d5 = (out / "day" / "2026-09-05.html").read_text()                     # the Job list panel's links
    assert 'href="../../archive/2026-09-05.html">Open the job report for 2026-09-05</a>' in d5
    assert 'href="https://github.com/example-user/job-radar/tree/main/data/applications/mercor-1"' in d5
    assert "application folder" in d5 and "stage → phone" in d5 and "recruiter call" in d5
    d4 = (out / "day" / "2026-09-04.html").read_text()
    assert "application folder" not in d4                                  # no folder on disk for stripe:2 / apple:4
    assert debrief_form_html({"date": "2026-09-06", "events": [], "debrief_due": [{"title": "X"}]}).count("(yesterday)") == 1
