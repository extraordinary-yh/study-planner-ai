import json
from pathlib import Path

import yaml

from studyplan.cli import main
from studyplan.curriculum import flat_lessons, load_progress, mark_done, next_lessons
from studyplan.config import load_curriculum
from studyplan.plan import Block, Item, Plan, allocate, build_plan, parse_done_text, quality_issues, render, save_plan, write_plan

FIX = Path(__file__).resolve().parent / "fixtures"


def test_allocate_rounds_to_five_and_sums():
    mins = allocate(180, {"news": 0.1, "stack": 0.15, "system_design": 0.3, "leetcode": 0.4, "stories": 0.05})
    assert sum(mins.values()) == 180 and all(m % 5 == 0 for m in mins.values()) and mins["leetcode"] >= mins["system_design"]


def test_curriculum_progress(roots):
    root, _ = roots
    modules = load_curriculum(root)
    lessons = flat_lessons(modules)
    assert lessons[0]["id"] == "m1l1" and len({l["id"] for l in lessons}) == len(lessons) >= 25
    assert all(l.get("read") and l.get("do") for l in lessons)
    own = {l["own_system"] for l in lessons if l.get("own_system")}
    context = {p.stem for p in (Path(__file__).resolve().parents[2] / "profile" / "context").glob("*.md")}
    assert own <= context, own - context                                  # every own_system names a real context file
    progress = load_progress(root / "data" / "progress.json")
    mark_done(progress, "m1l1", "2026-09-06", 45, "")
    assert next_lessons(modules, progress, 1)[0]["id"] == "m1l2"


def test_plan_without_history(roots):
    root, parent = roots
    (root / "data" / "remnote-problem-catalog.jsonl").write_text("{}\n{}\n")
    plan = build_plan(root, parent, "2026-09-07")            # a Monday: weekday budget
    assert plan.minutes == 240 and [b.id for b in plan.blocks] == ["news", "stack", "design", "leetcode", "stories"]
    assert any("No assessment" in r for r in plan.radar)
    assert any("Mercor phone" in r for r in plan.radar)      # stage set 2026-09-05, within 7 days
    stack = plan.blocks[1]
    assert stack.title.startswith("Stack gap · ") and "Mercor posting" in stack.lines[0]   # pinned to the interview company
    assert plan.blocks[2].title.startswith("System design · m1l1")
    assert "Delivery Framework (required)" in plan.blocks[2].first_action
    assert plan.blocks[2].first_action.endswith("/delivery) and read the first screen.")
    lc = plan.blocks[3]
    assert sum("new medium" in l for l in lc.lines) >= 1 and any("No history yet" in l for l in lc.lines)
    assert any("RemNote candidate pool: 2 note-only" in l for l in lc.lines)
    st = plan.blocks[4]
    assert any("profile/stories/acme-media-" in l for l in st.lines) and any("Mock" in l for l in st.lines) and any("Mercor" in l for l in st.lines)
    md = render(plan)
    assert md.startswith("# Study plan — 2026-09-07 (Mon) · 240 min") and "## 4. LeetCode" in md
    path = write_plan(root, plan)
    assert path.read_text() == md
    try:
        write_plan(root, plan)
        raise AssertionError("expected FileExistsError")
    except FileExistsError:
        pass


def test_plan_modifiers_and_history(roots):
    root, parent = roots
    level = yaml.safe_load((root / "profile" / "level.yaml").read_text())
    level["assessed_on"] = "2026-09-06"
    level["coding"]["overall"] = 2
    level["coding"]["topics"]["heaps"] = 1
    level["constraints"]["weekend_minutes"] = 240
    level["stack"] = {"FastAPI": "working", "Kubernetes": "working", "LangChain/LangGraph": "working", "RL environments": "working"}
    (root / "profile" / "level.yaml").write_text(yaml.safe_dump(level))
    # no interview any more: move the clock past the window
    main(["--date", "2026-09-20", "leetcode", "import", str(FIX / "leetcode.csv")])
    (root / "data" / "log.jsonl").write_text(json.dumps({"date": "2026-09-19", "block": "stack", "minutes": 20, "term": "Java"}) + "\n")
    plan = build_plan(root, parent, "2026-09-20")            # a Sunday
    assert plan.minutes == 240 and not any("No assessment" in r for r in plan.radar)
    assert any("Last 7 days: 20 min" in r for r in plan.radar)
    assert not plan.blocks[1].title.endswith("Java")          # studied this fortnight; next gap instead
    lc = plan.blocks[3]
    assert lc.minutes > plan.blocks[2].minutes                # weak coding raises leetcode above system design
    reviews = [l for l in lc.lines if "review:" in l]
    assert reviews and "Course Schedule" in reviews[0]        # gave up first, most overdue
    assert any("**heaps**" in l for l in lc.lines)            # rated 1 and never attempted -> weakest


def test_cli_roundtrip(roots, capsys):
    root, parent = roots
    assert main(["--date", "2026-09-07", "status"]) == 0
    out = capsys.readouterr().out
    assert "assessed: never" in out and "interview: Mercor phone" in out and "next m1l1" in out
    assert main(["stack", "--gaps", "--limit", "5"]) == 0
    assert "GAP" in capsys.readouterr().out
    assert main(["--date", "2026-09-06", "leetcode", "add", "--problem", "Two Sum", "--difficulty", "easy", "--topics", "Array, Hash Table", "--minutes", "7"]) == 0
    assert "added: Two Sum (easy, arrays_hashing) clean 7 min" in capsys.readouterr().out
    assert main(["--date", "2026-09-06", "leetcode", "due"]) == 0 and "nothing due" in capsys.readouterr().out
    assert main(["--date", "2026-09-06", "leetcode", "stats"]) == 0 and "arrays_hashing" in capsys.readouterr().out
    assert main(["--date", "2026-09-06", "lesson", "done", "m1l1", "--minutes", "40"]) == 0
    assert "next: m1l2" in capsys.readouterr().out
    assert main(["lesson", "done", "nope"]) == 1
    capsys.readouterr()
    assert main(["--date", "2026-09-06", "done", "leetcode", "--minutes", "30", "--note", "two easies"]) == 0
    assert main(["curriculum", "--all"]) == 0 and "[x] m1l1" in capsys.readouterr().out
    # Fixture parsing must not depend on live feed outages in personal config.
    feed_path = root / "config" / "news.yaml"
    feeds = yaml.safe_load(feed_path.read_text())
    for feed in feeds["feeds"]:
        feed["enabled"] = True
    feed_path.write_text(yaml.safe_dump(feeds))
    assert main(["news", "--fixtures", str(FIX / "feeds"), "--now", "2026-09-06T12:00:00+00:00"]) == 0
    news = capsys.readouterr().out
    assert "OpenAI ships" in news and "WARNING" in news              # eight feeds have no fixture
    assert main(["--date", "2026-09-07", "plan", "--fixtures", str(FIX / "feeds"), "--now", "2026-09-06T12:00:00+00:00", "--minutes", "120"]) == 0
    text = (root / "plans" / "2026-09-07.md").read_text()
    assert "· 120 min" in text and "OpenAI ships" in text and "**Warnings**" in text
    assert main(["--date", "2026-09-07", "plan", "--no-news"]) == 2  # refuses to overwrite
    assert main(["--date", "2026-09-07", "plan", "--no-news", "--force"]) == 0
    assert main(["assess"]) == 0 and "unanswered" in capsys.readouterr().out
    log = [json.loads(l) for l in (root / "data" / "log.jsonl").read_text().splitlines()]
    assert [e["block"] for e in log] == ["design", "leetcode"]


def test_remnote_sync_requires_runtime_auth(roots, monkeypatch, capsys):
    monkeypatch.delenv("REMNOTE_MCP_AUTH", raising=False)
    assert main(["remnote", "sync"]) == 2
    assert "REMNOTE_MCP_AUTH is required" in capsys.readouterr().out


def test_parse_done_text():
    entries = parse_done_text("done leetcode 70 two DP problems\nstack 25 term=Kafka read the docs\n\n")
    assert entries[0] == {"block": "leetcode", "minutes": 70, "note": "two DP problems"}
    assert entries[1]["term"] == "Kafka" and entries[1]["minutes"] == 25
    try:
        parse_done_text("nap 30")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_plan_clarity_check_requires_direct_start_resource_and_timeboxed_tasks(roots, capsys):
    root, _ = roots
    good = Plan("2026-09-07", 30, [], [
        Block("design", "System design · delivery framework", 30, "13:00",
              "open [Delivery Framework](https://example.com/delivery) and copy the five steps.",
              ["Learn the repeatable interview opening."],
              [Item("design-1", "Read the framework and write the five steps (30 min).")]),
    ])
    assert quality_issues(good) == []
    save_plan(root, good)
    assert main(["--date", "2026-09-07", "plan", "check"]) == 0
    assert "PASS" in capsys.readouterr().out

    vague = Plan("2026-09-08", 30, [], [
        Block("design", "System design", 30, "13:00", "search for system-design frameworks",
              ["Learn the opening."], [Item("design-1", "Read the two opening-framework resources.")]),
    ])
    issues = quality_issues(vague)
    assert any("exact page or file" in issue for issue in issues)
    assert any("time-box" in issue for issue in issues)
    assert any("vague resource" in issue for issue in issues)
