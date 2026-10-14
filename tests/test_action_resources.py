import json
import urllib.request

from studyplan.board import serve_in_thread
from studyplan.hub import build_site, render_file_page, rewrite_document_links
from studyplan.plan import Block, Item, Plan, from_dict, parse_md, render_md, save_plan, to_dict
from studyplan.resources import canonical_ref, first_target, resource_issues, resource_path


def action_plan(root):
    (root / 'docs').mkdir(exist_ok=True)
    (root / 'docs' / 'first.md').write_text('# First step\n\n[Second](second.html)\n')
    (root / 'docs' / 'second.html').write_text('<h1>Second step</h1><button onclick="this.textContent=\'Works\'">Try</button>')
    return Plan('2026-10-13', 10, [], [Block('case', 'Case', 10, '13:00',
        'Open ./docs/first.md#first-step', items=[
            Item('case-1', 'Learn (5 min)', first_action='Read the first step', action_url='./docs/first.md#first-step'),
            Item('case-2', 'Try (5 min)', first_action='Try the example', action_url='./docs/second.html')])])


def test_actions_roundtrip_and_old_plans(roots):
    root, _ = roots
    plan = action_plan(root)
    assert to_dict(parse_md(render_md(plan))) == to_dict(plan)
    assert to_dict(from_dict(json.loads(json.dumps(to_dict(plan))))) == to_dict(plan)
    old = to_dict(plan)
    for item in old['blocks'][0]['items']:
        item.pop('first_action'); item.pop('action_url')
    assert from_dict(old).blocks[0].items[0].action_url == ''


def test_plain_paths_validation_and_safe_resolution(roots):
    root, parent = roots
    plan = action_plan(root)
    assert first_target('Open `./docs/first.md#first-step` and read.') == './docs/first.md#first-step'
    assert canonical_ref('../profile/stories/a.md', action=True) == 'profile/stories/a.md'
    assert resource_issues(plan, root, parent) == []
    plan.blocks[0].items[0].action_url = './docs/first.md#wrong-heading'
    assert any('section does not exist' in x for x in resource_issues(plan, root, parent))
    plan.blocks[0].items[0].action_url = './docs/missing.md'
    assert any('missing' in x for x in resource_issues(plan, root, parent))
    for raw in ['javascript:alert(1)', '//evil.test/x', '../../../outside.md', '.env', '%2e%2e/secret.md']:
        assert resource_path(raw, root, parent) is None
    (root / 'docs' / 'escape.md').symlink_to('/etc/passwd')
    assert resource_path('study-plan/docs/escape.md', root, parent) is None
    backlink = '<a href="http://127.0.0.1:8765/?date=2026-10-13#case">Return</a>'
    assert 'href="../../../day/2026-10-13.html#case"' in rewrite_document_links(backlink, 'study-plan/docs/first.md', live=False)
    assert rewrite_document_links(backlink, 'study-plan/docs/first.md', live=True) == backlink


def test_local_and_hosted_documents_include_action_only_and_nested_resources(roots):
    root, parent = roots
    plan = action_plan(root)
    save_plan(root, plan)
    server, base = serve_in_thread(root, plan.date)
    try:
        md = urllib.request.urlopen(base + '/files/study-plan/docs/first.md').read().decode()
        assert 'href="/files/study-plan/docs/second.html"' in md
        doc = urllib.request.urlopen(base + '/files/study-plan/docs/second.html').read().decode()
        assert 'sandbox="allow-scripts allow-popups"' in doc and 'Second step' in doc
        assert 'allow-same-origin' not in doc
    finally:
        server.shutdown(); server.server_close()
    out = root / 'site' / 'hub'
    build_site(root, parent, out, plan.date)
    assert (out / 'files/study-plan/docs/first.md.html').exists()
    assert (out / 'files/study-plan/docs/second.html.html').exists()
    doc = (out / 'files/study-plan/docs/first.md.html').read_text()
    assert '../../../files/study-plan/docs/second.html.html' in doc
    page = (out / 'day/2026-10-13.html').read_text()
    assert '"study-plan/docs/first.md"' in page and '"action_url"' in page
    assert render_file_page('study-plan/../../secret.md', root, parent) is None
