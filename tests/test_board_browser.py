"""Browser regression tests use a completely synthetic day."""
import json
import pytest
from studyplan.board import serve_in_thread
from studyplan.plan import Plan, Block, Event, save_plan, load_plan

pytestmark = pytest.mark.browser
DATE = "2026-09-08"

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

from studyplan.plan import load_plan, save_plan


def test_relative_first_action_opens_a_rendered_repo_file(board, browser):
    root, base = board
    (root / "profile" / "notes.md").write_text("# Coach notes\n\nA concrete repair.")
    plan = load_plan(root, DATE)
    plan.block("design").first_action = "open [coach notes](profile/notes.md) and read the first screen."
    save_plan(root, plan)

    page = browser.new_page(viewport={"width": 1200, "height": 800})
    page.goto(base + "/#design", wait_until="networkidle")
    page.wait_for_selector("#tab-study #first")
    assert page.get_attribute("#first", "href") == "/files/study-plan/profile/notes.md"
    with page.expect_popup() as popup_info:
        page.click("#first")
    popup = popup_info.value
    popup.wait_for_load_state("networkidle")
    assert "Coach notes" in popup.inner_text("body")
    popup.close()
    page.close()


def test_plain_path_and_item_action_advancement_survive_reload_and_undo(board, browser):
    from studyplan.plan import Item
    root, base = board
    (root / 'docs').mkdir(exist_ok=True)
    (root / 'docs/steps.md').write_text('# First\n\nStart.\n\n## Second\n\nContinue.\n\n## Third\n\nFinish.')
    plan = load_plan(root, DATE)
    b = plan.block('design')
    b.first_action = 'Open ./docs/steps.md#first and read.'
    b.items = [Item('step-1', 'First (5 min)'),
               Item('step-2', 'Second (5 min)', first_action='Open the second step', action_url='./docs/steps.md#second'),
               Item('step-3', 'Third (5 min)', first_action='Open the third step', action_url='./docs/steps.md#third')]
    save_plan(root, plan)
    page = browser.new_page()
    page.goto(base + '/#design')
    assert page.get_attribute('#first', 'href') == '/files/study-plan/docs/steps.md#first'
    with page.expect_popup() as opened:
        page.click('#first')
    opened.value.wait_for_load_state()
    assert opened.value.locator('#first').inner_text() == 'First'
    opened.value.close()
    page.check('#it-step-1')
    page.wait_for_function("document.querySelector('#first').dataset.item === 'step-2'")
    assert page.get_attribute('#first', 'href').endswith('#second')
    page.reload()
    assert page.get_attribute('#first', 'data-item') == 'step-2'
    page.uncheck('#it-step-1')
    page.wait_for_function("document.querySelector('#first').dataset.item === 'step-1'")
    page.check('#it-step-1')
    page.wait_for_function("document.querySelector('#first').dataset.item === 'step-2'")
    page.once('dialog', lambda d: d.accept('Already covered; defer this step'))
    page.locator('li').filter(has=page.locator('#it-step-2')).get_by_role('button', name='暂不做', exact=True).click()
    page.wait_for_function("document.querySelector('#first').dataset.item === 'step-3'")
    page.check('#it-step-3')
    page.wait_for_selector('#actions-complete')
    assert page.locator('a#first').count() == 0
    page.close()
