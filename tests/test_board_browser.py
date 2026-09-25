"""Browser regression tests use a completely synthetic day."""
import json
import pytest
from studyplan.board import serve_in_thread
from studyplan.plan import Plan, Block, Event, save_plan, load_plan

pytestmark = pytest.mark.browser

@pytest.fixture
def board(roots):
    root, parent = roots
    design = Block(id="design", title="System design · HTTP requests", minutes=30, start="10:00",
                   first_action="open https://example.com/http and read the example",
                   description=["Purpose: explain a request lifecycle."])
    design.add_item("Trace the request (20 min)")
    design.add_item("Explain a failure case (10 min)")
    code = Block(id="leetcode", title="Coding · Two Sum", minutes=30, start="11:00",
                 first_action="open https://leetcode.com/problems/two-sum/", description=["Purpose: practice hash maps."])
    code.add_item("Solve and test (30 min)")
    plan = Plan(date="2026-09-08", minutes=60, radar=[], blocks=[design,code], call="Practice HTTP, then solve a coding problem.")
    save_plan(root,plan)
    server, base = serve_in_thread(root,plan.date)
    yield root,base
    server.shutdown();server.server_close()

def test_selection_tick_and_undo_persist(board,browser):
    root,base=board
    page=browser.new_page(viewport={"width":1440,"height":900})
    errors=[];page.on("pageerror",lambda e:errors.append(str(e)))
    page.goto(base+"/#design")
    assert "HTTP requests" in page.inner_text("#tab-study h2")
    page.check("#it-design-1")
    page.wait_for_function("() => document.querySelector('#blk-design .m').textContent.includes('1/2')")
    rows=[json.loads(x) for x in (root/'data/log.jsonl').read_text().splitlines()]
    assert rows[-1]['target']=='design-1'
    page.uncheck("#it-design-1")
    page.wait_for_function("() => document.querySelector('#blk-design .m').textContent.includes('0/2')")
    assert not errors
    page.close()

def test_mobile_board_and_missing_integrations(board,browser):
    _,base=board
    page=browser.new_page(viewport={"width":390,"height":844})
    page.goto(base+"/#leetcode")
    assert "Two Sum" in page.inner_text("#tab-study h2")
    assert page.locator("#it-leetcode-1").is_visible()
    page.goto(base+"/#jobs")
    assert "on" in page.get_attribute("#tab-b-jobs","class")
    page.close()
