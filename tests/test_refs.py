"""Item -> artifact refs, and done state read from the log (the hosted page appends log rows only)."""
import json
import urllib.request

from studyplan import log as logmod
from studyplan.board import plan_payload, serve_in_thread
from studyplan.cli import main
from studyplan.plan import apply_log, build_plan, from_dict, load_plan, parse_md, render_md, save_plan, to_dict


def test_generator_fills_refs_where_it_knows_the_artifact(roots):
    root, parent = roots
    main(["--date", "2026-09-20", "leetcode", "import", str(__import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "leetcode.csv")])
    plan = build_plan(root, parent, "2026-09-20")
    design = plan.block("design")
    read, do = design.items[0], design.items[1]
    assert read.refs[0] == "study-plan/config/curriculum.yaml#m1l1" and any(r.startswith("https://") for r in read.refs)
    assert do.refs == ["study-plan/config/curriculum.yaml#m1l1"]
    reviews = [i for i in plan.block("leetcode").items if i.text.startswith("review:")]
    assert reviews and reviews[0].refs[0].startswith("https://leetcode.com/problems/") \
        and reviews[0].refs[1].startswith("study-plan/data/leetcode.jsonl#")
    new = [i for i in plan.block("leetcode").items if i.text.startswith("new ")]
    assert all(i.refs == [] for i in new)                                # nothing to link yet
    stories = plan.block("stories")
    assert stories.items[0].refs == [f"profile/stories/{stories.items[0].text.split('stories/')[1].split('.md')[0]}.md"]
    assert stories.items[1].refs == ["profile/context/98-interview-map.md"]


def test_refs_round_trip_through_json_and_markdown(roots):
    root, parent = roots
    plan = build_plan(root, parent, "2026-09-07")
    plan.blocks[0].add_item("read the thing", refs=["https://example.com/a?b=1", "profile/notes.md#2026-09-07"])
    md = render_md(plan)
    assert "- [ ] read the thing <!--refs:https://example.com/a?b=1|profile/notes.md#2026-09-07-->" in md
    assert to_dict(parse_md(md)) == to_dict(plan)
    assert to_dict(from_dict(json.loads(json.dumps(to_dict(plan))))) == to_dict(plan)
    old = {"date": "2026-09-07", "minutes": 60, "blocks": [{"id": "a", "title": "A", "minutes": 60,
                                                             "items": [{"id": "a-1", "text": "x"}]}]}
    assert from_dict(old).blocks[0].items[0].refs == []                  # plans written before refs still load


def test_net_is_sequential_so_a_retick_after_an_undo_counts():
    rows = [{"date": "d", "target": "a", "block": "news", "minutes": 0},
            {"date": "d", "target": "a", "undo": True},
            {"date": "d", "target": "a", "block": "news", "minutes": 0},
            {"date": "d", "target": "b", "block": "news", "minutes": 10},
            {"date": "e", "target": "b", "undo": True}]                  # a different day's undo does not reach back
    netted = logmod.net(rows)
    assert [(r["date"], r["target"]) for r in netted] == [("d", "a"), ("d", "b")]
    assert logmod.done_targets(rows, "d") == {"a", "b"} and logmod.done_targets(rows, "e") == set()


def test_done_state_comes_from_the_log_too(roots):
    root, parent = roots
    plan = build_plan(root, parent, "2026-09-07")
    save_plan(root, plan)
    item = plan.blocks[0].items[0].id
    logmod.append(root / "data" / "log.jsonl", {"ts": "2026-09-07T20:00:00", "date": "2026-09-07", "block": "news",
                                                 "minutes": 0, "target": item, "item": item, "source": "hub"})
    logmod.append(root / "data" / "log.jsonl", {"ts": "2026-09-07T21:00:00", "date": "2026-09-07", "block": "leetcode",
                                                 "minutes": 40, "target": "leetcode", "source": "hub"})
    assert load_plan(root, "2026-09-07").blocks[0].items[0].done is False    # the JSON was not touched
    payload = plan_payload(root, "2026-09-07")
    assert payload["blocks"][0]["items"][0]["done"] is True                   # but the board shows the tick
    assert next(b for b in payload["blocks"] if b["id"] == "leetcode")["done"] is True
    assert apply_log(build_plan(root, parent, "2026-09-07"), set()).blocks[0].items[0].done is False

    server, base = serve_in_thread(root, "2026-09-07")
    try:
        req = urllib.request.Request(base + "/api/undo", data=json.dumps({"date": "2026-09-07", "block": "news", "item": item}).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            after = json.loads(r.read())
        assert after["blocks"][0]["items"][0]["done"] is False               # the local undo nets the hosted tick out
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            html = r.read().decode()
        assert "refNodes" in html and "HUB_FILES" in html
    finally:
        server.shutdown()
        server.server_close()
