import json
import shutil
from pathlib import Path

import yaml

from studyplan.coaching import coding_candidates, load_coaching, pending_concepts
from studyplan.config import load_level, load_preferences
from studyplan.plan import Event, build_plan, quality_issues

PROJECT = Path(__file__).resolve().parents[1]


def enable(root, *, role_focus=False):
    shutil.copy(PROJECT / 'tests/fixtures/coaching_synthetic.yaml', root / 'profile/coaching.yaml')
    coaching = load_coaching(root)
    if not role_focus:
        # Keep the original foundation scenarios independent of today's role priorities.
        coaching['design']['concepts'] = [c for c in coaching['design']['concepts']
                                         if c.get('track') != 'ai_examples']
        coaching['design'].pop('priority_order', None)
        coaching['planning'].pop('focus_weights_final', None)
        coaching['planning']['focus_weights'].update(system_design=0.45, leetcode=0.55)
        (root / 'profile/coaching.yaml').write_text(yaml.safe_dump(coaching))
    return coaching


def test_role_focus_teaches_ai_with_foundations_and_computer_first(roots):
    root, parent = roots
    enable(root, role_focus=True)
    for budget in (20, 60, 120, 240):
        plan = build_plan(root, parent, '2026-09-15', minutes=budget)
        assert quality_issues(plan) == []
        assert sum(b.minutes for b in plan.blocks) <= budget
        if plan.block('leetcode') and plan.block('design'):
            assert plan.blocks.index(plan.block('leetcode')) < plan.blocks.index(plan.block('design'))
            assert plan.call.startswith('Start with LeetCode')
        if budget == 120:
            # Personal weights already incorporate readiness; old stage/weakness
            # multipliers must not reverse the intended coding emphasis.
            assert plan.block('leetcode').minutes == 70
            assert plan.block('design').minutes == 50
        if plan.block('design'):
            sd = plan.block('design')
            assert sd.title.endswith('HTTP and grounded AI')
            assert './docs/ai-system-design.md#start-here-http-meets-grounded-ai' in sd.first_action
            assert 'Why now:' in ' '.join(sd.description)
            assert 'No coding or typed write-up' in sd.items[-1].text
    assert not (root / 'data/progress.json').exists()


def test_role_priority_updates_and_observed_evidence_drive_next_lesson(roots):
    root, parent = roots
    coaching = enable(root, role_focus=True)
    coaching['design']['evidence']['ai_grounding'] = {'stage': 'independent', 'evidence': ''}
    assert pending_concepts(coaching)[0]['id'] == 'ai_grounding'
    coaching['design']['evidence']['ai_grounding']['evidence'] = 'Explained request, evidence and policy unaided.'
    assert pending_concepts(coaching)[0]['id'] == 'ai_evals'
    coaching['design']['priority_order'] = ['ai_workers', 'ai_evals']
    (root / 'profile/coaching.yaml').write_text(yaml.safe_dump(coaching))
    assert build_plan(root, parent, '2026-09-15').block('design').title.endswith('Workers in AI serving and evaluation')


def test_external_concept_resource_is_not_rewritten_as_repo_path(roots):
    root, parent = roots
    coaching = enable(root, role_focus=True)
    concept = next(c for c in coaching['design']['concepts'] if c['id'] == 'ai_grounding')
    concept['resource'] = 'https://example.test/http-lesson#request'
    (root / 'profile/coaching.yaml').write_text(yaml.safe_dump(coaching))
    plan = build_plan(root, parent, '2026-09-15')
    assert plan.block('design').links[0]['url'] == concept['resource']


def attempt(slug, date='2026-09-12', outcome='clean', topics=None):
    return dict(slug=slug, title=slug, url=f'https://leetcode.com/problems/{slug}/',
                date=date, outcome=outcome, difficulty='medium', minutes=25,
                topics=topics or ['graphs_topo_union_find'])


def test_personal_priority_is_topological_not_any_graph(roots):
    root, parent = roots
    enable(root)
    (root / 'data/leetcode.jsonl').write_text(json.dumps(attempt('pacific-atlantic-water-flow', outcome='gave_up'))+'\n')
    plan = build_plan(root, parent, '2026-09-13', minutes=120)
    lc = plan.block('leetcode')
    assert 'Course Schedule' in lc.items[0].text
    assert 'Design Add and Search Words' in lc.items[1].text
    assert 'Synthetic learner needs practice' in lc.items[0].text
    assert 'topological_sort' in lc.items[0].text
    assert 'low < high' in ' '.join(lc.description)
    assert plan.block('design').title.endswith('HTTP request and response')
    assert quality_issues(plan) == []
    assert not (root / 'data/progress.json').exists()


def test_success_not_reassigned_before_due_and_transfer_is_allowed(roots):
    root, _ = roots
    coaching = enable(root)
    (root / 'data/leetcode.jsonl').write_text(json.dumps(attempt('course-schedule'))+'\n')
    rows = coding_candidates(root, '2026-09-13', load_preferences(root), load_level(root), coaching)
    slugs = [r['slug'] for r in rows]
    assert 'course-schedule' not in slugs
    assert 'course-schedule-ii' in slugs
    assert slugs[0] == 'design-add-and-search-words-data-structure'


def test_resolved_priority_does_not_permanently_pin_topic(roots):
    root, _ = roots
    coaching = enable(root)
    coaching['coding']['priorities'][0]['resolved_on'] = '2026-09-13'
    rows = coding_candidates(root, '2026-09-14', load_preferences(root), load_level(root), coaching)
    assert all(r['slug'] not in ('course-schedule', 'course-schedule-ii') for r in rows)


def test_completion_is_not_readiness_and_evidence_advances(roots):
    root, parent = roots
    coaching = enable(root)
    (root / 'data/progress.json').write_text(json.dumps({'lessons': {'m1l1': {}, 'm1l4': {}}}))
    assert build_plan(root, parent, '2026-09-13').block('design').title.endswith('HTTP request and response')
    coaching['design']['evidence']['http_api'] = {'stage': 'independent', 'evidence': ''}
    assert pending_concepts(coaching)[0]['id'] == 'http_api'
    coaching['design']['evidence']['http_api']['evidence'] = 'Observed: labeled request and response unaided.'
    (root / 'profile/coaching.yaml').write_text(yaml.safe_dump(coaching))
    plan = build_plan(root, parent, '2026-09-13')
    assert plan.block('design').title.endswith('QPS and peak estimates')
    assert 'Interviewer probes deferred' in ' '.join(plan.radar)
    for concept in coaching['design']['concepts']:
        coaching['design']['evidence'][concept['id']] = {'stage': 'independent', 'evidence': 'Observed explanation.'}
    assert pending_concepts(coaching) == []


def test_tiny_budget_prepares_instead_of_overbooking_or_claiming_attempt(roots):
    root, parent = roots
    enable(root)
    before = (root / 'data/leetcode.jsonl').read_bytes() if (root / 'data/leetcode.jsonl').exists() else None
    for minutes in (10, 20, 30, 45, 60, 90, 120, 240):
        plan = build_plan(root, parent, '2026-09-13', minutes=minutes)
        assert sum(b.minutes for b in plan.blocks) <= minutes
        assert quality_issues(plan) == []
        assert all(b.id in ('design', 'leetcode') for b in plan.blocks)
    assert before is None and not (root / 'data/leetcode.jsonl').exists()


def test_calendar_capacity_used_before_task_selection(roots):
    root, parent = roots
    enable(root)
    events = [Event('afternoon', 'Busy', '13:00', 180), Event('evening', 'Busy', '19:00', 100)]
    # Explicit slots make this independent of the blank assessment fixture.
    level = load_level(root)
    level['constraints']['slots'] = [{'start': '13:00', 'minutes': 180}, {'start': '19:00', 'minutes': 120}]
    (root / 'profile/level.yaml').write_text(yaml.safe_dump(level))
    plan = build_plan(root, parent, '2026-09-13', minutes=240, events=events)
    assert plan.minutes == 20
    assert quality_issues(plan) == []
    assert sum(b.minutes for b in plan.blocks) <= 20


def test_foundation_start_opens_locally_and_in_rebuilt_hub(roots):
    from studyplan.board import serve_in_thread
    from studyplan.hub import build_site
    from studyplan.plan import save_plan
    import urllib.request
    root, parent = roots
    enable(root)
    (root / 'docs').mkdir(exist_ok=True)
    doc = root / 'docs/example-classroom-system-design-foundations.md'
    doc.write_text('# Foundation\n\n## HTTP request and response\n\nFirst edition.\n')
    plan = build_plan(root, parent, '2026-09-13', minutes=120)
    assert './docs/' in plan.block('design').first_action
    save_plan(root, plan)
    server, base = serve_in_thread(root, '2026-09-13')
    try:
        with urllib.request.urlopen(base + '/files/study-plan/docs/example-classroom-system-design-foundations.md') as r:
            assert r.status == 200 and 'First edition' in r.read().decode()
    finally:
        server.shutdown(); server.server_close()
    out = root / 'site/hub'
    build_site(root, parent, out, '2026-09-13')
    day = (out / 'day/2026-09-13.html').read_text()
    assert '../files/study-plan/docs/example-classroom-system-design-foundations.md.html#http-request-and-response' in day
    doc.write_text('# Foundation\n\nRevised lesson.\n')
    build_site(root, parent, out, '2026-09-13')
    assert 'Revised lesson' in (out / 'files/study-plan/docs/example-classroom-system-design-foundations.md.html').read_text()
