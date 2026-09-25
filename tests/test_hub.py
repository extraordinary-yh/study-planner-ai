"""The hub site: the date index over the repo, the month / week / day pages, and the copied files."""
import json
import shutil
from pathlib import Path

import pytest

from studyplan import log as logmod
from studyplan.cli import main
from studyplan.hub import (build_index, build_site, ledger_by_day, md_to_html, notes_sections, ref_kind, ref_link,
                           render_file_page, week_days, week_id)
from studyplan.plan import build_plan, save_plan

FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def hub(roots):
    """Two planned days, a log row, a hosted tick, a notes file and the sf-events fixtures; the site built once."""
    root, parent = roots
    shutil.copy(FIX / "notes.md", root / "profile" / "notes.md")
    main(["--date", "2026-09-20", "leetcode", "import", str(FIX / "leetcode.csv")])
    plan = build_plan(root, parent, "2026-09-07")
    plan.call = "Kill the binary-search freeze first."
    save_plan(root, plan)
    save_plan(root, build_plan(root, parent, "2026-09-08"))
    logmod.append(root / "data" / "log.jsonl", {"ts": "2026-09-07T20:00:00", "date": "2026-09-07", "block": "leetcode", "minutes": 40,
                                                 "target": "leetcode", "source": "board"})
    item = plan.blocks[0].items[0].id
    logmod.append(root / "data" / "log.jsonl", {"ts": "2026-09-07T21:00:00", "date": "2026-09-07", "block": "news", "minutes": 0,
                                                 "target": item, "item": item, "source": "hub"})
    out = root / "site" / "hub"
    info = build_site(root, parent, out, "2026-09-08")
    return root, parent, out, info, item


def test_dates():
    import datetime as dt
    assert week_id(dt.date(2026, 9, 8)) == "2026-W37" and week_days("2026-W37")[0] == dt.date(2026, 9, 7)
    assert week_days("2026-W37")[-1] == dt.date(2026, 9, 13)


def test_notes_sections_and_markdown():
    sections = notes_sections(FIX / "notes.md")
    assert set(sections) == {"2026-09-07", "2026-09-06"}
    deb = sections["2026-09-07"][0]
    assert deb["title"] == "debrief: Agents meetup" and deb["body"].startswith("**What did you do?**")
    html = md_to_html(sections["2026-09-06"][0]["body"])
    assert "<ul>" in html and "<li>A fictional learner needs practice" in html and "<table>" in html and "<td>binary_search</td>" in html
    html = md_to_html("# T\n\ntext with `code` and **bold** and [a](https://x.y/z) and https://bare.example\n\n```\nraw <b>\n```\n- [x] done\n- [ ] not\n")
    assert '<h1 id="t">T</h1>' in html and "<code>code</code>" in html and "<b>bold</b>" in html
    assert 'href="https://x.y/z"' in html and 'href="https://bare.example"' in html
    assert "<pre><code>raw &lt;b&gt;</code></pre>" in html and "<li>☑ done</li>" in html and "<li>☐ not</li>" in html
    diagram = md_to_html("```mermaid\nflowchart LR\nA --> B\n```")
    assert '<pre class="mermaid">' in diagram and "A --&gt; B" in diagram


def test_index_has_one_entry_per_date_with_everything_that_day(hub):
    root, parent, out, info, item = hub
    index = json.loads((out / "index.json").read_text())
    days = index["days"]
    assert index["today"] == "2026-09-08" and info["planned"] == 2
    d7 = days["2026-09-07"]
    assert d7["plan"]["call"] == "Kill the binary-search freeze first." and d7["minutes_done"] == 40
    assert next(b for b in d7["plan"]["blocks"] if b["id"] == "leetcode")["done"] is True     # from the log
    assert d7["plan"]["blocks"][0]["items"][0]["done"] is True                                 # the hosted tick
    assert d7["notes"][0]["title"] == "debrief: Agents meetup" and d7["report"] is None
    assert any(a["kind"] == "story" for a in d7["artifacts"]) and any(a["kind"] == "notes" for a in d7["artifacts"])
    d5 = days["2026-09-05"]
    assert d5["report"] == "reports/2026-09-05.html"
    assert [r["what"] for r in d5["ledger"]] == ["stage → phone"] and d5["ledger"][0]["company"] == "Mercor"
    assert d5["interviews"][0]["stage"] == "phone" and d5["interviews"][0]["dated"] is False
    assert [e["verdict"] for e in days["2026-09-05"]["events"]] == ["went"]
    assert [e["title"] for e in days["2026-09-12"]["events"]] == ["Founders dinner", "Agents hackathon"]
    assert days["2026-09-04"]["ledger"][0]["what"] == "followup" and days["2026-09-04"]["ledger"][1]["what"] == "bad"
    assert days["2026-09-06"]["notes"][0]["title"] == "first assessment"
    assert days["2026-09-03"]["leetcode"][0]["title"] == "Word Break"                       # from the CSV import
    assert "2026-08-21" in days                                                              # LIS attempt inside the window


def test_ledger_reads_titles_from_seen_json(roots):
    root, parent = roots
    (parent / "data" / "seen.json").write_text(json.dumps({"jobs": {"mercor:1": {"company": "Mercor", "title": "SWE, Agents", "url": "https://m/1"}}}))
    rows = ledger_by_day(parent)["2026-09-05"]
    assert rows[0]["title"] == "SWE, Agents" and rows[0]["url"] == "https://m/1" and rows[0]["folder"] == "data/applications/mercor-1"


def test_month_week_day_pages_carry_the_ids_and_links(hub):
    root, parent, out, info, item = hub
    month = (out / "month" / "2026-09.html").read_text()
    assert 'id="d-2026-09-07"' in month and 'class="cell planned" href="../day/2026-09-07.html"' in month
    assert "40 min" in month and "Kill the binary-search" in month
    assert 'id="d-2026-09-05"' in month and "●" in month and "◆" in month                 # interview and event dots
    assert 'class="cell planned today" href="../day/2026-09-08.html"' in month
    assert 'href="../month/2026-08.html"' in month and 'href="../month/2026-10.html"' in month

    week = (out / "week" / "2026-W37.html").read_text()
    assert 'id="col-2026-09-07"' in week and 'href="../day/2026-09-07.html#blk-leetcode"' in week
    assert 'class="wb blk done"' in week                                                     # LeetCode ticked off
    assert "Agents hackathon" in week and 'href="https://lu.ma/agents-hack"' in week         # going, as a busy block
    assert 'href="../week/2026-W36.html"' in week and 'href="../week/2026-W38.html"' in week
    w36 = (out / "week" / "2026-W36.html").read_text()
    assert "● Mercor phone" in w36 and 'href="../day/2026-09-05.html"' in w36

    day = (out / "day" / "2026-09-07.html").read_text()
    assert 'id="panel-jobs"' in day and 'id="panel-artifacts"' in day and 'id="panel-debrief"' in day
    assert "debrief: Agents meetup" in day and "What did you do?" in day
    assert 'id="readonly-note"' in day and "studyplan serve --date 2026-09-07" in day
    assert '"files":' in day and "../files/profile/stories/" in day                        # HUB_FILES for the refs
    assert 'href="../week/2026-W37.html"' in day and 'href="../month/2026-09.html"' in day
    assert 'href="../day/2026-09-08.html">2026-09-08 ›' in day
    assert '"jobs": {"date": "2026-09-05", "url": "../../archive/2026-09-05.html"}' in day     # the Jobs tab: newest on or before
    assert '"events": null' in day and '"prev": "2026-09-06.html", "next": "2026-09-08.html"' in day
    d5 = (out / "day" / "2026-09-05.html").read_text()
    assert 'href="../../archive/2026-09-05.html"' in d5 and "<b>Mercor</b>" in d5 and "stage → phone" in d5
    assert "No study plan that day" in d5

    idx = (out / "index.html").read_text()
    assert '"2026-W37"' in idx and 'week/2026-W37.html' in idx


def test_files_are_copied_into_the_site_and_refs_resolve(hub):
    root, parent, out, info, item = hub
    assert info["files"] >= 4
    stories = list((out / "files" / "profile" / "stories").glob("*.md.html"))
    assert stories and "<article" in stories[0].read_text()
    curriculum = (out / "files" / "study-plan" / "config" / "curriculum.yaml.html").read_text()
    assert 'id="m1l1"' in curriculum and "<b>Read:</b>" in curriculum
    lc = (out / "files" / "study-plan" / "data" / "leetcode.jsonl.html").read_text()
    assert 'id="word-break"' in lc and "<td>hint</td>" in lc
    notes = (out / "files" / "study-plan" / "profile" / "notes.md.html").read_text()
    assert 'id="2026-09-07-debrief-agents-meetup"' in notes
    assert (out / "files" / "study-plan" / "plans" / "2026-09-07.md.html").exists()

    assert ref_kind("https://x") == "url" and ref_kind("profile/stories/a.md") == "md"
    assert ref_kind("data/applications/x") == "github" and ref_link("data/applications/x").startswith("https://github.com/")
    assert ref_link("study-plan/config/curriculum.yaml#m1l1") == "../files/study-plan/config/curriculum.yaml.html#m1l1"
    assert render_file_page("profile/stories/missing.md", root, parent) is None
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "diagram.md").write_text("```mermaid\nflowchart LR\nA --> B\n```\n")
    rendered = render_file_page("study-plan/docs/diagram.md", root, parent)
    assert rendered is not None and 'class="mermaid"' in rendered and "mermaid@11" in rendered


def test_cli_builds_into_site_hub(roots, capsys):
    root, parent = roots
    save_plan(root, build_plan(root, parent, "2026-09-07"))
    assert main(["--date", "2026-09-08", "hub", "build"]) == 0
    out = capsys.readouterr().out
    assert "hub:" in out and "1 with a plan" in out and str(root / "site" / "hub") in out
    assert (root / "site" / "hub" / "day" / "2026-09-07.html").exists()
