import json
import datetime as dt
import pytest

from studyplan.board import BoardError, plan_payload, serve_in_thread
from studyplan.hub import build_site
from studyplan.plan import Block, Item, Plan, load_plan, save_plan
from studyplan.session import api_session, load_state, prompt_presentation, render_session, session_issues, session_payload

DATE = '2026-10-13'


@pytest.fixture
def focused(roots):
    root, parent = roots
    save_plan(root, Plan(DATE, 10, [], [Block('case', 'Customer discovery', 10, '13:00',
        'Open the focused case', items=[Item('case-1', 'Discover (5 min)'), Item('case-2', 'Play back (5 min)')])]))
    (root / 'sessions').mkdir()
    (root / 'coaching').mkdir()
    (root / 'coaching/case.json').write_text('{"answer":"COACH_ONLY_SECRET"}')
    (root / 'sessions' / (DATE + '.json')).write_text(json.dumps({
        'coach_ref': 'study-plan/coaching/case.json', 'steps': {
            'case-1': {'title': 'Ask your first question', 'goal': 'Understand one recent case.',
                       'prompt': 'Customer: Our workflow is slow. What would you ask?', 'mode': 'Interview mock'},
            'case-2': {'title': 'Confirm the problem', 'goal': 'Agree on scope.',
                       'prompt': 'FUTURE_PROMPT: Give your playback.', 'notes': ['Only confirmed facts.']}}}))
    return root, parent


def act(root, action, item='case-1', **fields):
    return api_session(root, {'date': DATE, 'block': 'case', 'item': item, 'action': action, **fields})


def test_focus_is_a_projection_not_the_day_or_coach_context(focused):
    root, parent = focused
    payload = session_payload(root, DATE)
    page = render_session(payload)
    assert 'FUTURE_PROMPT' not in page and 'COACH_ONLY_SECRET' not in page
    assert 'Our workflow is slow' in page
    assert payload['next']['title'] == 'Confirm the problem'
    assert payload['status'] == 'ready'
    assert payload['timer'] == {'budget_seconds': 300, 'elapsed_seconds': 0, 'running_since': None}
    assert len(payload['handoff']) < 250 and 'Saved checkpoint:' not in payload['handoff']
    assert not (root / 'data/active-study').exists()  # viewing never starts a session
    assert plan_payload(root, DATE)['blocks'][0]['items'][0]['session'] is True
    assert session_issues(load_plan(root, DATE), root, parent) == []
    out = root / 'site/hub'
    build_site(root, parent, out, DATE, write=False)
    exported = out / 'sessions' / DATE / 'case-1.html'
    assert exported.exists() and 'FUTURE_PROMPT' not in exported.read_text()
    assert 'COACH_ONLY_SECRET' not in exported.read_text()
    assert not (out / 'files/study-plan/coaching/case.json.html').exists()
    assert session_payload(root, DATE, 'case', 'case-1', live=False)['calendar'] == '../../day/2026-10-13.html#case'


def test_lifecycle_checkpoints_idempotent_completion_and_restore(focused):
    root, _ = focused
    assert act(root, 'start')['status'] == 'in_progress'
    assert act(root, 'start')['status'] == 'in_progress'
    assert not (root / 'data/log.jsonl').exists()
    assert load_state(root, DATE)['steps']['case-1']['minutes_actual'] is None
    act(root, 'question', text='Who owns the source of truth?')
    assert session_payload(root, DATE)['prompt'] == 'Who owns the source of truth?'
    act(root, 'checkpoint', text='Needed a hint to separate owner from tool.', assistance='guided')
    act(root, 'pause')
    assert session_payload(root, DATE)['status'] == 'paused'
    assert session_payload(root, DATE)['checkpoint'] == 'Needed a hint to separate owner from tool.'
    act(root, 'start')
    assert act(root, 'complete')['item'] == 'case-2'
    act(root, 'complete')  # retry after an ambiguous response must not duplicate the completion log
    assert len((root / 'data/log.jsonl').read_text().splitlines()) == 1
    assert act(root, 'undo')['item'] == 'case-1'
    assert not load_plan(root, DATE).blocks[0].items[0].done
    act(root, 'complete')
    from studyplan.board import api_undo
    api_undo(root, {'date': DATE, 'block': 'case', 'item': 'case-1'})
    assert session_payload(root, DATE)['status'] == 'paused'
    assert act(root, 'skip', text='Covered in another practice')['item'] == 'case-2'
    assert act(root, 'undo')['status'] == 'paused'
    act(root, 'complete')
    assert act(root, 'complete', 'case-2')['finished'] is True
    assert session_payload(root, DATE)['finished'] is True
    assert not load_plan(root, DATE).blocks[0].done  # no scheduled block duration inferred


def test_bad_ids_dates_and_failed_actions_do_not_record_progress(focused):
    root, _ = focused
    with pytest.raises(ValueError):
        session_payload(root, '../../secret')
    with pytest.raises(BoardError):
        act(root, 'start', 'missing')
    with pytest.raises(BoardError):
        act(root, 'question', text='')
    assert not (root / 'data/log.jsonl').exists()
    assert load_state(root, DATE)['steps'] == {}


def test_scenario_hierarchy_preserves_content_and_escapes_html(focused):
    root, _ = focused
    context = ('A consulting company reviews employee expense claims with supporting receipts. '
               'The finance manager says, “We review each request, then approve it.”')
    question = 'What would you ask next to uncover an unstated rule?'
    original = 'Practice scenario: ' + context + ' ' + question
    parts = prompt_presentation(original)
    assert parts == {'heading': 'Practice scenario', 'paragraphs': [context], 'question': question}
    act(root, 'question', text=original)
    assert session_payload(root, DATE)['prompt'] == original
    assert session_payload(root, DATE)['presentation'] == parts
    unsafe = original.replace('consulting company', '<script>window.bad=1</script> company')
    payload = session_payload(root, DATE)
    payload['prompt'] = unsafe
    assert '<script>window.bad=1</script>' not in render_session(payload)
    direct = 'What would you ask before designing a system for a customer whose workflow you do not understand?'
    assert prompt_presentation(direct)['question'] == direct
    assert not prompt_presentation(direct)['paragraphs']


def test_timer_resume_question_changes_expiry_and_next_step(focused, monkeypatch):
    from studyplan import session
    root, _ = focused
    now = dt.datetime(2026, 10, 13, 17, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(session, '_now', lambda: now)
    # Preparing the first question is enough for the chat; the timer is optional.
    prepared = act(root, 'question', text='Which workflow would you ask about?')
    assert prepared['status'] == 'ready' and prepared['active_step']
    assert prepared['timer']['running_since'] is None
    first = act(root, 'start')['timer']['running_since']
    now += dt.timedelta(seconds=25)
    assert act(root, 'start')['timer']['running_since'] == first  # retry is idempotent
    act(root, 'question', text='Who owns the records?')
    assert session_payload(root, DATE)['timer']['running_since'] == first
    paused = act(root, 'pause')
    assert paused['timer']['elapsed_seconds'] == 25
    assert paused['timer']['running_since'] is None
    now += dt.timedelta(minutes=20)
    assert session_payload(root, DATE)['timer']['elapsed_seconds'] == 25
    act(root, 'start')
    now += dt.timedelta(minutes=6)
    assert session_payload(root, DATE)['status'] == 'in_progress'
    assert not load_plan(root, DATE).blocks[0].items[0].done  # expiry cannot complete
    next_step = act(root, 'complete')
    assert next_step['item'] == 'case-2'
    assert next_step['timer'] == {'budget_seconds': 300, 'elapsed_seconds': 0, 'running_since': None}
    prior = load_state(root, DATE)['steps']['case-1']
    assert prior['timer']['elapsed_seconds'] == 385
    assert prior['timer']['running_since'] is None and prior['minutes_actual'] is None
    act(root, 'undo')
    act(root, 'start')
    now += dt.timedelta(seconds=10)
    act(root, 'question', 'case-2', text='Play back the problem.')
    assert load_state(root, DATE)['steps']['case-1']['timer']['running_since'] is None
    assert session_payload(root, DATE)['item'] == 'case-2'
    assert session_payload(root, DATE)['timer']['running_since'] is None
    act(root, 'checkpoint', 'case-2', text='A real checkpoint survives a timer reset.')
    reset = act(root, 'reset_timer', 'case-2')
    assert reset['timer']['elapsed_seconds'] == 0 and reset['timer']['running_since'] is None
    assert reset['prompt'] == 'Play back the problem.'
    assert reset['checkpoint'] == 'A real checkpoint survives a timer reset.'
    act(root, 'start', 'case-2')
    now += dt.timedelta(minutes=6)
    restarted = act(root, 'start', 'case-2', restart_timer=True)
    assert restarted['status'] == 'in_progress'
    assert restarted['timer'] == {'budget_seconds': 300, 'elapsed_seconds': 0,
                                 'running_since': now.isoformat(timespec='milliseconds')}
    assert restarted['prompt'] == reset['prompt'] and restarted['checkpoint'] == reset['checkpoint']
    assert not load_plan(root, DATE).blocks[0].items[1].done
    assert load_state(root, DATE)['steps']['case-2']['minutes_actual'] is None


@pytest.mark.browser
def test_interview_flow_in_browser_and_calendar_preserved(focused, browser, tmp_path):
    root, _ = focused
    server, base = serve_in_thread(root, DATE)
    try:
        page = browser.new_page(viewport={'width': 460, 'height': 900})
        # Ordinary study must not need clipboard access at all.
        page.context.add_init_script("window.copyCalls=0;Object.defineProperty(navigator, 'clipboard', {value:{writeText:()=>{window.copyCalls++;return Promise.reject(Error('Denied'))}}})")
        page.goto(base + '/#case')
        assert page.locator('#timeline').count() == 1
        assert '/study?' in page.get_attribute('#first', 'href')
        with page.expect_popup() as opened:
            page.click('#first')
        focus = opened.value
        focus.wait_for_load_state()
        assert focus.locator('#title').inner_text() == 'Ask your first question'
        assert focus.locator('#timeline').count() == 0
        assert 'FUTURE_PROMPT' not in focus.locator('body').inner_text()
        assert not focus.locator('#more').is_visible()
        assert focus.locator('#timer').inner_text() == '05:00'
        focus.click('#start')
        focus.wait_for_selector('#pause:visible')
        assert focus.evaluate('window.copyCalls') == 0
        assert not focus.locator('#more').is_visible()
        assert focus.locator('#handoff-text').input_value().startswith('Resume my 2026-10-13 study session')
        focus.wait_for_function("document.querySelector('#timer').textContent !== '05:00'")
        assert not (root / 'data/log.jsonl').exists()
        act(root, 'question', text='What happens when permission is missing?')
        focus.wait_for_function("document.querySelector('#question').textContent.includes('permission is missing')")
        focus.reload()
        assert 'permission is missing' in focus.locator('#question').inner_text()
        assert focus.locator('#pause').is_visible()
        focus.click('#more-toggle')
        focus.locator('#checkpoint summary').click()
        focus.fill('#note', 'Need to clarify who can approve publication.')
        focus.click('#pause')
        focus.wait_for_function("document.querySelector('#start').textContent === 'Resume'")
        focus.reload()
        focus.click('#more-toggle')
        focus.locator('#checkpoint summary').click()
        assert 'clarify who' in focus.locator('#saved-note').inner_text()
        focus.click('#start')
        focus.wait_for_selector('#pause:visible')
        focus.click('#complete')
        focus.wait_for_function("document.querySelector('#title').textContent === 'Confirm the problem'")
        assert not focus.locator('#more').is_visible()
        assert focus.locator('#timer').inner_text() == '05:00'
        assert focus.locator('#start').is_visible()
        assert 'Customer: Our workflow' not in focus.locator('body').inner_text()
        focus.click('#undo-last')
        focus.wait_for_function("document.querySelector('#title').textContent === 'Ask your first question'")
        assert not load_plan(root, DATE).blocks[0].items[0].done
        # Switching chats remains a quiet, optional fallback with manual copy recovery.
        focus.click('#more-toggle')
        focus.locator('#handoff summary').click()
        focus.click('#copy')
        assert 'Select and copy' in focus.locator('#status').inner_text()
        focus.click('#more-toggle')
        assert focus.evaluate('document.documentElement.scrollWidth <= innerWidth')
        focus.screenshot(path=str(tmp_path / 'focused-study.png'), full_page=True)
        focus.click('#calendar')
        assert focus.locator('#timeline').count() == 1
        focus.close(); page.close()
    finally:
        server.shutdown(); server.server_close()


@pytest.mark.browser
def test_chat_can_lead_without_starting_timer_and_failed_start_stays_ready(focused, browser, monkeypatch):
    root, _ = focused
    act(root, 'question', text='Ask about one recent case.')
    server, base = serve_in_thread(root, DATE)
    page = browser.new_page(viewport={'width': 390, 'height': 780}, reduced_motion='reduce')
    try:
        page.goto(base + '/study?date=' + DATE)
        assert page.locator('#question').inner_text() == 'Ask about one recent case.'
        assert page.locator('#timer').inner_text() == '05:00'
        scenario = ('Practice scenario: A consulting company receives expense requests with supporting receipts. '
                    'The finance team reviews each request and approves eligible expenses for repayment. '
                    'You are interviewing the finance manager before proposing changes. '
                    'The manager says that the team reviews each request, then approves it. '
                    'What would you ask next to uncover an unstated rule?')
        act(root, 'question', text=scenario)
        page.wait_for_function("document.querySelector('#question').textContent === 'What would you ask next to uncover an unstated rule?'")
        assert page.locator('#context-title').inner_text() == 'Practice scenario'
        assert 'receives expense requests' in page.locator('#context-copy').inner_text()
        assert page.locator('#context-copy').evaluate('e => getComputedStyle(e).textAlign') == 'left'
        assert page.locator('#question').evaluate('e => parseFloat(getComputedStyle(e).fontSize)') > page.locator('#context-copy').evaluate('e => parseFloat(getComputedStyle(e).fontSize)')
        assert page.locator('#timer').evaluate('e => e.getBoundingClientRect().bottom <= innerHeight')
        page.route('**/api/session', lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"Temporary failure"}'))
        page.click('#start')
        page.wait_for_function("document.querySelector('#status').textContent.includes('Not saved')")
        assert page.locator('#start').is_visible() and not page.locator('#pause').is_visible()
        assert load_state(root, DATE)['steps']['case-1']['status'] == 'ready'
        page.unroute('**/api/session')
        # The coach can finish a discussion and lead into the next without requiring Start.
        act(root, 'complete', text='Observed a complete playback.', assistance='independent')
        act(root, 'question', 'case-2', text='What remains unknown?')
        page.wait_for_function("document.querySelector('#question').textContent === 'What remains unknown?'")
        assert page.locator('#timer').inner_text() == '05:00'
        page.click('#start')
        page.wait_for_selector('#pause:visible')
        assert page.locator('body').evaluate("e => getComputedStyle(e, '::after').animationName") == 'none'
        # Browser time passing must not trigger completion or write study minutes.
        from studyplan import session
        real_now = session._now
        page.clock.install()
        monkeypatch.setattr(session, '_now', lambda: real_now() + dt.timedelta(seconds=301))
        page.clock.fast_forward(301000)
        page.wait_for_function("document.querySelector('#timer').textContent === '00:00'")
        assert 'finish your thought' in page.locator('#timer-note').inner_text()
        assert not load_plan(root, DATE).blocks[0].items[1].done
        assert load_state(root, DATE)['steps']['case-2']['minutes_actual'] is None
        assert page.get_by_role('button', name='Restart', exact=True).is_visible()
        assert not page.get_by_role('button', name='Pause', exact=True).is_visible()
        page.get_by_role('button', name='Restart', exact=True).click()
        page.wait_for_function("document.querySelector('#timer').textContent === '05:00'")
        assert page.get_by_role('button', name='Pause', exact=True).is_visible()
        assert page.locator('#question').inner_text() == 'What remains unknown?'
        assert not load_plan(root, DATE).blocks[0].items[1].done
        page.reload()
        assert page.get_by_role('button', name='Pause', exact=True).is_visible()
        page.click('#more-toggle')
        page.click('#reset-timer')
        page.wait_for_function("document.querySelector('#timer').textContent === '05:00'")
        page.wait_for_selector('#start:visible')
    finally:
        page.close()
        server.shutdown(); server.server_close()


@pytest.mark.browser
def test_question_survives_missing_timer_or_disabled_javascript(focused, browser, monkeypatch):
    from studyplan import session
    root, _ = focused
    payload = session_payload(root, DATE)
    payload.pop('timer')  # An older running server can return a payload without new fields.
    monkeypatch.setattr(session, 'session_payload', lambda *a, **kw: payload)
    server, base = serve_in_thread(root, DATE)
    try:
        for javascript in (False, True):
            page = browser.new_page(java_script_enabled=javascript, viewport={'width': 750, 'height': 850})
            try:
                page.goto(base + '/study?date=' + DATE)
                assert page.locator('#question').inner_text() == payload['prompt']
                if javascript:
                    assert 'Refresh the page' in page.locator('#timer-note').inner_text()
                    geometry = page.locator('#question').evaluate('(e) => {const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,w:innerWidth,h:innerHeight,align:getComputedStyle(e).textAlign}}')
                    assert abs(geometry['x'] - geometry['w']/2) < 2
                    assert abs(geometry['y'] - geometry['h']/2) < 90
                    assert geometry['align'] == 'left'
                    assert page.locator('#focus-card').evaluate('e => getComputedStyle(e).boxShadow') == 'none'
                    assert page.locator('#start').evaluate('e => getComputedStyle(e).borderTopWidth') == '0px'
            finally:
                page.close()
    finally:
        server.shutdown(); server.server_close()
