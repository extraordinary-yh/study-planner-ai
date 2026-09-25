"""The hosted write path: the JS module's requests (under Node), and how the day page carries it."""
import shutil
import subprocess
from pathlib import Path

import pytest

from studyplan.board import hubwrite_js
from studyplan.hub import build_site, debrief_form_html
from studyplan.plan import build_plan, save_plan

HERE = Path(__file__).resolve().parent
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_request_construction_under_node():
    out = subprocess.run([NODE, str(HERE / "hubwrite.test.mjs")], capture_output=True, text=True, cwd=str(HERE.parent))
    assert out.returncode == 0, out.stderr
    assert "hubwrite: ok" in out.stdout


def test_module_never_touches_storage_or_logs():
    import re
    js = re.sub(r"/\*.*?\*/", "", hubwrite_js(), flags=re.S)                     # code only, not the header comment
    assert "localStorage" not in js and "console." not in js and "document." not in js
    assert "X-GitHub-Api-Version" in js and '"hub: " + o.date' in js


def test_day_page_inlines_the_writer_and_the_form(roots):
    root, parent = roots
    save_plan(root, build_plan(root, parent, "2026-09-07"))
    out = root / "site" / "hub"
    build_site(root, parent, out, "2026-09-08", api="http://127.0.0.1:1/api")
    day = (out / "day" / "2026-09-07.html").read_text()
    assert "HubWriter.prototype.tick" in day                                  # the module is inlined, no CDN
    assert '"write": {"repo": "example-user/job-radar", "branch": "main", "api": "http://127.0.0.1:1/api"}' in day
    assert 'id="settings"' in day and 'id="token"' in day and "Contents: Read and write" in day
    assert 'id="debrief-form"' in day and 'name="did"' in day and 'name="story"' in day
    assert "github_pat" not in day.replace("github_pat_…", "")                 # no token anywhere in the payload
    assert 'src="http' not in day and "cdn" not in day.lower()

    build_site(root, parent, out, "2026-09-08", write=False)
    day = (out / "day" / "2026-09-07.html").read_text()
    assert "HubWriter.prototype.tick" not in day and '"write"' not in day and 'id="debrief-form"' not in day


def test_debrief_form_lists_the_day_s_events():
    entry = {"date": "2026-09-07", "events": [{"title": "Agents meetup"}, {"title": "Hack night"}]}
    html = debrief_form_html(entry)
    assert '<option value="Agents meetup">' in html and 'value="">the day</option>' in html
    assert debrief_form_html({"date": "2026-09-07", "events": []}).count("<select") == 0
