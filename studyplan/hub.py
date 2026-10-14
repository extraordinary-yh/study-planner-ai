"""The study hub: a date-indexed static site rendered over the repo (design: docs/superpowers/specs/2026-09-08-study-hub-design.md).

`uv run studyplan hub build` writes `site/hub/` (gitignored):

    index.json                 one entry per date: plan call and blocks, minutes done, the job report and the
                               ledger changes that day, events going / went, interview stages, notes sections,
                               LeetCode attempts and lessons done that day
    index.html                 redirects to the current week
    month/<YYYY-MM>.html       one cell per day: minutes, dots for interviews / events, a tint for planned days
    week/<YYYY-Www>.html       seven time-axis columns: events as busy blocks, interviews fixed, study blocks
    day/<date>.html            the M1 board rendered statically (hub mode) + Job list, Artifacts, Debrief panels
    files/<repo path>.html     documents reached from actions, refs, curriculum entries and LeetCode rows,
                               copied into the site as HTML so a link works offline once the page is decrypted

Standard library only, vanilla JS, no CDN: `jobradar publish` encrypts every page for GitHub Pages, and the
decrypted page must work with no network. Unsupported repo paths (such as folders) link to GitHub.
"""
from __future__ import annotations

import base64
import mimetypes
import datetime as dt
import html
import json
import re
import posixpath
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

from . import log as logmod
from .board import GITHUB_REPO, hubwrite_js, plan_payload, render_html, reports_payload
from .config import load_curriculum, load_level
from .curriculum import flat_lessons, load_progress
from .jobs import INTERVIEW_STAGES
from .leetcode import load_attempts
from .plan import load_plan, to_minutes
from .sfdata import marked, went_yesterday
from .resources import canonical_ref, resource_path, plan_resources, TEXT_SUFFIXES, BINARY_SUFFIXES

GITHUB_API = "https://api.github.com"
REPORT_BASE = "../../archive"          # where `jobradar publish` puts the day's job report next to /hub/ on the site
EVENTS_BASE = "../../events"           # and the day's event report (sf-events/reports/<date>.html, encrypted the same way)
WINDOW_BACK, WINDOW_AHEAD = 35, 35      # days around today the site always covers (more when the data reaches further)
WEEK_FROM, WEEK_TO = 6 * 60, 24 * 60    # the week view's time axis
DEBRIEF_QUESTIONS = ("What did you do?", "What did you learn?", "What belongs in a story?")

esc = html.escape


# ---------------------------------------------------------------- dates

def week_id(day: dt.date) -> str:
    return day.strftime("%G-W%V")


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def week_days(wid: str) -> list[dt.date]:
    year, week = int(wid[:4]), int(wid[6:])
    first = dt.date.fromisocalendar(year, week, 1)
    return [first + dt.timedelta(days=i) for i in range(7)]


def month_days(month: str) -> list[dt.date]:
    year, mon = int(month[:4]), int(month[5:7])
    first = dt.date(year, mon, 1)
    nxt = dt.date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    return [first + dt.timedelta(days=i) for i in range((nxt - first).days)]


def shift_month(month: str, by: int) -> str:
    year, mon = int(month[:4]), int(month[5:7]) - 1 + by
    return f"{year + mon // 12:04d}-{mon % 12 + 1:02d}"


# ---------------------------------------------------------------- reading the repo

def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not Path(path).exists():
        return []
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def notes_sections(path: Path) -> dict[str, list[dict[str, str]]]:
    """`## <date> — <title>` sections of notes.md keyed by date, body kept as markdown."""
    out: dict[str, list[dict[str, str]]] = {}
    if not Path(path).exists():
        return out
    head = re.compile(r"^## (\d{4}-\d{2}-\d{2})(?:\s*[—–-]\s*(.*))?$")
    date, title, body = None, "", []

    def flush():
        if date:
            out.setdefault(date, []).append({"title": title, "body": "\n".join(body).strip(), "anchor": _anchor(f"{date} {title}")})
    for line in Path(path).read_text().splitlines():
        m = head.match(line)
        if m:
            flush()
            date, title, body = m.group(1), (m.group(2) or "").strip(), []
        elif date:
            body.append(line)
    flush()
    return out


def _anchor(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _seen_jobs(parent: Path) -> dict[str, dict[str, Any]]:
    path = Path(parent) / "data" / "seen.json"
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text())
    except ValueError:
        return {}
    return doc.get("jobs", doc) if isinstance(doc, dict) else {}


def ledger_by_day(parent: Path) -> dict[str, list[dict[str, Any]]]:
    """Every feedback row (verdicts, stages, referral / tailor / followup marks) on the day it was recorded."""
    seen = _seen_jobs(parent)
    out: dict[str, list[dict[str, Any]]] = {}
    for rec in _jsonl(Path(parent) / "data" / "feedback.jsonl"):
        day = str(rec.get("ts", ""))[:10]
        job_id = str(rec.get("job_id", ""))
        if not day or not job_id:
            continue
        job = seen.get(job_id, {})
        row = {"job_id": job_id, "company": job.get("company") or job_id.split(":", 1)[0].title(),
               "title": job.get("title", ""), "url": job.get("url", ""), "ts": rec.get("ts", ""),
               "kind": rec.get("kind") or "verdict", "verdict": rec.get("verdict", ""), "stage": rec.get("stage", ""),
               "note": rec.get("note", ""), "state": rec.get("state", ""),
               "folder": f"data/applications/{job_id.replace(':', '-')}"}
        row["what"] = (f"{row['kind']} {row['state']}".strip() if row["kind"] != "verdict"
                       else (f"stage → {row['stage']}" if row["stage"] and row["verdict"] == "applied" else row["verdict"]))
        row["folder_exists"] = (Path(parent) / row["folder"]).is_dir()
        out.setdefault(day, []).append(row)
    return out


def interviews_by_day(parent: Path, root: Path) -> dict[str, list[dict[str, Any]]]:
    """Interview stages on the day they were set (feedback.jsonl) and dated interviews from level.yaml."""
    out: dict[str, list[dict[str, Any]]] = {}
    seen = _seen_jobs(parent)
    for rec in _jsonl(Path(parent) / "data" / "feedback.jsonl"):
        stage = str(rec.get("stage") or "").lower()
        if rec.get("kind") or stage not in INTERVIEW_STAGES:
            continue
        day = str(rec.get("ts", ""))[:10]
        job = seen.get(str(rec.get("job_id")), {})
        out.setdefault(day, []).append({"company": job.get("company") or str(rec.get("job_id", "")).split(":")[0].title(),
                                        "stage": stage, "title": job.get("title", ""), "job_id": rec.get("job_id", ""),
                                        "dated": False, "start": "", "minutes": 0})
    for e in (load_level(root).get("goals") or {}).get("interview_dates") or []:
        try:
            day = dt.date.fromisoformat(str(e.get("date"))).isoformat()
        except (TypeError, ValueError):
            continue
        out.setdefault(day, []).append({"company": str(e.get("company", "")), "stage": str(e.get("stage", "interview")),
                                        "title": "", "job_id": "", "dated": True, "start": str(e.get("time") or ""),
                                        "minutes": int(e.get("minutes") or 60)})
    return out


def events_by_day(parent: Path) -> dict[str, list[dict[str, Any]]]:
    """sf-events items marked going / went, on the day they happen."""
    out: dict[str, list[dict[str, Any]]] = {}
    for ev in marked(parent):
        out.setdefault(str(ev["date"]), []).append({
            "id": str(ev["id"]), "title": ev.get("title", ev["id"]), "url": ev.get("url", ""), "verdict": ev["verdict"],
            "note": ev.get("verdict_note", ""), "starts_at": ev.get("starts_at") or "", "ends_at": ev.get("ends_at") or "",
            "venue": ev.get("venue", ""), "format": ev.get("format", "")})
    return out


def _hhmm(iso: str) -> str:
    try:
        t = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return t.strftime("%H:%M")


# ---------------------------------------------------------------- the index

def build_index(root: Path, parent: Path, today: str) -> dict[str, Any]:
    root, parent = Path(root), Path(parent)
    t = dt.date.fromisoformat(today)
    log_rows = logmod.load(root / "data" / "log.jsonl")
    netted = logmod.net(log_rows)
    ledger, interviews, events = ledger_by_day(parent), interviews_by_day(parent, root), events_by_day(parent)
    notes = notes_sections(root / "profile" / "notes.md")
    attempts: dict[str, list[dict[str, Any]]] = {}
    for a in load_attempts(root / "data" / "leetcode.jsonl"):
        attempts.setdefault(str(a.get("date", "")), []).append(a)
    lessons_done: dict[str, list[dict[str, Any]]] = {}
    titles = {l["id"]: l for l in flat_lessons(load_curriculum(root))}
    for lid, rec in (load_progress(root / "data" / "progress.json").get("lessons") or {}).items():
        lessons_done.setdefault(str(rec.get("done_on", "")), []).append(
            {"id": lid, "title": titles.get(lid, {}).get("title", lid), "minutes": rec.get("minutes"), "note": rec.get("note", "")})
    plan_dates = {p.stem for p in (root / "plans").glob("*.json")} | {p.stem for p in (root / "plans").glob("*.md")}
    plan_dates = {d for d in plan_dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)}

    since = min([t - dt.timedelta(days=WINDOW_BACK)] + [dt.date.fromisoformat(d) for d in plan_dates])
    until = max([t + dt.timedelta(days=WINDOW_AHEAD)] + [dt.date.fromisoformat(d) for d in plan_dates | set(events) | set(interviews)
                                                       if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)])
    dated = set(plan_dates) | set(ledger) | set(interviews) | set(events) | set(notes) | set(lessons_done) \
        | {e.get("date") for e in netted if e.get("date")} | set(attempts)
    days: dict[str, dict[str, Any]] = {}
    for d in sorted(x for x in dated if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x) and since <= dt.date.fromisoformat(x) <= until):
        plan = load_plan(root, d) if d in plan_dates else None
        payload = plan_payload(root, d) if plan is not None else None
        rows = [e for e in netted if e.get("date") == d]
        entry = {
            "date": d, "weekday": dt.date.fromisoformat(d).strftime("%a"), "week": week_id(dt.date.fromisoformat(d)),
            "plan": None if payload is None else {
                "call": payload["call"], "minutes": payload["minutes"], "arranged": payload.get("arranged", False),
                "blocks": [{"id": b["id"], "title": b["title"], "start": b["start"], "end": b["end"], "minutes": b["minutes"],
                            "done": b["done"], "items": [{"id": i["id"], "text": i["text"], "done": i["done"], "refs": i.get("refs", []), "decisions": i.get("decisions", []), "first_action": i.get("first_action", ""), "action_url": i.get("action_url", "")}
                                                          for i in b["items"]]} for b in payload["blocks"]],
                "events": [{"id": e["id"], "title": e["title"], "start": e["start"], "end": e["end"], "minutes": e["minutes"],
                            "all_day": e["all_day"], "busy": e["busy"], "source": e["source"], "url": e["url"]} for e in payload["events"]],
                "warnings": payload["warnings"]},
            "minutes_done": sum(int(e.get("minutes") or 0) for e in rows),
            "log": rows,
            "report": f"reports/{d}.html" if (parent / "reports" / f"{d}.html").exists() else None,
            "ledger": ledger.get(d, []),
            "interviews": interviews.get(d, []),
            "events": events.get(d, []),
            "debrief_due": [{"id": str(ev["id"]), "title": ev.get("title", ""), "url": ev.get("url", "")} for ev in went_yesterday(parent, d)],
            "notes": notes.get(d, []),
            "leetcode": [{"slug": a["slug"], "title": a["title"], "url": a["url"], "difficulty": a["difficulty"],
                          "outcome": a["outcome"], "minutes": a.get("minutes")} for a in attempts.get(d, [])],
            "lessons": lessons_done.get(d, []),
        }
        entry["artifacts"] = artifacts_for(entry, root)
        days[d] = entry
    return {"generated": dt.datetime.now().replace(microsecond=0).isoformat(), "today": today,
            "since": since.isoformat(), "until": until.isoformat(), "days": days}


def artifacts_for(entry: dict[str, Any], root: Path) -> list[dict[str, str]]:
    """Every file whose date is that day: plan, notes sections, stories the plan points at, LeetCode rows, lessons."""
    d = entry["date"]
    out: list[dict[str, str]] = []
    if entry["plan"] is not None:
        out.append({"label": f"plan {d}", "ref": f"study-plan/plans/{d}.md", "kind": "plan"})
        seen: set[str] = set()
        for b in entry["plan"]["blocks"]:
            for i in b["items"]:
                for r in i.get("refs", []):
                    if r.startswith("profile/stories/") and r not in seen:
                        seen.add(r)
                        out.append({"label": r.rsplit("/", 1)[-1].removesuffix(".md"), "ref": r, "kind": "story"})
    for n in entry["notes"]:
        out.append({"label": f"notes: {n['title'] or d}", "ref": f"study-plan/profile/notes.md#{n['anchor']}", "kind": "notes"})
    for lesson in entry["lessons"]:
        out.append({"label": f"lesson {lesson['id']} {lesson['title']}", "ref": f"study-plan/config/curriculum.yaml#{lesson['id']}", "kind": "lesson"})
    for a in entry["leetcode"]:
        out.append({"label": f"{a['title']} · {a['outcome']}", "ref": f"study-plan/data/leetcode.jsonl#{a['slug']}", "kind": "leetcode"})
    return out


# ---------------------------------------------------------------- markdown -> HTML (the little the repo files need)

_INLINE = [
    (re.compile(r"`([^`]+)`"), lambda m: f"<code>{m.group(1)}</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), lambda m: f"<b>{m.group(1)}</b>"),
    (re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])"), lambda m: f"<i>{m.group(1)}</i>"),
    (re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)"), lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>'),
    (re.compile(r"(?<![\"'>=])(https?://[^\s<)]+)"), lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>'),
]


def inline_md(text: str) -> str:
    out = esc(text)
    for rx, fn in _INLINE:
        out = rx.sub(fn, out)
    return out


def md_to_html(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i, para, list_tag, table = 0, [], None, []

    def close_para():
        if para:
            out.append("<p>" + " ".join(inline_md(x) for x in para) + "</p>")
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def close_table():
        if table:
            head, *body = table
            out.append("<table><thead><tr>" + "".join(f"<th>{inline_md(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{inline_md(c)}</td>" for c in r) + "</tr>" for r in body) + "</tbody></table>")
            table.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            close_para(); close_list(); close_table()
            language = line[3:].strip().lower()
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            source = esc("\n".join(lines[i + 1:j]))
            if language == "mermaid":
                out.append('<pre class="mermaid">' + source + "</pre>")
            else:
                out.append("<pre><code>" + source + "</code></pre>")
            i = j + 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            close_para(); close_list(); close_table()
            level = len(h.group(1))
            out.append(f'<h{level} id="{_anchor(h.group(2))}">{inline_md(h.group(2))}</h{level}>')
            i += 1
            continue
        if line.startswith("|") and line.rstrip().endswith("|"):
            close_para(); close_list()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                table.append(cells)
            i += 1
            continue
        close_table()
        li = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if li:
            close_para()
            tag = "ol" if li.group(2)[0].isdigit() else "ul"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            body = li.group(3)
            box = re.match(r"^\[( |x)\]\s+(.*)$", body)
            if box:
                body = ("☑ " if box.group(1) == "x" else "☐ ") + box.group(2)
            out.append(f"<li>{inline_md(body)}</li>")
            i += 1
            continue
        if line.startswith(">"):
            close_para(); close_list()
            out.append(f"<blockquote>{inline_md(line[1:].strip())}</blockquote>")
            i += 1
            continue
        if not line.strip():
            close_para(); close_list()
            i += 1
            continue
        if list_tag and line.startswith("  "):
            out[-1] = out[-1][:-5] + " " + inline_md(line.strip()) + "</li>"
            i += 1
            continue
        close_list()
        para.append(line)
        i += 1
    close_para(); close_list(); close_table()
    return "\n".join(out)


# ---------------------------------------------------------------- file pages (what refs point at)

def ref_kind(ref: str) -> str:
    if re.match(r"^https?://", ref):
        return "url"
    path, _, frag = ref.partition("#")
    if path.endswith(".md"):
        return "md"
    if path == "study-plan/config/curriculum.yaml":
        return "curriculum"
    if path == "study-plan/data/leetcode.jsonl":
        return "leetcode"
    if Path(path).suffix.lower() in TEXT_SUFFIXES | BINARY_SUFFIXES | {".html", ".htm"}:
        return "document"
    return "github"


def ref_link(ref: str, depth: int = 1) -> str:
    """Where a ref goes from a page `depth` folders below the hub root."""
    up = "../" * depth
    kind = ref_kind(ref)
    if kind == "url":
        return ref
    path, _, frag = ref.partition("#")
    if kind == "github":
        return f"{GITHUB_REPO}/tree/main/{path}"
    return f"{up}files/{path}.html" + (f"#{frag}" if frag else "")


def _resolve(ref: str, root: Path, parent: Path) -> Path:
    path = ref.partition("#")[0]
    return (root / path[len("study-plan/"):]) if path.startswith("study-plan/") else (parent / path)


def document_ref(raw: str, source: str) -> str:
    if re.match(r"^(?:https?:|mailto:|data:|#)", raw):
        return raw
    if raw.startswith("/") or re.match(r"^[a-z]+:", raw, re.I):
        return ""
    return canonical_ref(posixpath.normpath(posixpath.join(posixpath.dirname(source), raw.partition("#")[0]))
                         + ("#" + raw.partition("#")[2] if "#" in raw else ""))


def rewrite_document_links(body: str, source: str, *, live: bool) -> str:
    def replace(m):
        raw = html.unescape(m[2])
        parsed = urlsplit(raw)
        if not live and parsed.hostname in ('127.0.0.1', 'localhost') and parsed.port == 8765 and parsed.path == '/':
            date = (parse_qs(parsed.query).get('date') or [''])[0]
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
                target = '../' * (1 + source.count('/')) + 'day/' + date + '.html'
                return m[1] + '="' + esc(target + ('#' + parsed.fragment if parsed.fragment else '')) + '"'
        if re.match(r"^(?:https?:|mailto:|data:|#)", raw):
            return m[0]
        ref = document_ref(raw, source)
        if not ref:
            return m[1] + '="#"'
        target = "/files/" + ref if live else ref_link(ref, depth=1 + source.count("/"))
        return m[1] + '="' + esc(target) + '"'
    return re.sub(r'(href|src)="([^"<>]+)"', replace, body)


def render_file_page(ref: str, root: Path, parent: Path, *, live: bool = False) -> str | None:
    """The HTML body for a copied repo file, or None when the file is missing."""
    kind = ref_kind(ref)
    path = ref.partition("#")[0]
    src = resource_path(ref, root, parent)
    if src is None:
        return None
    if kind == "md":
        if not src.exists():
            return None
        body = md_to_html(src.read_text())
    elif kind == "curriculum":
        parts = []
        for m in load_curriculum(root):
            parts.append(f'<h2 id="{esc(m["id"])}">{esc(m["id"])} · {esc(m["title"])}</h2>')
            for lesson in m.get("lessons", []):
                parts.append(f'<h3 id="{esc(lesson["id"])}">{esc(lesson["id"])} · {esc(lesson["title"])} · {int(lesson.get("minutes", 45))} min</h3>'
                             f'<p><b>Read:</b> {inline_md(str(lesson.get("read", "")))}</p><p><b>Do:</b> {inline_md(str(lesson.get("do", "")))}</p>'
                             + (f'<p><b>Own system:</b> <a href="../../profile/context/{esc(lesson["own_system"])}.md">profile/context/{esc(lesson["own_system"])}.md</a></p>'
                                if lesson.get("own_system") else ""))
        body = "\n".join(parts)
    elif kind == "leetcode":
        by_slug: dict[str, list[dict[str, Any]]] = {}
        for a in load_attempts(src):
            by_slug.setdefault(a["slug"], []).append(a)
        parts = []
        for slug, rows in sorted(by_slug.items(), key=lambda kv: kv[1][-1]["date"], reverse=True):
            last = rows[-1]
            parts.append(f'<h3 id="{esc(slug)}"><a href="{esc(last["url"])}" target="_blank" rel="noopener">{esc(last["title"])}</a> '
                         f'<small>{esc(last["difficulty"])} · {esc(", ".join(last.get("topics") or []))}</small></h3>'
                         "<table><thead><tr><th>date</th><th>outcome</th><th>min</th><th>notes</th></tr></thead><tbody>"
                         + "".join(f"<tr><td>{esc(r['date'])}</td><td>{esc(r['outcome'])}</td><td>{r.get('minutes') or ''}</td><td>{esc(r.get('notes') or '')}</td></tr>" for r in rows)
                         + "</tbody></table>")
        body = "\n".join(parts) or "<p>No attempts on file.</p>"
    elif kind == "document":
        suffix = src.suffix.lower()
        if suffix in {".html", ".htm"}:
            # Opaque-origin frame: a lesson script cannot read hub tokens or write to the board.
            document = src.read_text()
            document = rewrite_document_links(document, path, live=live)
            policy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: https:; connect-src 'none'; form-action 'none'"
            document = '<meta http-equiv="Content-Security-Policy" content="' + esc(policy) + '">' + document
            document += '''<script>addEventListener('message', e => {
              if(e.source !== parent || typeof e.data.studyAnchor !== 'string') return;
              const target = document.getElementById(e.data.studyAnchor); if(target) target.scrollIntoView();
            });</script>'''
            body = '<iframe id="document-frame" title="Study document" sandbox="allow-scripts allow-popups" style="width:100%;height:80vh;border:0" srcdoc="' + esc(document) + '"></iframe>'
            body += '''<script>const frame = document.getElementById('document-frame');
              function goToSection(){let anchor;try{anchor=decodeURIComponent(location.hash.slice(1));}catch{return;}
                frame.contentWindow.postMessage({studyAnchor:anchor}, '*');}
              frame.addEventListener('load', goToSection); addEventListener('hashchange', goToSection);</script>'''
        elif suffix in BINARY_SUFFIXES:
            mime = mimetypes.guess_type(src.name)[0] or 'application/octet-stream'
            data = 'data:' + mime + ';base64,' + base64.b64encode(src.read_bytes()).decode()
            body = ('<iframe title="PDF document" style="width:100%;height:80vh" src="' + data + '"></iframe>' if suffix == '.pdf'
                    else '<img alt="' + esc(src.name) + '" style="max-width:100%" src="' + data + '">')
        else:
            body = '<pre style="white-space:pre-wrap;overflow-wrap:anywhere"><code>' + esc(src.read_text()) + '</code></pre>'
    else:
        return None
    if kind != "document" or src.suffix.lower() not in {".html", ".htm"}:
        body = rewrite_document_links(body, path, live=live)
    mermaid = ('<script type="module">import mermaid from '
               '"https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"; '
               'mermaid.initialize({startOnLoad:true, securityLevel:"strict"});</script>'
               if 'class="mermaid"' in body else "")
    return page(f"{path}", f'<p class="crumb"><a href="{"/" if live else "../" * (1 + path.count("/")) + "index.html"}">hub</a> · <code>{esc(path)}</code> · '
                f'<a href="{GITHUB_REPO}/blob/main/{esc(path)}" target="_blank" rel="noopener">on GitHub</a></p>'
                f'<article class="doc">{body}</article>{mermaid}', depth=2 + path.count("/"))


# ---------------------------------------------------------------- page chrome

CSS = """
:root { --bg:#fafafa; --fg:#1a1a1a; --muted:#6b6b6b; --card:#fff; --line:#e4e4e4; --accent:#2557d6; --good:#1b7f3b; --warn:#b26a00; --bad:#b3261e; --chip:#eef1f7; --tint:#eaf0fb; }
@media (prefers-color-scheme: dark) { :root { --bg:#111; --fg:#ececec; --muted:#9a9a9a; --card:#1b1b1b; --line:#2c2c2c; --accent:#7aa2ff; --good:#5ccf7d; --warn:#f0b35a; --bad:#ff7b72; --chip:#26292f; --tint:#1a2236; } }
:root { color-scheme: light dark } a { color:var(--accent) }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif }
main { max-width:1100px; margin:0 auto; padding:16px 16px 64px } h1 { font-size:22px; margin:8px 0 }
.nav { display:flex; flex-wrap:wrap; gap:8px; align-items:center; font-size:13px; margin:4px 0 12px }
.nav a, .nav span.here { border:1px solid var(--line); background:var(--card); border-radius:14px; padding:3px 10px; text-decoration:none }
.nav span.here { background:var(--accent); color:#fff; border-color:var(--accent) } .nav .sep { color:var(--muted) }
.crumb { color:var(--muted); font-size:13px }
.month { display:grid; grid-template-columns:repeat(7, 1fr); gap:6px } .month .dow { font-size:12px; color:var(--muted); text-align:center }
.cell { min-height:84px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:6px 8px; font-size:13px; position:relative; display:block; color:inherit; text-decoration:none }
.cell.planned { background:var(--tint) } .cell.today { outline:2px solid var(--accent) } .cell.pad { opacity:.35 } .cell.empty { cursor:default }
.cell .n { font-weight:600 } .cell .min { display:block; color:var(--good); font-weight:600 } .cell .dots { position:absolute; right:8px; top:6px; font-size:12px }
.cell .call { display:block; color:var(--muted); font-size:11px; line-height:1.3; margin-top:2px; overflow:hidden; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical }
.week { display:grid; grid-template-columns:44px repeat(7, 1fr); gap:4px } .week .head { font-size:13px; text-align:center; padding:4px 0 } .week .head a { text-decoration:none; font-weight:600 }
.week .head.today a { color:var(--accent) } .week .head .fixed { display:block; font-size:11px; color:var(--bad); font-weight:600 }
.week .head .chips { display:flex; flex-wrap:wrap; gap:3px; justify-content:center } .week .head .chip { font-size:10px; padding:0 6px; border-radius:8px; background:var(--chip); border:1px solid var(--line) }
.axis { position:relative; height:1080px; font-size:11px; color:var(--muted) } .axis div { position:absolute; right:4px; transform:translateY(-50%) }
.col { position:relative; height:1080px; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden }
.col .hr { position:absolute; left:0; right:0; border-top:1px dashed var(--line) }
.wb { position:absolute; left:2px; right:2px; border-radius:6px; padding:2px 6px; font-size:11px; line-height:1.25; overflow:hidden; border-left:3px solid var(--accent); background:var(--tint); color:inherit; text-decoration:none }
.wb.blk { left:38% } .wb.done { border-left-color:var(--good); opacity:.7 } .wb.ev { background:var(--chip); border-left-color:var(--muted); left:2px; right:63% } .wb.ev.free { border-left-style:dashed; opacity:.7 }
.wb.iv { background:color-mix(in srgb, var(--bad) 14%, var(--card)); border-left-color:var(--bad) } .wb b { display:block; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
.panels { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:12px; margin-top:20px }
.panel { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 16px; font-size:14px } .panel h2 { font-size:15px; margin:0 0 8px }
.panel ul { margin:0; padding-left:18px } .panel li { margin:3px 0 } .panel .muted { color:var(--muted); font-size:13px } .panel textarea { width:100%; min-height:60px; font:inherit; font-size:13px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); padding:6px }
.doc { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 22px; overflow-x:auto } .doc pre { background:var(--chip); padding:10px; border-radius:8px; overflow-x:auto; font-size:13px }
.doc table { border-collapse:collapse; font-size:13px } .doc th, .doc td { border:1px solid var(--line); padding:3px 8px; text-align:left } .doc h2, .doc h3 { margin-top:1.2em }
code { font-family:ui-monospace,Menlo,monospace; font-size:.92em }
"""


def page(title: str, body: str, *, depth: int = 1) -> str:
    return (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>study hub · {esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n")


def nav_html(*, month: str, week: str, day: str | None, index: dict[str, Any], depth: int = 1) -> str:
    up = "../" * depth
    days = index["days"]
    parts = [f'<a href="{up}index.html">hub</a>', '<span class="sep">·</span>']
    parts.append(f'<a href="{up}month/{shift_month(month, -1)}.html">‹</a>'
                 + (f'<span class="here">{month}</span>' if day is None and not week else f'<a href="{up}month/{month}.html">{month}</a>')
                 + f'<a href="{up}month/{shift_month(month, 1)}.html">›</a>')
    if week:
        wd = week_days(week)
        prev_w, next_w = week_id(wd[0] - dt.timedelta(days=1)), week_id(wd[-1] + dt.timedelta(days=1))
        parts.append('<span class="sep">·</span>')
        parts.append(f'<a href="{up}week/{prev_w}.html">‹</a>'
                     + (f'<span class="here">{week}</span>' if day is None else f'<a href="{up}week/{week}.html">{week}</a>')
                     + f'<a href="{up}week/{next_w}.html">›</a>')
    if day:
        d = dt.date.fromisoformat(day)
        before = [x for x in days if x < day]
        after = [x for x in days if x > day]
        parts.append('<span class="sep">·</span>')
        parts.append((f'<a href="{up}day/{before[-1]}.html">‹ {before[-1]}</a>' if before else "")
                     + f'<span class="here">{day} {d.strftime("%a")}</span>'
                     + (f'<a href="{up}day/{after[0]}.html">{after[0]} ›</a>' if after else ""))
    parts.append(f'<span class="sep">·</span><a href="{up}day/{index["today"]}.html">today</a>' if index["today"] in days else "")
    return '<div class="nav">' + " ".join(p for p in parts if p) + "</div>"


# ---------------------------------------------------------------- the views

def render_month(month: str, index: dict[str, Any]) -> str:
    days = index["days"]
    cells = ['<div class="month">'] + [f'<div class="dow">{d}</div>' for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    first = month_days(month)[0]
    for _ in range(first.weekday()):
        cells.append('<span class="cell pad empty"></span>')
    for d in month_days(month):
        iso = d.isoformat()
        e = days.get(iso)
        cls = "cell" + (" planned" if e and e["plan"] else "") + (" today" if iso == index["today"] else "") + ("" if e else " empty")
        dots = ""
        if e:
            dots = ("<span title='interview'>●</span>" * len(e["interviews"])) + ("<span title='event'>◆</span>" * len(e["events"]))
        inner = f'<span class="n">{d.day}</span><span class="dots">{dots}</span>'
        if e:
            if e["minutes_done"]:
                inner += f'<span class="min">{e["minutes_done"]} min</span>'
            if e["plan"] and e["plan"]["call"] and not e["plan"]["call"].startswith("_("):
                inner += f'<span class="call">{esc(e["plan"]["call"])}</span>'
            cells.append(f'<a class="{cls}" href="../day/{iso}.html" id="d-{iso}">{inner}</a>')
        else:
            cells.append(f'<span class="{cls}" id="d-{iso}">{inner}</span>')
    cells.append("</div>")
    body = nav_html(month=month, week="", day=None, index=index) + f"<h1>{month}</h1>" + "\n".join(cells)
    return page(month, body)


def _wb(top: int, bottom: int, cls: str, title: str, sub: str, href: str = "") -> str:
    top, bottom = max(top, WEEK_FROM), min(bottom, WEEK_TO)
    if bottom <= top:
        return ""
    style = f"top:{top - WEEK_FROM}px;height:{max(bottom - top, 14)}px"
    inner = f"<b>{esc(title)}</b>{esc(sub)}"
    if href:
        return f'<a class="wb {cls}" style="{style}" href="{href}" title="{esc(title)}">{inner}</a>'
    return f'<div class="wb {cls}" style="{style}" title="{esc(title)}">{inner}</div>'


def render_week(wid: str, index: dict[str, Any]) -> str:
    days = index["days"]
    wd = week_days(wid)
    grid = ['<div class="week"><div></div>']
    for d in wd:
        iso = d.isoformat()
        e = days.get(iso)
        label = f'{d.strftime("%a")} {d.day}'
        head = f'<a href="../day/{iso}.html">{label}</a>' if e else label
        fixed = "".join(f'<span class="fixed">● {esc(iv["company"])} {esc(iv["stage"])}</span>' for iv in (e["interviews"] if e else []) if not iv["start"])
        chips = "".join(f'<span class="chip">{esc(ev["title"])}</span>' for ev in (e["plan"]["events"] if e and e["plan"] else []) if ev["all_day"])
        grid.append(f'<div class="head{" today" if iso == index["today"] else ""}">{head}{fixed}<span class="chips">{chips}</span></div>')
    grid.append('<div class="axis">' + "".join(f'<div style="top:{(h * 60 - WEEK_FROM)}px">{h:02d}</div>' for h in range(WEEK_FROM // 60, 24)) + "</div>")
    for d in wd:
        iso = d.isoformat()
        e = days.get(iso)
        col = [f'<div class="col" id="col-{iso}">'] + [f'<div class="hr" style="top:{h * 60 - WEEK_FROM}px"></div>' for h in range(WEEK_FROM // 60 + 1, 24)]
        if e:
            plan_ev_ids = set()
            if e["plan"]:
                for ev in e["plan"]["events"]:
                    if ev["all_day"]:
                        continue
                    plan_ev_ids.add(ev["id"])
                    col.append(_wb(to_minutes(ev["start"]), to_minutes(ev["start"]) + ev["minutes"], "ev" + ("" if ev["busy"] else " free"),
                                   ev["title"], f'{ev["start"]}–{ev["end"]}', ev["url"]))
                for b in e["plan"]["blocks"]:
                    if b["start"]:
                        col.append(_wb(to_minutes(b["start"]), to_minutes(b["start"]) + b["minutes"], "blk done" if b["done"] else "blk",
                                       b["title"], f'{b["start"]}–{b["end"]} · {b["minutes"]} min', f"../day/{iso}.html#blk-{b['id']}"))
            for ev in e["events"]:                                  # going / went without a plan that day
                start = _hhmm(ev["starts_at"])
                if not start or (start == "00:00" and not ev["ends_at"]):
                    continue
                end = _hhmm(ev["ends_at"]) if ev["ends_at"] else ""
                lo = to_minutes(start)
                hi = to_minutes(end) if end else lo + 120
                if not e["plan"] or not any(x["title"] == ev["title"] for x in e["plan"]["events"]):
                    col.append(_wb(lo, hi, "ev", ev["title"], f"{start}–{end or '?'} · {ev['verdict']}", ev["url"]))
            for iv in e["interviews"]:
                if iv["start"]:
                    lo = to_minutes(iv["start"])
                    col.append(_wb(lo, lo + iv["minutes"], "iv", f'{iv["company"]} {iv["stage"]}', iv["start"]))
        col.append("</div>")
        grid.append("\n".join(col))
    grid.append("</div>")
    month = wd[0].strftime("%Y-%m")
    total = sum(days[d.isoformat()]["minutes_done"] for d in wd if d.isoformat() in days)
    body = (nav_html(month=month, week=wid, day=None, index=index) + f"<h1>{wid} · {wd[0].strftime('%b %-d')} – {wd[-1].strftime('%b %-d')}"
            + f' <small class="crumb">{total} min done</small></h1>' + "\n".join(grid))
    return page(wid, body)


def panels_html(entry: dict[str, Any], *, files: dict[str, str], report_base: str = REPORT_BASE, debrief_form: str = "") -> str:
    d = entry["date"]
    # job list
    job = []
    if entry["report"]:
        job.append(f'<p><a href="{report_base}/{d}.html">Open the job report for {d}</a> <span class="muted">(the encrypted copy next to this site)</span></p>')
    else:
        job.append(f'<p class="muted">No job report was rendered on {d}.</p>')
    if entry["ledger"]:
        job.append("<ul>")
        for r in entry["ledger"]:
            links = []
            if r["url"]:
                links.append(f'<a href="{esc(r["url"])}" target="_blank" rel="noopener">posting</a>')
            if r["folder_exists"]:
                links.append(f'<a href="{GITHUB_REPO}/tree/main/{esc(r["folder"])}" target="_blank" rel="noopener">application folder</a>')
            job.append(f'<li><b>{esc(r["company"])}</b> {esc(r["title"])} · {esc(r["what"])}'
                       + (f' <span class="muted">{esc(r["note"])}</span>' if r["note"] else "")
                       + (" · " + " · ".join(links) if links else "") + "</li>")
        job.append("</ul>")
    else:
        job.append('<p class="muted">No ledger changes that day.</p>')
    for iv in entry["interviews"]:
        job.append(f'<p>● <b>{esc(iv["company"])}</b> {esc(iv["stage"])}' + (f' at {iv["start"]}' if iv["start"] else " (stage set this day)") + "</p>")
    # artifacts
    art = []
    if entry["artifacts"]:
        art.append("<ul>")
        for a in entry["artifacts"]:
            href = files.get(a["ref"]) or ref_link(a["ref"])
            art.append(f'<li><a href="{esc(href)}"{" target=_blank rel=noopener" if ref_kind(a["ref"]) in ("url", "github") else ""}>{esc(a["label"])}</a> <span class="muted">{esc(a["kind"])}</span></li>')
        art.append("</ul>")
    else:
        art.append('<p class="muted">Nothing dated that day yet.</p>')
    for ev in entry["events"]:
        art.append(f'<p>◆ <a href="{esc(ev["url"])}" target="_blank" rel="noopener">{esc(ev["title"])}</a> · {esc(ev["verdict"])}'
                   + (f' <span class="muted">{esc(ev["note"])}</span>' if ev["note"] else "") + "</p>")
    # debrief
    deb = []
    for ev in entry.get("debrief_due", []):
        deb.append(f'<p>◆ Debrief due: <a href="{esc(ev["url"])}" target="_blank" rel="noopener">{esc(ev["title"])}</a> (yesterday)</p>')
    debriefs = [n for n in entry["notes"] if "debrief" in n["title"].lower()]
    for n in debriefs:
        deb.append(f'<h3>{esc(n["title"])}</h3>' + md_to_html(n["body"]))
    if debrief_form:
        deb.append(debrief_form)
    elif not debriefs:
        deb.append('<p class="muted">' + " · ".join(esc(q) for q in DEBRIEF_QUESTIONS) + "</p>"
                   '<p class="muted">Read-only here: tell the coach in chat, or write a <code>## ' + d + ' — debrief</code> section in '
                   '<code>study-plan/profile/notes.md</code>. The hosted form arrives with the write path.</p>')
    return ('<div class="panels" id="panels">'
            f'<section class="panel" id="panel-jobs"><h2>Job list</h2>{"".join(job)}</section>'
            f'<section class="panel" id="panel-artifacts"><h2>Artifacts</h2>{"".join(art)}</section>'
            f'<section class="panel" id="panel-debrief"><h2>Debrief</h2>{"".join(deb)}</section></div>')


def debrief_form_html(entry: dict[str, Any]) -> str:
    """The hosted Debrief form: an optional event, the three questions; the page posts it through the writer."""
    d = entry["date"]
    due = [ev["title"] for ev in entry.get("debrief_due", [])]
    events = due + [ev["title"] for ev in entry["events"] if ev["title"] not in due]
    options = "".join(f'<option value="{esc(t)}"{" selected" if t in due else ""}>{esc(t)}{" (yesterday)" if t in due else ""}</option>'
                      for t in events)
    select = (f'<label>Event <select name="event"><option value="">the day</option>{options}</select></label>' if events
              else '<input type="hidden" name="event" value="">')
    fields = "".join(f'<label>{esc(q)}<textarea name="{n}" rows="2"></textarea></label>'
                     for q, n in zip(DEBRIEF_QUESTIONS, ("did", "learned", "story")))
    return (f'<form id="debrief-form">{select}{fields}<button type="submit">Commit the debrief</button> '
            f'<span class="muted">→ <code>## {esc(d)} — debrief</code> in notes.md</span></form>')


def render_day(entry: dict[str, Any], index: dict[str, Any], root: Path, parent: Path, *, files: dict[str, str],
               report_base: str = REPORT_BASE, events_base: str = EVENTS_BASE, debrief_form: str = "",
               write: dict[str, Any] | None = None) -> str:
    d = entry["date"]
    dd = dt.date.fromisoformat(d)
    nav = nav_html(month=dd.strftime("%Y-%m"), week=week_id(dd), day=d, index=index)
    if write and entry["plan"] is not None and not debrief_form:
        debrief_form = debrief_form_html(entry)
    panels = panels_html(entry, files=files, report_base=report_base, debrief_form=debrief_form)
    if entry["plan"] is not None:
        payload = plan_payload(root, d)
        days = list(index["days"])
        before = [x for x in days if x < d]
        after = [x for x in days if x > d]
        hub = {"date": d, "today": index["today"], "files": files, "repo": GITHUB_REPO.split("github.com/")[1],
               "prev": f"{before[-1]}.html" if before else "", "next": f"{after[0]}.html" if after else "",
               "today_href": f"{index['today']}.html" if index["today"] in index["days"] else ""}
        if write:
            hub["write"] = write
        reports = reports_payload(parent, d, {"jobs": report_base + "/{date}.html", "events": events_base + "/{date}.html"})
        return render_html(payload, hub, reports=reports, nav=nav, panels=panels,
                           scripts=f"<script>\n{hubwrite_js()}\n</script>" if write else "")
    body = nav + f"<h1>{d} ({dd.strftime('%a')})</h1><p class='crumb'>No study plan that day.</p>" + panels
    return page(d, body)


# ---------------------------------------------------------------- the build

INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>study hub</title>
<meta name="robots" content="noindex"></head>
<body><p>Opening this week…</p>
<script>
(function(){
  var weeks = __WEEKS__;
  var d = new Date(), t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  var day = t.getUTCDay() || 7; t.setUTCDate(t.getUTCDate() + 4 - day);
  var y = t.getUTCFullYear(), w = Math.ceil((((t - Date.UTC(y, 0, 1)) / 86400000) + 1) / 7);
  var id = y + "-W" + String(w).padStart(2, "0");
  if (weeks.indexOf(id) < 0) { var past = weeks.filter(function(x){ return x <= id; }); id = past.length ? past[past.length - 1] : weeks[0]; }
  location.replace("week/" + id + ".html");
})();
</script>
<noscript><a href="week/__LATEST__.html">this week</a></noscript>
</body></html>
"""


def build_site(root: Path, parent: Path, out: Path, today: str, *, report_base: str = REPORT_BASE,
               events_base: str = EVENTS_BASE, debrief_form: str = "", write: bool = True,
               api: str = GITHUB_API) -> dict[str, Any]:
    """Render everything. `write=False` builds a read-only copy; `api` points the hosted writes elsewhere (tests)."""
    root, parent, out = Path(root), Path(parent), Path(out)
    write_cfg = {"repo": GITHUB_REPO.split("github.com/")[1], "branch": "main", "api": api} if write else None
    index = build_index(root, parent, today)
    days = index["days"]
    for sub in ("month", "week", "day", "files"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n")

    # every ref the plans and artifacts point at -> a copied page (or a GitHub / external link)
    refs: set[str] = set()
    for e in days.values():
        for a in e["artifacts"]:
            refs.add(a["ref"])
        if e["plan"]:
            for b in e["plan"]["blocks"]:
                for i in b["items"]:
                    refs.update(i.get("refs", []))
    for date in days:
        plan = load_plan(root, date)
        if plan:
            refs.update(plan_resources(plan))
            from .session import load_lessons
            for lesson in load_lessons(root, date).get('steps', {}).values():
                refs.update(canonical_ref(r['url']) for r in lesson.get('resources', []))
    # Copy documents reached from required resources too, so nested reading links work offline.
    pending = list(refs)
    inspected = set()
    while pending:
        ref = pending.pop()
        path = ref.partition("#")[0]
        if path in inspected:
            continue
        inspected.add(path)
        src = resource_path(ref, root, parent)
        if src is None or src.suffix.lower() not in {'.md', '.html', '.htm'}:
            continue
        raw = src.read_text()
        links = re.findall(r'\]\(([^)\s]+)\)', raw) + re.findall(r'(?:href|src)=["\']([^"\']+)["\']', raw)
        for link in links:
            target = document_ref(link, path)
            if target and resource_path(target, root, parent) is not None and target not in refs:
                refs.add(target); pending.append(target)
    files: dict[str, str] = {}
    copied = 0
    rendered_paths: set[str] = set()
    for ref in sorted(refs):
        kind = ref_kind(ref)
        if kind in ("url", "github"):
            files[ref] = ref_link(ref)
            continue
        path = ref.partition("#")[0]
        target = out / "files" / (path + ".html")
        if path not in rendered_paths:
            body = render_file_page(ref, root, parent)
            if body is None:
                files[ref] = ref_link(f"{path}#github") if False else f"{GITHUB_REPO}/blob/main/{path}"
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            rendered_paths.add(path)
            copied += 1
        files[ref] = ref_link(ref)
        files.setdefault(path, ref_link(path))

    since, until = dt.date.fromisoformat(index["since"]), dt.date.fromisoformat(index["until"])
    months = sorted({d.strftime("%Y-%m") for d in (since + dt.timedelta(days=i) for i in range((until - since).days + 1))})
    weeks = sorted({week_id(d) for d in (since + dt.timedelta(days=i) for i in range((until - since).days + 1))})
    for m in months:
        (out / "month" / f"{m}.html").write_text(render_month(m, index))
    for w in weeks:
        (out / "week" / f"{w}.html").write_text(render_week(w, index))
    for d, e in days.items():
        (out / "day" / f"{d}.html").write_text(render_day(e, index, root, parent, files=files, report_base=report_base,
                                                          events_base=events_base, debrief_form=debrief_form, write=write_cfg))
        from .session import load_lessons, session_payload, render_session
        lessons = load_lessons(root, d)
        if lessons and e['plan']:
            folder = out / 'sessions' / d
            folder.mkdir(parents=True, exist_ok=True)
            for block in e['plan']['blocks']:
                for item in block['items']:
                    if item['id'] in lessons.get('steps', {}):
                        if not re.fullmatch(r'[A-Za-z0-9_-]+', item['id']):
                            raise ValueError('Unsafe session item id')
                        payload = session_payload(root, d, block['id'], item['id'], live=False)
                        (folder / (item['id'] + '.html')).write_text(render_session(payload))
    current = week_id(dt.date.fromisoformat(today))
    (out / "index.html").write_text(INDEX_HTML.replace("__WEEKS__", json.dumps(weeks)).replace("__LATEST__", current if current in weeks else weeks[-1]))
    return {"out": str(out), "days": len(days), "months": len(months), "weeks": len(weeks), "files": copied,
            "planned": sum(1 for e in days.values() if e["plan"]), "current_week": current}
