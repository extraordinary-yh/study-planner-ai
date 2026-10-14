import pytest
from studyplan.board import api_item_decision, api_done, BoardError
from studyplan.plan import build_plan, save_plan, load_plan, to_dict, parse_md, render_md


def test_decision_preserves_work_and_roundtrips(roots):
    root, parent = roots
    plan = build_plan(root, parent, "2026-09-07")
    save_plan(root, plan)
    block, item = plan.blocks[0], plan.blocks[0].items[0]
    body = dict(date=plan.date, block=block.id, item=item.id, state="not_applicable", reason="Lower value for this interview")
    api_item_decision(root, body)
    saved = load_plan(root, plan.date)
    changed = saved.blocks[0].items[0]
    assert changed.text == item.text and not changed.done and changed.not_applicable
    assert not (root / "data/log.jsonl").exists()
    assert to_dict(parse_md(render_md(saved))) == to_dict(saved)
    with pytest.raises(BoardError):
        api_done(root, body)
    api_item_decision(root, {**body, "state": "applicable", "reason": "New interview requires it"})
    restored = load_plan(root, plan.date).blocks[0].items[0]
    assert not restored.not_applicable and len(restored.decisions) == 2
    assert not restored.done
    with pytest.raises(BoardError):
        api_item_decision(root, {**body, "reason": " "})
