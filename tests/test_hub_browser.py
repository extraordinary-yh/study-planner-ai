"""Playwright checks of the hub site: the week and day pages open from disk with no script error, a ref link
reaches the copied file, two temporary screenshots are written, and the hosted write path ticks an item and
commits a debrief against a stubbed GitHub Contents API (an `http.server` with CORS on the loopback).

Marked `browser`; skipped wherever Chromium cannot launch. Run alone with `uv run pytest -m browser`.
"""
import base64
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from studyplan import log as logmod
from studyplan.busy import calendar_events
from studyplan.hub import build_site
from studyplan.plan import build_plan, save_plan

pytestmark = pytest.mark.browser

HERE = Path(__file__).resolve().parent
DOCS = HERE.parents[0] / "build" / "test-artifacts"


class FakeGitHub:
    """Enough of the Contents API for the page: GET returns content + sha, PUT checks the sha and records the body."""

    def __init__(self, files: dict[str, str]):
        self.files = {k: {"text": v, "sha": f"sha-{i}"} for i, (k, v) in enumerate(files.items())}
        self.puts: list[dict] = []
        self.fail_next_put = 0
        store = self

        class Handler(BaseHTTPRequestHandler):
            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "authorization, content-type, accept, x-github-api-version")
                self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _path(self):
                return unquote(urlparse(self.path).path.split("/contents/", 1)[1])

            def do_OPTIONS(self):  # noqa: N802
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):  # noqa: N802
                f = store.files.get(self._path())
                if not f:
                    return self._json(404, {"message": "Not Found"})
                self._json(200, {"content": base64.b64encode(f["text"].encode()).decode(), "sha": f["sha"]})

            def do_PUT(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                path = self._path()
                body["path"], body["authorization"] = path, self.headers.get("Authorization", "")
                store.puts.append(body)
                if store.fail_next_put:
                    store.fail_next_put -= 1
                    return self._json(409, {"message": "sha conflict (stub)"})
                cur = store.files.get(path)
                if cur and body.get("sha") != cur["sha"]:
                    return self._json(422, {"message": "stale sha"})
                store.files[path] = {"text": base64.b64decode(body["content"]).decode(), "sha": f"sha-{len(store.puts) + 100}"}
                self._json(200, {"commit": {"sha": f"c{len(store.puts)}"}, "content": {"sha": store.files[path]["sha"]}})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site(roots):
    root, parent = roots
    shutil.copy(HERE / "fixtures" / "notes.md", root / "profile" / "notes.md")
    events, _ = calendar_events(root, parent, "2026-09-07", urls=[str(HERE / "fixtures" / "ical" / "calendar.ics")])
    plan = build_plan(root, parent, "2026-09-07", events=events)
    plan.call = "Kill the binary-search freeze first: it is the one concrete failure you named, and it is a 30-minute fix."
    save_plan(root, plan)
    save_plan(root, build_plan(root, parent, "2026-09-08"))
    logmod.append(root / "data" / "log.jsonl", {"ts": "2026-09-07T20:00:00", "date": "2026-09-07", "block": "leetcode",
                                                 "minutes": 40, "target": "leetcode", "source": "board"})
    github = FakeGitHub({"study-plan/data/log.jsonl": (root / "data" / "log.jsonl").read_text(),
                         "study-plan/plans/2026-09-07.json": (root / "plans" / "2026-09-07.json").read_text(),
                         "study-plan/profile/notes.md": (root / "profile" / "notes.md").read_text()})
    out = root / "site" / "hub"
    build_site(root, parent, out, "2026-09-08", api=github.url)
    yield out, github, plan
    github.close()


def test_week_and_day_pages_open_from_disk(site, browser):
    out, github, plan = site
    errors = []
    page = browser.new_page(viewport={"width": 1280, "height": 1000})
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto((out / "week" / "2026-W37.html").as_uri(), wait_until="load")
    assert page.locator("#col-2026-09-07 .wb.blk").count() >= 5 and page.locator("#col-2026-09-07 .wb.ev").count() >= 3
    assert page.locator("#col-2026-09-12 .wb.ev").count() == 1                   # the hackathon the user is going to
    ev_box = page.locator("#col-2026-09-07 .wb.ev").first.bounding_box()
    blk_box = page.locator("#col-2026-09-07 .wb.blk").first.bounding_box()
    assert ev_box["x"] + ev_box["width"] <= blk_box["x"] + 1                      # events left, study right
    DOCS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(DOCS / "hub-week.png"), full_page=False)

    page.click("#col-2026-09-07 .wb.blk >> nth=0")                                # the week links into the day
    page.wait_for_load_state("load")
    assert page.url.endswith("/day/2026-09-07.html#blk-news")
    assert "Kill the binary-search freeze" in page.inner_text("#call")
    assert "sel" in page.get_attribute("#blk-news", "class")                     # the hash selected the block in the pane
    assert page.locator("#hosted-note").is_visible() and not page.locator("#readonly-note").is_visible()
    assert page.locator("#tab-study input[type=checkbox]").first.is_enabled()   # the hosted copy can write
    assert page.locator("#panel-jobs").count() == 1 and "debrief: Agents meetup" in page.inner_text("#panel-debrief")
    assert page.locator("#now").count() == 0                                      # not today: no now-line
    assert "hosted copy" in page.inner_text("#status")
    assert page.locator(".strip").count() >= 1                                    # the compact timeline, statically too
    # the two report tabs point next to the hub on the published site
    page.click("#tab-b-jobs")
    assert "Latest report: Sat 5 Sep" in page.inner_text("#jobs-head")             # the fixture has 09-05 only: fallback
    assert page.get_attribute("#frame-jobs", "src") == "../../archive/2026-09-05.html"
    page.click("#tab-b-events")
    assert "No event report was rendered on 2026-09-07" in page.inner_text("#events-head")
    assert page.locator("#frame-events").count() == 0 and "sf-events/reports" in page.get_attribute("#tab-events .none a", "href")
    page.click("#tab-b-study")
    page.evaluate("() => document.querySelector('#panels').scrollIntoView({block: 'end'})")
    page.wait_for_timeout(150)
    page.screenshot(path=str(DOCS / "hub-day.png"), full_page=False)

    story = page.locator("#panel-artifacts a").first
    href = story.get_attribute("href")
    assert href.startswith("../files/") and href.endswith(".md.html")
    story.click()
    page.wait_for_load_state("load")
    assert "/files/" in page.url and page.locator("article.doc").count() == 1    # the copied file opened offline
    assert errors == [], errors
    assert (DOCS / "hub-week.png").stat().st_size > 5000 and (DOCS / "hub-day.png").stat().st_size > 5000
    page.close()


def test_hosted_page_writes_through_the_github_api(site, browser):
    out, github, plan = site
    item = plan.blocks[0].items[0].id
    errors = []
    page = browser.new_page(viewport={"width": 1280, "height": 1000})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto((out / "day" / "2026-09-07.html").as_uri(), wait_until="load")

    # without a token a tick reverts and opens the drawer
    page.click("#blk-news")
    page.wait_for_selector("#it-" + item)
    page.click("#it-" + item)                                                     # reverts at once: no token yet
    page.wait_for_selector("#settings[open]")
    page.wait_for_function("() => !document.querySelector('#it-%s').checked" % item)
    assert github.puts == []

    # the drawer stores the token in localStorage only
    page.fill("#token", "github_pat_TEST")
    page.click("#token-save")
    assert page.evaluate("() => localStorage.getItem('hub.token')") == "github_pat_TEST"
    assert "· set" in page.inner_text("#token-state")
    assert "github_pat_TEST" not in page.content()                                # never in the page

    # tick an item: optimistic, then one commit appending a log row
    page.check("#it-" + item)
    page.wait_for_selector("#writes:not([hidden])")
    assert page.is_checked("#it-" + item)
    assert len(github.puts) == 1
    put = github.puts[0]
    assert put["path"] == "study-plan/data/log.jsonl" and put["authorization"] == "Bearer github_pat_TEST"
    assert put["sha"] == "sha-0" and put["branch"] == "main" and put["message"] == f"hub: 2026-09-07 tick {item}"
    text = base64.b64decode(put["content"]).decode()
    assert text.startswith('{"ts": "2026-09-07T20:00:00"')                       # the old row kept
    row = json.loads(text.strip().splitlines()[-1])
    assert row["target"] == item and row["item"] == item and row["block"] == "news" and row["source"] == "hub"
    assert "re-renders in ~2 min" in page.inner_text("#writes") and item in page.inner_text("#writes")

    # a conflict is retried once, transparently
    github.fail_next_put = 1
    page.uncheck("#it-" + item)
    page.wait_for_function("() => document.querySelectorAll('#writes > div').length === 2")
    assert [p["message"] for p in github.puts[1:]] == [f"hub: 2026-09-07 undo {item}"] * 2
    assert not page.is_checked("#it-" + item)

    # a hard failure reverts the tick and shows the API's message
    github.fail_next_put = 2
    page.click("#it-" + item)
    page.wait_for_selector("#toast.bad")
    assert "sha conflict (stub)" in page.inner_text("#toast")
    page.wait_for_function("() => !document.querySelector('#it-%s').checked" % item)

    # the debrief form writes a dated section to notes.md
    page.fill("textarea[name=did]", "talked to two founders")
    page.fill("textarea[name=learned]", "nobody has evals")
    page.fill("textarea[name=story]", "the BGM quality gate")
    page.click("#debrief-form button[type=submit]")
    page.wait_for_selector("#debrief-written")
    notes = base64.b64decode(github.puts[-1]["content"]).decode()
    assert github.puts[-1]["path"] == "study-plan/profile/notes.md" and github.puts[-1]["message"] == "hub: 2026-09-07 debrief"
    assert "## 2026-09-07 — debrief\n\n**What did you do?** talked to two founders" in notes
    assert notes.index("## 2026-09-07 — debrief\n") < notes.index("## 2026-09-07 — debrief: Agents meetup")   # newest first

    # forgetting the token
    page.click("#token-forget")
    assert page.evaluate("() => localStorage.getItem('hub.token')") is None
    assert errors == [], errors
    page.close()
