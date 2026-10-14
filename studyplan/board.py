"""The day board: a local, loopback-only HTTP server that renders the day's plan as a timeline and writes back.

Standard library only, no build step. `uv run studyplan serve` starts it; the page is one HTML file with inline
CSS and vanilla JS (two small modules, `static/timeline.js` and, in hub mode, `static/hubwrite.js`, are inlined
into it). Every write goes through the structured plan (`plans/<date>.json`) and re-renders `plans/<date>.md`,
so the repo stays the source of truth and `plan --force` is never needed.

The page has two panes (design: docs/superpowers/specs/2026-09-08-board-ui-design.md): a compact timeline on the
left, where empty stretches of the day collapse to a strip, and a detail pane on the right with Study, Jobs and
Events tabs. The same template renders in three modes: `live` (this server), `static` (the hub's read-only copy)
and `github` (the hub's copy that writes through the GitHub API).

API (all loopback):
    GET  /api/plan?date=YYYY-MM-DD   the plan, with a `gcal` link per block
    GET  /api/log?date=YYYY-MM-DD    the day's log rows (raw, append-only)
    POST /api/done                   {date, block, item?, minutes?, note?}  -> tick, append a log row
    POST /api/undo                   {date, block, item?}                   -> untick, append an `undo` row
    POST /api/block                  {date, block, start?, minutes?, title?} -> move/resize/rename, snapped to 5 min
    GET  /api/busy?date=YYYY-MM-DD   re-fetch the iCal feeds and sf-events; the plan, plus `changed`
    GET  /reports/jobs/<date>.html   the job report rendered that day (read-only; `../reports/`)
    GET  /reports/events/<date>.html the event report rendered that day (read-only; `../sf-events/reports/`)
The two file routes are allowlisted to exactly those two folders and the `YYYY-MM-DD.html` name: every other
path under /reports/ is a 404.
"""
from __future__ import annotations

import datetime as dt
import html as htmlmod
import ipaddress
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import busy as busymod
from . import log as logmod
from .config import parent_root
from .plan import Plan, apply_log, gcal_url, load_plan, save_plan, snap, to_dict, to_minutes

MAX_BODY = 1 << 20
GITHUB_REPO = "https://github.com/example-user/job-radar"  # Optional legacy hosted integration; use the local board by default.
REPORT_DIRS = {"jobs": ("reports",), "events": ("sf-events", "reports")}      # the two folders the page may read
REPORT_FOLDERS = {"jobs": f"{GITHUB_REPO}/tree/main/reports", "events": f"{GITHUB_REPO}/tree/main/sf-events/reports"}
LIVE_REPORT_BASES = {"jobs": "/reports/jobs/{date}.html", "events": "/reports/events/{date}.html"}
_REPORT_ROUTE = re.compile(r"^/reports/(jobs|events)/(\d{4}-\d{2}-\d{2})\.html$")
_FILE_ROUTE = re.compile(r"^/files/(.+)$")
_LEGACY_PROFILE_ROUTE = re.compile(r"^/(profile/(?:notes\.md|stories/[^/]+\.md|context/[^/]+\.md))$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BoardError(Exception):
    """A bad request from the page; reported as a 400 with a message."""


# ---------------------------------------------------------------- the data the page gets

def plan_payload(root: Path, date: str) -> dict[str, Any]:
    from .session import load_lessons
    plan = load_plan(root, date)
    if plan is None:
        raise BoardError(f"no plan for {date}: run `uv run studyplan plan --date {date}`")
    apply_log(plan, logmod.done_targets(logmod.load(root / "data" / "log.jsonl"), date))   # hosted ticks live in the log
    data = to_dict(plan)
    focused = load_lessons(root, date).get('steps', {})
    for block, payload in zip(plan.blocks, data["blocks"]):
        payload["gcal"] = gcal_url(date, block)
        payload["end"] = block.end
        for item in payload['items']:
            if item['id'] in focused:
                item['session'] = True
    for event, payload in zip(plan.events, data["events"]):
        payload["end"] = event.end
    return data


def log_rows(root: Path, date: str) -> list[dict[str, Any]]:
    return [e for e in logmod.load(root / "data" / "log.jsonl") if e.get("date") == date]


def report_dir(parent: Path, kind: str) -> Path:
    return Path(parent).joinpath(*REPORT_DIRS[kind])


def report_for(folder: Path, date: str) -> str | None:
    """The date of the report to show for `date`: the report rendered that day when it exists, otherwise the newest
    one dated on or before it (the event scan does not run every day), otherwise None."""
    folder = Path(folder)
    if (folder / f"{date}.html").exists():
        return date
    dates = sorted(p.stem for p in folder.glob("*.html") if _DATE.match(p.stem) and p.stem <= date)
    return dates[-1] if dates else None


def reports_payload(parent: Path, date: str, bases: dict[str, str] | None = None) -> dict[str, Any]:
    """What the Jobs and Events tabs need: per kind the date shown and the URL of that file (None when there is
    none on or before the day), plus the report folders on GitHub for the empty state. `bases` are the URL
    templates (`{date}` is filled in); the live server's routes by default, the published site's `archive/` and
    `events/` folders for the hub."""
    bases = bases or LIVE_REPORT_BASES
    out: dict[str, Any] = {}
    for kind in REPORT_DIRS:
        found = report_for(report_dir(parent, kind), date)
        out[kind] = {"date": found, "url": bases[kind].format(date=found)} if found else None
    out["folders"] = dict(REPORT_FOLDERS)
    return out


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _find(plan: Plan, block_id: str, item_id: str | None = None):
    block = plan.block(block_id)
    if block is None:
        raise BoardError(f"unknown block {block_id!r}")
    if item_id is None:
        return block, None
    item = next((i for i in block.items if i.id == item_id), None)
    if item is None:
        raise BoardError(f"unknown item {item_id!r} in block {block_id!r}")
    return block, item


def _load(root: Path, body: dict[str, Any]) -> tuple[str, Plan]:
    date = str(body.get("date") or dt.date.today().isoformat())
    plan = load_plan(root, date)
    if plan is None:
        raise BoardError(f"no plan for {date}")
    return date, plan


# ---------------------------------------------------------------- the three writes

def api_done(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    date, plan = _load(root, body)
    block, item = _find(plan, str(body.get("block") or ""), body.get("item") or None)
    if item is not None:
        if item.not_applicable:
            raise BoardError("Restore this item before marking it done.")
        item.done = True
        entry = {"block": block.id, "minutes": 0, "note": f"item: {item.text}"[:200], "target": item.id, "item": item.id}
    else:
        minutes = body.get("minutes")
        if any(i.not_applicable for i in block.items) and minutes is None:
            raise BoardError("Enter actual study minutes for a block containing N/A items.")
        block.done = True
        entry = {"block": block.id, "minutes": int(minutes) if minutes is not None else block.minutes,
                 "note": str(body.get("note") or ""), "target": block.id}
    logmod.append(root / "data" / "log.jsonl", {"ts": _now_iso(), "date": date, **entry, "source": "board"})
    save_plan(root, plan)
    return plan_payload(root, date)


def api_item_decision(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    date, plan = _load(root, body)
    _, item = _find(plan, str(body.get("block") or ""), body.get("item") or None)
    state = body.get("state")
    reason = str(body.get("reason") or "").strip()
    if item is None or state not in ("applicable", "not_applicable") or not reason or len(reason) > 1000:
        raise BoardError("Choose an item and a valid state, with a reason (1–1000 characters).")
    item.decisions.append({"state": state, "reason": reason, "ts": _now_iso(), "source": "board"})
    save_plan(root, plan)
    return plan_payload(root, date)


def api_undo(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    date, plan = _load(root, body)
    block, item = _find(plan, str(body.get("block") or ""), body.get("item") or None)
    target = item.id if item is not None else block.id
    if item is not None:
        item.done = False
    else:
        block.done = False
    logmod.append(root / "data" / "log.jsonl",
                  {"ts": _now_iso(), "date": date, "block": block.id, "minutes": 0, "note": f"undo {target}",
                   "undo": True, "target": target, "source": "board"})
    save_plan(root, plan)
    return plan_payload(root, date)


def api_block(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    date, plan = _load(root, body)
    block, _ = _find(plan, str(body.get("block") or ""))
    if body.get("start") is not None:
        start = str(body["start"])
        try:
            block.start = _hhmm(snap(to_minutes(start)))
        except ValueError as e:
            raise BoardError(f"bad start {start!r} (expected HH:MM)") from e
    if body.get("minutes") is not None:
        block.minutes = max(snap(int(body["minutes"])), 5)
    if body.get("title"):
        block.title = str(body["title"])
    if body.get("start") is not None or body.get("minutes") is not None:
        plan.arranged = True                    # from now on a calendar refresh leaves the layout alone
    save_plan(root, plan)
    return plan_payload(root, date)


def api_busy(root: Path, date: str) -> dict[str, Any]:
    """Refresh the outside sources into the day. Re-lays the blocks only when busy time changed and the day is
    not arranged by hand; the page polls this every five minutes and on focus."""
    plan = load_plan(root, date)
    if plan is None:
        raise BoardError(f"no plan for {date}")
    changed, warnings = busymod.refresh(root, parent_root(root), plan)
    if changed:
        save_plan(root, plan)
    payload = plan_payload(root, date)
    payload["changed"] = changed
    payload["busy_warnings"] = warnings
    return payload


def _hhmm(minutes: int) -> str:
    minutes = max(0, min(24 * 60 - 5, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------------------------------------------------------------- the page

def render_html(plan: dict[str, Any], hub: dict[str, Any] | None = None, *, reports: dict[str, Any] | None = None,
                nav: str = "", panels: str = "", scripts: str = "") -> str:
    """The page. With `hub` (the static site: `{"files": {ref: link}, "today": date, "prev": href, "next": href,
    "write": {repo, branch, api}}`) the page runs in hub mode: no local server; writes go through the GitHub
    Contents API when `write` is set and `scripts` inlines `static/hubwrite.js` (mode `github`), otherwise
    check-off and moves are disabled (mode `static`); `nav` goes into the header, `panels` below the board.
    `reports` is `reports_payload(...)`: where the Jobs and Events tabs find the two daily reports."""
    blob = json.dumps(plan, ensure_ascii=False).replace("<", "\\u003c")
    hub_blob = json.dumps(hub, ensure_ascii=False).replace("<", "\\u003c") if hub else "null"
    rep_blob = json.dumps(reports or {"jobs": None, "events": None, "folders": dict(REPORT_FOLDERS)},
                          ensure_ascii=False).replace("<", "\\u003c")
    return (HTML.replace("__PLAN__", blob).replace("__HUB__", hub_blob).replace("__REPORTS__", rep_blob)
            .replace("__DATE__", plan["date"]).replace("__NAV__", nav).replace("__PANELS__", panels)
            .replace("__GLASS_THEME__", glass_css()).replace("__TIMELINE_JS__", timeline_js()).replace("__SCRIPTS__", scripts))


def render_no_plan_html(date: str, message: str) -> str:
    """What the browser gets for a day without a plan (the API routes answer 400 JSON as before)."""
    d = htmlmod.escape(date)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Study Planner AI · " + d + "</title>"
            "<style>body{font:14px/1.5 ui-sans-serif,-apple-system,sans-serif;padding:40px;color:#333}code{font-family:ui-monospace,monospace}</style>"
            "</head><body><h1>No plan for " + d + "</h1><p>" + htmlmod.escape(message) + "</p>"
            "<p><a href=\"/\">Back to the served day</a></p></body></html>")


def glass_css() -> str:
    return (Path(__file__).parent / "static" / "glass.css").read_text()


def hubwrite_js() -> str:
    return (Path(__file__).parent / "static" / "hubwrite.js").read_text()


def timeline_js() -> str:
    return (Path(__file__).parent / "static" / "timeline.js").read_text()


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Study Planner AI · __DATE__</title>
<style>
/* --- design tokens: the same block the job and event reports use (jobradar/templates/report.html.j2) ------ */
:root {
  --bg:oklch(0.98 0.005 262); --panel:oklch(1 0 0); --panel-2:oklch(0.985 0.006 262);
  --fg:oklch(0.22 0.028 262); --muted:oklch(0.52 0.022 262); --faint:oklch(0.63 0.02 262);
  --line:oklch(0.9 0.014 262); --line-soft:oklch(0.945 0.009 262); --chip:oklch(0.955 0.012 262);
  --accent:oklch(0.43 0.16 266); --accent-2:oklch(0.53 0.15 262); --accent-soft:oklch(0.935 0.035 266);
  --accent-ink:oklch(0.38 0.15 266); --on-accent:oklch(0.99 0.005 260);
  --good:oklch(0.5 0.09 195);  --good-soft:oklch(0.95 0.025 195);
  --warn:oklch(0.5 0.07 70);   --warn-soft:oklch(0.955 0.018 80);
  --bad:oklch(0.52 0.13 25);   --bad-soft:oklch(0.95 0.025 25);
  --r-sm:8px; --r-md:11px; --r-lg:14px; --r-pill:999px;
  --sh-1:0 1px 2px oklch(0.22 0.03 262 / .05), 0 1px 3px oklch(0.22 0.03 262 / .04);
  --sh-2:0 8px 24px oklch(0.22 0.03 262 / .1), 0 2px 6px oklch(0.22 0.03 262 / .05);
  --sh-3:0 24px 60px oklch(0.18 0.03 262 / .22);
  --maxw:1720px; --pad:clamp(16px,3vw,40px);
  --ease:cubic-bezier(.32,.72,0,1);
  --lane-l:0.52; --lane-c:0.105; --tint-l:0.958; --tint-c:0.022;  /* lane edge and tint: one hue per lane, one lightness and chroma for all */
  color-scheme:light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:oklch(0.16 0.02 262); --panel:oklch(0.21 0.022 262); --panel-2:oklch(0.245 0.022 262);
    --fg:oklch(0.95 0.012 262); --muted:oklch(0.7 0.022 262); --faint:oklch(0.6 0.022 262);
    --line:oklch(0.3 0.024 262); --line-soft:oklch(0.26 0.022 262); --chip:oklch(0.26 0.022 262);
    --accent:oklch(0.7 0.14 264); --accent-2:oklch(0.79 0.12 262); --accent-soft:oklch(0.31 0.07 266);
    --accent-ink:oklch(0.85 0.09 264); --on-accent:oklch(0.16 0.02 260);
    --good:oklch(0.75 0.09 190); --good-soft:oklch(0.28 0.04 190);
    --warn:oklch(0.78 0.08 75);  --warn-soft:oklch(0.3 0.04 70);
    --bad:oklch(0.72 0.12 25);   --bad-soft:oklch(0.3 0.06 25);
    --sh-1:0 1px 2px oklch(0 0 0 / .4); --sh-2:0 10px 28px oklch(0 0 0 / .5); --sh-3:0 24px 60px oklch(0 0 0 / .6);
    --lane-l:0.74; --lane-c:0.1; --tint-l:0.27; --tint-c:0.035;
    color-scheme:dark;
  }
}
*,*::before,*::after { box-sizing:border-box }
html { -webkit-text-size-adjust:100%; height:100% }
body { margin:0; background:var(--bg); color:var(--fg); -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
  font:13px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif; }
a { color:var(--accent) } :focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:4px }
::selection { background:var(--accent-soft) }
button, input, select, textarea { font:inherit; color:inherit }
.i { width:14px; height:14px; flex:none; vertical-align:-2px; fill:none; stroke:currentColor; stroke-width:1.75; stroke-linecap:round; stroke-linejoin:round }
code, pre { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.93em }

/* --- the board fills the viewport; the hub's panels sit below it ------------------------------------------- */
.board { height:100vh; display:flex; flex-direction:column; min-height:0 }
.hdr { position:sticky; top:0; z-index:15; padding:10px var(--pad) 8px; background:var(--bg); border-bottom:1px solid var(--line); flex:none }
@supports (backdrop-filter:blur(1px)) { .hdr { background:color-mix(in oklab, var(--bg) 82%, transparent); backdrop-filter:saturate(180%) blur(14px) } }
.nav { display:flex; flex-wrap:wrap; gap:6px; align-items:center; font-size:12px; margin:0 0 6px }
.nav a, .nav span.here { border:1px solid var(--line); background:var(--panel); border-radius:var(--r-pill); padding:2px 9px; text-decoration:none; color:var(--muted) }
.nav a:hover { border-color:var(--accent); color:var(--accent) }
.nav span.here { background:var(--accent); color:var(--on-accent); border-color:var(--accent) } .nav .sep { color:var(--faint) }
.row { display:flex; flex-wrap:wrap; gap:8px 18px; align-items:center }
.datenav { display:flex; align-items:center; gap:4px }
h1 { font-size:clamp(17px,1.6vw,20px); font-weight:680; letter-spacing:-.02em; margin:0 6px; white-space:nowrap }
.ib { display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; border-radius:var(--r-sm); border:1px solid transparent;
  background:transparent; color:var(--muted); cursor:pointer; text-decoration:none; padding:0 }
.ib:hover { border-color:var(--line); color:var(--accent); background:var(--panel) } .ib[hidden] { display:none }
.chip { border:1px solid var(--line); background:var(--panel); color:var(--muted); border-radius:var(--r-pill);
  padding:4px 11px; font-size:12.5px; font-weight:500; cursor:pointer; transition:color .15s, border-color .15s, background .15s; display:inline-flex; align-items:center; gap:5px }
.chip:hover { border-color:var(--accent); color:var(--accent) }
.chip.on { background:var(--accent); color:var(--on-accent); border-color:var(--accent) }
.kpi { display:flex; align-items:center; gap:8px; color:var(--muted); font-size:12.5px; white-space:nowrap }
.kpi b { color:var(--fg); font-weight:650; font-variant-numeric:tabular-nums }
.bar { width:120px; height:4px; border-radius:var(--r-pill); background:var(--chip); overflow:hidden }
.bar i { display:block; height:100%; background:var(--accent); border-radius:var(--r-pill); transition:width .5s var(--ease) }
.ctl { margin-left:auto; display:flex; align-items:center; gap:8px }
.sub { color:var(--muted); font-size:12px }
.menu { position:relative } .menu summary { list-style:none } .menu summary::-webkit-details-marker { display:none }
.pop { position:absolute; right:0; top:34px; z-index:30; min-width:260px; max-width:min(420px, 90vw); background:var(--panel); border:1px solid var(--line); border-radius:var(--r-md);
  box-shadow:var(--sh-2); padding:6px; font-size:12.5px }
.pop a, .pop button { display:flex; width:100%; align-items:center; gap:8px; padding:6px 9px; border:0; background:transparent; border-radius:var(--r-sm); text-align:left; text-decoration:none; color:var(--fg); cursor:pointer }
.pop a:hover, .pop button:hover { background:var(--chip) } .pop a[hidden], .pop button[hidden] { display:none }
.pop .h { color:var(--faint); font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; padding:8px 9px 2px }
.pop ul { margin:0; padding:0 9px 6px 24px; color:var(--muted); line-height:1.45 } .pop li { margin:3px 0 }
.pop .keys { color:var(--muted); padding:2px 9px 6px; line-height:1.6 } .pop kbd { font:inherit; font-size:11px; border:1px solid var(--line); border-radius:4px; padding:0 4px; background:var(--panel-2) }
.callrow { margin-top:8px; display:flex; align-items:baseline; gap:8px; font-size:13.5px; min-width:0 }
.callrow b { color:var(--faint); font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; font-weight:650; flex:none }
.callrow .call { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; flex:1 }
.callrow.open .call { white-space:normal } .callrow .more { border:0; background:transparent; color:var(--accent); cursor:pointer; padding:0; flex:none; font-size:12px }
.callrow .more[hidden] { display:none }
.allday { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px } .allday:empty { display:none }
.allday .e { display:inline-flex; align-items:center; gap:5px; font-size:11.5px; padding:2px 9px; border-radius:var(--r-pill); background:var(--chip); color:var(--muted) }
.allday .e a { color:inherit; text-decoration:none }
.warn { display:flex; gap:8px; align-items:flex-start; color:var(--warn); background:var(--warn-soft); border-radius:var(--r-sm); padding:5px 10px; font-size:12px; margin:6px 0 0 }
.note { color:var(--muted); font-size:12px; margin:6px 0 0 } .note[hidden] { display:none }
#settings { margin:6px 0 0; font-size:12px } #settings summary { cursor:pointer; color:var(--muted) }
#settings .box { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-md); padding:10px 14px; margin-top:6px; max-width:720px }
#settings input[type=password] { width:100%; padding:5px 8px; border-radius:var(--r-sm); border:1px solid var(--line); background:var(--bg) }
#settings button { border:1px solid var(--line); background:var(--panel-2); border-radius:var(--r-sm); padding:3px 10px; cursor:pointer; margin:6px 6px 0 0 }
#settings .scope { font-family:ui-monospace,Menlo,monospace } #settings .warn { display:inline; background:transparent; padding:0 }
#writes { font-size:11.5px; color:var(--muted); margin:4px 0 0; max-height:60px; overflow:auto } #writes code { display:block; overflow-wrap:anywhere; font-size:11px }
.narrow { display:none }

/* --- the two panes ---------------------------------------------------------------------------------------- */
.panes { flex:1; min-height:0; display:grid; grid-template-columns:minmax(360px, 440px) minmax(0, 1fr) }
.tl { position:relative; overflow:auto; border-right:1px solid var(--line); outline:none; background:var(--bg) }
#timeline { position:relative; margin:6px 0 12px }
.grid, .chips { position:absolute; inset:0 } .chips { pointer-events:none } .chips > * { pointer-events:auto }
.hr { position:absolute; left:52px; right:0; border-top:1px solid var(--line) } .hr.half { border-top-color:var(--line-soft) }
.hl { position:absolute; left:0; width:46px; text-align:right; font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums; transform:translateY(-50%); line-height:1 }
.strip { position:absolute; left:8px; right:8px; height:22px; display:flex; align-items:center; justify-content:center; gap:8px; font-size:11px; color:var(--faint);
  border-radius:var(--r-sm); cursor:pointer; user-select:none; letter-spacing:.04em; transition:background .15s, color .15s }
.strip::before, .strip::after { content:""; flex:1; border-top:1px dashed var(--line); max-width:64px }
.strip:hover, .strip.hot { background:var(--accent-soft); color:var(--accent-ink) }
.fold { position:absolute; right:8px; transform:translateY(-100%); font-size:10.5px; color:var(--faint); background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-pill); padding:1px 8px; cursor:pointer; z-index:3 }
.fold:hover { color:var(--accent); border-color:var(--accent) }
#now { position:absolute; left:46px; right:0; border-top:1.5px solid var(--bad); z-index:4; pointer-events:none }
#now::before { content:""; position:absolute; left:0; top:-4.5px; width:8px; height:8px; border-radius:50%; background:var(--bad) }
.tc { position:absolute; border-radius:var(--r-sm); border:1px solid transparent; border-left:3px solid oklch(var(--lane-l) var(--lane-c) var(--h));
  background:oklch(var(--tint-l) var(--tint-c) var(--h)); padding:3px 8px 3px 7px; overflow:hidden; cursor:pointer; z-index:2; touch-action:none;
  transition:transform .2s var(--ease), box-shadow .2s var(--ease) }
.tc .txt { overflow:hidden } .tc .t { font-weight:600; font-size:12.5px; line-height:16px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; display:flex; gap:5px; align-items:center }
.tc .t span { min-width:0; overflow:hidden; text-overflow:ellipsis } .tc .t .i { width:12px; height:12px; opacity:.7 }
.tc .m { font-size:11px; line-height:14px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-variant-numeric:tabular-nums }
.tc.tiny .m { display:none } .tc.tiny { padding-top:1px; padding-bottom:1px }     /* a title-only chip fits in 18 px */
.tc:hover { transform:translateY(-1px); box-shadow:var(--sh-1); z-index:5 }
.tc.sel { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset; z-index:6 }
.tc.ev { --h:262; background:var(--chip); border-left-color:var(--faint); cursor:pointer } .tc.ev .t { font-weight:500 } .tc.ev.free { border-left-style:dashed; opacity:.75 }
.tc.done { background:var(--panel-2); border-left-color:var(--good); color:var(--muted) } .tc.done .t { text-decoration:line-through; text-decoration-color:var(--faint) }
.tc.pop { animation:pop .38s ease-out } @keyframes pop { 30% { transform:scale(1.05) } }
ul.items li.pop { animation:pop .3s ease-out }
.lanes .bar i { transition:width .5s ease-out }
#fx { position:fixed; inset:0; width:100%; height:100%; pointer-events:none; z-index:90 }
.fx-shake { animation:fxShake .22s cubic-bezier(.36,.07,.19,.97) both }
@keyframes fxShake { 0%,100%{transform:translate(0,0)} 20%{transform:translate(-3px,1.5px)} 40%{transform:translate(3px,-1.5px)} 60%{transform:translate(-2px,1px)} 80%{transform:translate(1.5px,-0.5px)} }
.fx-hero-wrap { position:fixed; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; pointer-events:none; z-index:9999 }
.fx-hero-card {
  display:flex; flex-direction:column; align-items:center; gap:9px; padding:18px 36px; border-radius:18px;
  background:rgba(15, 23, 42, 0.88);
  -webkit-backdrop-filter:blur(24px) saturate(180%);
  backdrop-filter:blur(24px) saturate(180%);
  border:1.5px solid rgba(56, 189, 248, 0.45);
  box-shadow:0 18px 50px -10px rgba(0,0,0,.7), 0 0 0 1px rgba(255,255,255,.12) inset, 0 0 24px rgba(56,189,248,.15);
  animation:fxHeroCard 2.4s cubic-bezier(0.16, 1, 0.3, 1) forwards;
  will-change:transform, opacity; text-align:center; max-width:min(500px, 90vw);
}
.fx-hero-card.fever-card {
  border-color:rgba(245, 158, 11, 0.7);
  box-shadow:0 18px 50px -10px rgba(0,0,0,.7), 0 0 0 1px rgba(255,255,255,.16) inset, 0 0 30px rgba(245,158,11,.28);
}
.fx-hero-card.supernova-card {
  gap:14px; padding:36px 56px; border-radius:24px; max-width:min(660px, 92vw);
  border-color:rgba(245, 158, 11, 0.85);
  box-shadow:0 30px 95px -10px rgba(0,0,0,.85), 0 0 0 1px rgba(255,255,255,.2) inset, 0 0 55px rgba(245,158,11,.45), 0 0 100px rgba(168,85,247,.35);
  animation:fxSupernovaCard 3.8s cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
.fx-hero-card.supernova-card .fx-hero-title {
  font-size:clamp(28px, 4vw, 38px);
}
.fx-hero-card.supernova-card .fx-hero-pill {
  font-size:12px; padding:6px 18px;
}
@keyframes fxHeroCard {
  0% { opacity:0; transform:scale(0.88) translateY(14px); }
  10% { opacity:1; transform:scale(1.02) translateY(-2px); }
  16% { opacity:1; transform:scale(1) translateY(0); }
  84% { opacity:1; transform:scale(1) translateY(0); }
  100% { opacity:0; transform:scale(0.96) translateY(-10px); }
}
@keyframes fxSupernovaCard {
  0% { opacity:0; transform:scale(0.86) translateY(16px); }
  8% { opacity:1; transform:scale(1.02) translateY(-2px); }
  14% { opacity:1; transform:scale(1) translateY(0); }
  86% { opacity:1; transform:scale(1) translateY(0); }
  100% { opacity:0; transform:scale(0.96) translateY(-12px); }
}
.fx-hero-pill {
  display:inline-flex; align-items:center; gap:5px; padding:4px 14px; border-radius:999px;
  font-size:11px; font-weight:800; letter-spacing:.06em; text-transform:uppercase;
  color:#38bdf8; background:rgba(56, 189, 248, 0.16); border:1px solid rgba(56, 189, 248, 0.45);
  box-shadow:0 0 10px rgba(56, 189, 248, 0.18);
}
.fx-hero-card.fever-card .fx-hero-pill {
  color:#fbbf24; background:rgba(245, 158, 11, 0.18); border-color:rgba(245, 158, 11, 0.5);
  box-shadow:0 0 12px rgba(245, 158, 11, 0.22);
}
.fx-hero-card.supernova-card .fx-hero-pill {
  color:#fde047; background:rgba(234, 179, 8, 0.2); border-color:rgba(234, 179, 8, 0.55);
  box-shadow:0 0 18px rgba(234, 179, 8, 0.3);
}
.fx-hero-title {
  font-size:clamp(20px, 3vw, 25px); font-weight:900; letter-spacing:-.02em; color:#f8fafc;
  line-height:1.2; margin:0; text-shadow:0 2px 10px rgba(0,0,0,0.5);
}
.fx-hero-streak {
  display:inline-flex; align-items:center; gap:6px; font-size:13px; font-weight:800; color:#fbbf24;
  background:rgba(245, 158, 11, 0.2); border:1px solid rgba(245, 158, 11, 0.45);
  padding:4px 16px; border-radius:999px; animation:fxFeverPulse 1.1s ease-in-out infinite alternate;
  box-shadow:0 0 12px rgba(245, 158, 11, 0.22);
}
@keyframes fxFeverPulse { 0% { transform:scale(1); } 100% { transform:scale(1.04); } }
.fx-hero-scramble {
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; font-weight:600;
  color:#38bdf8; letter-spacing:.02em; max-width:480px; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; text-shadow:0 1px 4px rgba(0,0,0,0.4);
}
.fx-supernova-vignette {
  position:fixed; inset:0; pointer-events:none; z-index:9990;
  background:radial-gradient(circle at 50% 40%, rgba(5,5,15,0.5) 0%, rgba(0,0,0,0.94) 80%);
  opacity:0; animation:fxVignette 0.45s ease-in forwards;
}
@keyframes fxVignette { 0%{opacity:0} 100%{opacity:1} }
.fx-supernova-flash {
  position:fixed; inset:0; pointer-events:none; z-index:9998;
  background:radial-gradient(circle at 50% 40%, #fff 0%, rgba(254,240,138,0.9) 35%, rgba(56,189,248,0.45) 75%, transparent 100%);
  animation:fxSupernovaFlash 0.65s ease-out forwards;
}
@keyframes fxSupernovaFlash { 0%{opacity:0.98} 30%{opacity:0.85} 100%{opacity:0} }
.fx-board-implode {
  transition:transform .48s cubic-bezier(.4,0,.7,0) !important;
  transform:scale(0.955) !important;
}
.fx-board-explode {
  transition:transform .55s cubic-bezier(.175,.885,.32,1.25) !important;
  transform:scale(1) !important;
}
.fx-shake-heavy { animation:fxShakeHeavy .55s cubic-bezier(.36,.07,.19,.97) both }
@keyframes fxShakeHeavy {
  0%,100%{transform:translate(0,0) rotate(0deg)}
  12%{transform:translate(-7px,4px) rotate(-0.8deg)}
  25%{transform:translate(7px,-5px) rotate(0.8deg)}
  40%{transform:translate(-5px,3px) rotate(-0.4deg)}
  60%{transform:translate(4px,-2px) rotate(0.3deg)}
  75%{transform:translate(-2px,1px) rotate(-0.1deg)}
  90%{transform:translate(1px,-0.5px)}
}
.fx-hero-card.supernova-card {
  border-color:rgba(245, 158, 11, 0.85);
  box-shadow:0 30px 95px -10px rgba(0,0,0,.85), 0 0 0 1px rgba(255,255,255,.2) inset, 0 0 55px rgba(245,158,11,.45), 0 0 100px rgba(168,85,247,.35);
}
@media (prefers-reduced-motion: reduce) { .tc.pop, ul.items li.pop, .tc.running, .fx-shake, .fx-shake-heavy, .fx-hero-card, .fx-hero-streak, .fx-board-implode, .fx-board-explode, .fx-supernova-vignette, .fx-supernova-flash { animation:none; transition:none !important } }
.tc.over { border-left-color:var(--warn); background:var(--warn-soft) }
.tc.running { animation:pulse 2.2s ease-in-out infinite } @keyframes pulse { 50% { border-left-color:var(--accent-2); box-shadow:0 0 0 2px var(--accent-soft) } }
.tc.dragging { z-index:9; box-shadow:var(--sh-2); opacity:.92 }
.tc .rz { position:absolute; left:0; right:0; bottom:0; height:7px; cursor:ns-resize } .tc.tiny .rz { height:5px }
.tc:focus-visible { outline:2px solid var(--accent); outline-offset:1px }

.pane { display:flex; flex-direction:column; min-width:0; min-height:0; container-type:inline-size; background:var(--bg) }
.tabs { display:flex; align-items:center; gap:4px; padding:8px var(--pad) 0; border-bottom:1px solid var(--line); flex:none }
.tab { border:0; background:transparent; color:var(--muted); padding:6px 12px 9px; font-weight:500; cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-1px; border-radius:var(--r-sm) var(--r-sm) 0 0 }
.tab:hover { color:var(--accent); background:var(--panel-2) } .tab.on { color:var(--fg); border-bottom-color:var(--accent) }
.tabs .out { margin-left:auto; display:none } .tabs .out.on { display:inline-flex }
.tabs .back { display:none; margin-right:4px }
.tb { display:none; flex:1; min-height:0 } .tb.on { display:flex; flex-direction:column }
#tab-study { overflow:auto; padding:18px var(--pad) 40px }
.rhead { padding:8px var(--pad); font-size:12.5px; color:var(--muted); border-bottom:1px solid var(--line-soft); display:flex; gap:12px; align-items:center; flex-wrap:wrap; flex:none }
.rhead a { margin-left:auto; text-decoration:none; display:inline-flex; gap:5px; align-items:center } .rhead .old { color:var(--warn) }
.tb iframe { flex:1; width:100%; border:0; background:var(--bg); min-height:0 }
.tb .none { padding:40px var(--pad); color:var(--muted) }

/* the study pane: the reveal is a short fade; the fallback class arrives from JS within 700 ms whatever happens */
.det { opacity:0; transform:translateY(8px); transition:opacity .4s var(--ease), transform .4s var(--ease) } .det.in { opacity:1; transform:none }
.dh { border-left:3px solid oklch(var(--lane-l) var(--lane-c) var(--h)); padding-left:12px; margin-bottom:12px }
.dh h2 { font-size:17.5px; font-weight:640; line-height:1.32; letter-spacing:-.011em; margin:0 }
.dh .meta { color:var(--muted); font-size:12.5px; margin-top:3px; display:flex; flex-wrap:wrap; gap:4px 12px; align-items:center; font-variant-numeric:tabular-nums }
.timer { display:inline-flex; align-items:center; gap:2px; border:1px solid var(--line); border-radius:var(--r-pill); padding:1px 4px 1px 2px; background:var(--panel) }
.timer .ib { width:24px; height:24px } .timer .t { font-weight:650; font-variant-numeric:tabular-nums; min-width:52px; text-align:center; color:var(--fg) } .timer .t.over { color:var(--warn) }
.item-action { display:block; margin-top:6px; }
.current-item { border-left:3px solid var(--accent); }
.action-note { padding:12px 0; }
.first { display:flex; align-items:center; gap:9px; margin:14px 0 8px; padding:10px 14px; border-radius:var(--r-md); font-size:14px; font-weight:600; text-decoration:none; overflow-wrap:anywhere }
a.first { background:var(--accent); color:var(--on-accent); box-shadow:var(--sh-1); transition:background .15s, transform .15s } a.first:hover { background:var(--accent-2); transform:translateY(-1px) }
a.first .i { width:16px; height:16px } a.first small { font-weight:450; opacity:.8; font-size:12px; margin-left:auto; white-space:nowrap }
div.first { background:var(--accent-soft); color:var(--accent-ink) } div.first small { font-weight:450; color:var(--accent-ink); opacity:.8; margin-left:auto; white-space:nowrap; font-size:12px }
div.first a { color:inherit }
.desc p { margin:6px 0; font-size:13.5px; line-height:1.55; overflow-wrap:anywhere }
.code { position:relative; margin:8px 0 } .code pre { margin:0; background:var(--panel); border:1px solid var(--line); border-radius:var(--r-sm); padding:10px 12px; overflow-x:auto; font-size:12px; line-height:1.5; tab-size:4 }
.code .cp { position:absolute; right:6px; top:6px } .cp { display:inline-flex; align-items:center; gap:4px; border:1px solid var(--line); background:var(--panel-2); color:var(--muted); border-radius:var(--r-sm); padding:2px 7px; font-size:11px; cursor:pointer }
.cp:hover { color:var(--accent); border-color:var(--accent) }
ul.items { list-style:none; margin:12px 0 0; padding:0 }
ul.items li { display:flex; gap:10px; align-items:flex-start; padding:7px 4px; border-top:1px solid var(--line-soft); font-size:13.5px; line-height:1.5; overflow-wrap:anywhere; border-radius:var(--r-sm); transition:background .12s }
ul.items li:first-child { border-top:0 }
ul.items li:hover { background:color-mix(in oklab, var(--chip) 45%, transparent) }
ul.items input[type="checkbox"] {
  -webkit-appearance:none; appearance:none; margin:1.5px 0 0; width:20px; height:20px; flex:none;
  border-radius:6px; border:1.8px solid var(--line); background-color:var(--panel); cursor:pointer;
  transition:border-color .15s, background-color .15s, box-shadow .15s, transform .15s cubic-bezier(.16,1,.3,1);
  box-shadow:0 1px 2px rgba(0,0,0,.06); position:relative;
}
ul.items input[type="checkbox"]:hover:not(:disabled) {
  border-color:var(--accent); background-color:var(--chip); transform:scale(1.08); box-shadow:0 2px 8px rgba(0,0,0,.1);
}
ul.items input[type="checkbox"]:checked {
  background-color:#10b981; border-color:#059669;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 16 16' fill='none' stroke='white' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round' xmlns='http://www.w3.org/2000/svg'%3E%3Cpolyline points='3.5 8.5 6.5 11.5 12.5 4.5'/%3E%3C/svg%3E");
  background-position:center; background-repeat:no-repeat; background-size:12px 12px;
  box-shadow:0 0 12px rgba(16,185,129,.42);
}
ul.items input[type="checkbox"]:disabled { opacity:.45; cursor:default }
ul.items input[type="checkbox"]:focus-visible { outline:2px solid var(--accent); outline-offset:2px }
ul.items li.on span { color:var(--muted); text-decoration:line-through; text-decoration-color:var(--faint) }
#done-btn.on { background:linear-gradient(135deg, #059669, #0d9488); border-color:#059669; color:#fff; box-shadow:0 2px 10px rgba(5,150,105,.3) }
.refs { display:inline-flex; flex-wrap:wrap; gap:4px; margin-left:6px; vertical-align:baseline }
.refs a, .refs code { font-size:11px; padding:0 7px; border-radius:var(--r-pill); background:var(--chip); text-decoration:none; color:var(--muted); white-space:nowrap }
.refs a { color:var(--accent) }
.links { display:flex; flex-wrap:wrap; gap:6px; margin:14px 0 0 } .pill { display:inline-flex; align-items:center; gap:5px; border:1px solid var(--line); background:var(--panel); color:var(--muted); border-radius:var(--r-pill); padding:4px 11px; font-size:12.5px; text-decoration:none }
.pill:hover { border-color:var(--accent); color:var(--accent) } .pill .i { opacity:.7 }
.tools { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:18px; padding-top:12px; border-top:1px solid var(--line); font-size:12.5px }
.btn { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--line); background:var(--panel-2); color:var(--muted); border-radius:var(--r-sm); padding:5px 11px; cursor:pointer; text-decoration:none; font-weight:500; transition:color .15s, border-color .15s, background .15s }
.btn:hover { border-color:var(--accent); color:var(--accent) } .btn.on { background:var(--accent); color:var(--on-accent); border-color:var(--accent) } .btn:disabled { opacity:.5; cursor:default }
.tools form { display:inline-flex; gap:4px; align-items:center; color:var(--muted) }
.tools input { padding:4px 6px; border-radius:var(--r-sm); border:1px solid var(--line); background:var(--panel); color:var(--fg); font-size:12.5px } .tools input[type=number] { width:64px }
.hint { display:flex; align-items:center; gap:6px; margin-top:10px; color:var(--muted); font-size:12px; min-width:0 } .hint code { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0 }
.empty h2 { font-size:15px; font-weight:640; margin:0 0 8px } .empty h3 { font-size:11px; color:var(--faint); text-transform:uppercase; letter-spacing:.06em; margin:18px 0 6px; font-weight:650 }
.lanes { display:grid; grid-template-columns:auto 1fr auto; gap:5px 12px; align-items:center; font-size:12.5px } .lanes .n { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:220px }
.lanes .bar { width:100%; max-width:none } .lanes .bar i { background:oklch(var(--lane-l) var(--lane-c) var(--h)) } .lanes .v { color:var(--muted); font-variant-numeric:tabular-nums; white-space:nowrap }
.next { border:1px solid var(--line); background:var(--panel); border-radius:var(--r-md); padding:12px 14px; box-shadow:var(--sh-1); cursor:pointer; transition:border-color .2s var(--ease), transform .2s var(--ease) }
.next:hover { border-color:color-mix(in oklab, var(--accent) 30%, var(--line)); transform:translateY(-2px); box-shadow:var(--sh-2) }
.next b { display:inline; font-size:14px; background-image:linear-gradient(var(--accent),var(--accent)); background-size:0 1.5px; background-position:0 100%;
  background-repeat:no-repeat; transition:background-size .3s var(--ease), color .2s }          /* the reports' title: the underline draws in on hover */
.next:hover b { color:var(--accent); background-size:100% 1.5px } .next .sub { display:block; margin-top:3px }
.empty ul { margin:0; padding-left:18px; color:var(--muted); font-size:12.5px } .empty li { margin:3px 0 }
.muted { color:var(--muted) }
#toast { position:fixed; left:50%; bottom:20px; transform:translateX(-50%) translateY(80px); background:var(--fg); color:var(--bg); padding:8px 16px; border-radius:var(--r-pill); font-size:13px; transition:transform .25s var(--ease); z-index:50; box-shadow:var(--sh-2) }
#toast.on { transform:translateX(-50%) } #toast.bad { background:var(--bad); color:#fff }

/* the hub's panels below the board keep their ids; their look joins the tokens */
.panels { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:12px; padding:16px var(--pad) 48px; max-width:var(--maxw); margin:0 auto }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-lg); padding:14px 18px; font-size:13px; box-shadow:var(--sh-1) } .panel h2 { font-size:15px; margin:0 0 8px; font-weight:640 }
.panel ul { margin:0; padding-left:18px } .panel li { margin:3px 0 } .panel .muted { color:var(--muted); font-size:12.5px } .panel h3 { font-size:13px; margin:12px 0 4px }
.panel textarea { width:100%; min-height:56px; border:1px solid var(--line); border-radius:var(--r-sm); background:var(--bg); padding:6px; margin:2px 0 8px }
.panel select, .panel button { border:1px solid var(--line); background:var(--panel-2); border-radius:var(--r-sm); padding:3px 10px }
.panel button[type=submit] { background:var(--accent); color:var(--on-accent); border-color:var(--accent); cursor:pointer }
.panel label { display:block; margin:6px 0; font-size:12.5px; color:var(--muted) }

@container (max-width: 560px) { .tools { flex-direction:column; align-items:stretch } .tools form { flex-wrap:wrap } .dh h2 { font-size:16px } }
@media (max-width: 899px) {
  .panes { grid-template-columns:1fr } .tl { border-right:0 }
  .pane { position:fixed; inset:0; z-index:40; transform:translateX(102%); transition:transform .3s var(--ease); box-shadow:var(--sh-3) }
  .pane.open { transform:none } .tabs .back { display:inline-flex } .narrow { display:inline-flex }
  .bar { width:72px }
}
__GLASS_THEME__
/* surfaces: glass-4 for the sticky header, glass-3 for the pane, frosted for the menu and the hub's panels */
.hdr { background:var(--g-s4); border-bottom:1px solid var(--g-border); box-shadow:var(--g-sh-sm); backdrop-filter:saturate(var(--g-sat)) blur(24px); -webkit-backdrop-filter:saturate(var(--g-sat)) blur(24px) }
.tl { background:transparent; border-right:1px solid var(--g-border) }
.pane { background:var(--g-s3); backdrop-filter:saturate(var(--g-sat)) blur(16px); -webkit-backdrop-filter:saturate(var(--g-sat)) blur(16px) }
.tabs { border-bottom:1px solid var(--g-border) }
.tab { border-bottom:0; margin-bottom:6px; padding:6px 14px; border-radius:var(--r-pill); border:1px solid transparent; transition:background .25s var(--g-ease), box-shadow .25s var(--g-ease), color .2s }
.tab:hover { background:var(--g-s2); color:var(--fg) }
.tab.on { color:var(--fg); background:var(--g-btn); border-color:var(--g-edge); border-top-color:var(--g-top); box-shadow:0 0 0 1px var(--g-ring) inset, var(--g-btn-sh); backdrop-filter:saturate(var(--g-sat)) blur(24px); -webkit-backdrop-filter:saturate(var(--g-sat)) blur(24px) }
.pop, .panel, #settings .box { background:var(--g-frosted); border:1px solid var(--g-border-strong); border-top-color:var(--g-top); box-shadow:0 0 0 1px rgb(255 255 255 / .15) inset, 0 0 20px rgb(255 255 255 / .12) inset, var(--g-sh-md);
  backdrop-filter:saturate(200%) blur(40px); -webkit-backdrop-filter:saturate(200%) blur(40px) }
/* the button `glass` variant: chips, pills, tools, the timer, inputs */
.chip, .btn, .pill, .cp, .timer, .tools input, #settings input[type=password], #settings button, .panel textarea, .panel select, .panel button, .ib:hover {
  position:relative; background:var(--g-btn); color:var(--fg); border:1px solid var(--g-edge); border-top-color:var(--g-top);
  box-shadow:0 0 0 1px var(--g-ring) inset, var(--g-btn-sh); backdrop-filter:saturate(var(--g-sat)) blur(24px); -webkit-backdrop-filter:saturate(var(--g-sat)) blur(24px);
  transition:transform .25s var(--g-ease), background .25s var(--g-ease), box-shadow .25s var(--g-ease), color .2s, border-color .2s }
.chip:hover, .btn:hover, .pill:hover, .cp:hover { background:var(--g-btn-hover); color:var(--fg); border-color:var(--g-edge); border-top-color:var(--g-top); box-shadow:0 0 0 1px var(--g-ring-hover) inset, var(--g-btn-sh-hover); transform:translateY(-1px) }
.chip:active, .btn:active, .pill:active { transform:none }
.chip.on, .btn.on, .panel button[type=submit] { background:var(--g-solid); color:var(--g-on-solid); border-color:rgb(0 0 0 / .1); box-shadow:var(--g-solid-sh) }
.chip.on:hover, .btn.on:hover { background:var(--g-solid-hover); color:var(--g-on-solid) }
.btn:disabled { transform:none; box-shadow:none }
.timer .ib { background:transparent; border:0; box-shadow:none; backdrop-filter:none; -webkit-backdrop-filter:none }
/* the card `liquid` variant for the Next card, `glass` for chips */
.next { background:var(--g-card-liquid); border:1px solid var(--g-border-strong); border-top-color:var(--g-top); box-shadow:0 0 0 1px var(--g-border) inset, var(--g-sh-md);
  backdrop-filter:saturate(var(--g-sat)) blur(24px); -webkit-backdrop-filter:saturate(var(--g-sat)) blur(24px); transition:transform .25s var(--g-ease), box-shadow .25s var(--g-ease) }
.next:hover { border-color:var(--g-border-strong); box-shadow:0 0 0 1px var(--g-border) inset, var(--g-sh-lg); transform:translateY(-2px) }
.tc { background:linear-gradient(oklch(var(--tint-l) var(--tint-c) var(--h) / .55), oklch(var(--tint-l) var(--tint-c) var(--h) / .55)), var(--g-card), var(--g-s3);
  border:1px solid var(--g-border); border-top-color:var(--g-top); border-left:3px solid oklch(var(--lane-l) var(--lane-c) var(--h));
  box-shadow:0 0 0 1px rgb(255 255 255 / .1) inset, var(--g-sh-sm); backdrop-filter:saturate(var(--g-sat)) blur(12px); -webkit-backdrop-filter:saturate(var(--g-sat)) blur(12px) }
.tc:hover { box-shadow:0 0 0 1px rgb(255 255 255 / .1) inset, var(--g-sh-md); transform:translateY(-2px) }
.tc.sel { border-color:var(--accent); border-left-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset, var(--g-sh-md) }
.tc.ev { background:var(--g-card), var(--g-s3); border-left-color:var(--faint) } .tc.done { background:var(--g-card), var(--g-s2); border-left-color:var(--good) }
.tc.over { background:linear-gradient(var(--warn-soft), var(--warn-soft)), var(--g-s3); border-left-color:var(--warn) }
.hr { border-top-color:var(--line) } .hr.half { border-top-color:var(--line-soft) }
.strip:hover, .strip.hot { background:var(--g-btn); color:var(--fg) }
.code pre { background:var(--g-s4); border:1px solid var(--g-border-strong); border-top-color:var(--g-top); backdrop-filter:saturate(var(--g-sat)) blur(16px); -webkit-backdrop-filter:saturate(var(--g-sat)) blur(16px) }
.dh { border-left-color:oklch(var(--lane-l) var(--lane-c) var(--h)) }
/* the button `liquid` variant, size lg, for the First action: white liquid glass, a left-top highlight, a sheen that slides on hover */
a.first, div.first { position:relative; isolation:isolate; overflow:hidden; color:var(--fg); font-size:15px; font-weight:600; padding:12px 18px; border-radius:var(--r-md);
  background:var(--g-liquid); border:1px solid var(--g-edge); border-top-color:var(--g-top); box-shadow:var(--g-liquid-sh);
  backdrop-filter:saturate(var(--g-sat)) blur(24px); -webkit-backdrop-filter:saturate(var(--g-sat)) blur(24px);
  transition:transform .25s var(--g-ease), background .25s var(--g-ease), box-shadow .25s var(--g-ease), border-color .25s var(--g-ease) }
a.first::before, div.first::before { content:""; position:absolute; inset:0; z-index:-1; pointer-events:none; background:var(--g-liquid-before); opacity:var(--g-before-opacity) }
a.first::after { content:""; position:absolute; top:-4rem; bottom:-4rem; left:-5rem; width:6rem; z-index:-1; pointer-events:none; transform:rotate(12deg);
  background:linear-gradient(100deg, transparent 20%, var(--g-sheen) 50%, transparent 80%); opacity:var(--g-sheen-opacity); filter:blur(4px); transition:transform .4s var(--g-ease) }
a.first:hover { background:var(--g-liquid-hover); border-top-color:rgb(255 255 255 / .95); box-shadow:var(--g-liquid-sh-hover); transform:translateY(-1px) }
a.first:hover::after { transform:rotate(12deg) translateX(calc(100cqw + 12rem)) }
a.first .i { width:18px; height:18px; color:var(--accent-ink) } a.first small, div.first small { color:var(--muted); opacity:1 }
div.first a { color:var(--accent-ink) }
@media (prefers-reduced-transparency: reduce) {
  .hdr, .pane, .pop, .panel, .chip, .btn, .pill, .cp, .timer, .next, .tc, .code pre, a.first, div.first, .tab.on { backdrop-filter:none; -webkit-backdrop-filter:none }
  .hdr, .pane, .pop, .panel, .next, .tc, .code pre { background:var(--panel) }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition-duration:.01ms !important; animation-duration:.01ms !important }
  .tc:hover, .next:hover, a.first:hover, .chip:hover, .btn:hover, .pill:hover { transform:none } .det { opacity:1; transform:none }
  body::after, .orb { animation:none }
}
</style>
</head>
<body>
<svg aria-hidden="true" width="0" height="0" style="position:absolute"><symbol id="i-check" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></symbol><symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></symbol><symbol id="i-left" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></symbol><symbol id="i-right" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></symbol><symbol id="i-more" viewBox="0 0 24 24"><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/></symbol><symbol id="i-play" viewBox="0 0 24 24"><path d="M6 3 20 12 6 21z"/></symbol><symbol id="i-pause" viewBox="0 0 24 24"><rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/></symbol><symbol id="i-reset" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></symbol><symbol id="i-copy" viewBox="0 0 24 24"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></symbol><symbol id="i-out" viewBox="0 0 24 24"><path d="M7 7h10v10"/><path d="M7 17 17 7"/></symbol><symbol id="i-cal" viewBox="0 0 24 24"><path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/></symbol><symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></symbol><symbol id="i-go" viewBox="0 0 24 24"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></symbol><symbol id="i-alert" viewBox="0 0 24 24"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></symbol><symbol id="i-pin" viewBox="0 0 24 24"><path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/></symbol></svg>
<div class="orb b" aria-hidden="true"></div><div class="orb c" aria-hidden="true"></div>
<div class="board">
<header class="hdr">
__NAV__
<div class="row">
  <div class="datenav">
    <a class="ib" id="prev" aria-label="previous day" title="previous day"><svg class="i"><use href="#i-left"/></svg></a>
    <h1 id="date-h">__DATE__</h1>
    <a class="ib" id="next" aria-label="next day" title="next day"><svg class="i"><use href="#i-right"/></svg></a>
    <button class="chip" id="today">Today</button>
  </div>
  <div class="kpi"><span><b id="k-planned">0</b> min planned</span><span class="bar"><i id="bar"></i></span><span><b id="k-done">0</b> done</span></div>
  <div class="ctl">
    <span class="sub" id="status"></span>
    <button class="chip narrow" id="n-jobs">Jobs</button><button class="chip narrow" id="n-events">Events</button>
    <button class="chip" id="allhours" aria-pressed="false">Show all hours</button>
    <details class="menu" id="menu"><summary class="ib" aria-label="more" title="more"><svg class="i"><use href="#i-more"/></svg></summary>
      <div class="pop">
        <a id="m-jobs" target="_blank" rel="noopener" hidden><svg class="i"><use href="#i-out"/></svg>Job report</a>
        <a id="m-events" target="_blank" rel="noopener" hidden><svg class="i"><use href="#i-out"/></svg>Event report</a>
        <a id="m-json" target="_blank" rel="noopener" hidden><svg class="i"><use href="#i-out"/></svg>Open JSON</a>
        <a id="m-md" hidden><svg class="i"><use href="#i-out"/></svg>Open markdown</a>
        <button id="m-copylog"><svg class="i"><use href="#i-copy"/></svg>Copy log commands</button>
        <div class="h">Keys</div>
        <div class="keys"><kbd>↑</kbd> <kbd>↓</kbd> select · <kbd>Enter</kbd> open · <kbd>Space</kbd> tick the next item · <kbd>Esc</kbd> back<br>drag a chip to move it, its bottom edge to resize; 5-minute snap</div>
        <div id="m-radar"></div>
      </div>
    </details>
  </div>
</div>
<div class="callrow" id="callrow"><b>Today's call</b><span class="call" id="call"></span><button class="more" id="more" hidden>more</button></div>
<div class="allday" id="allday"></div>
<div id="warnings"></div>
<p class="note" id="readonly-note" hidden>This is the published copy without a write path: check-off and moves are disabled here. Open the day locally with <code>uv run studyplan serve --date __DATE__</code> to check off.</p>
<p class="note" id="hosted-note" hidden>This is the hosted copy: a tick, a move or a debrief is committed to the repo through the GitHub API (one commit each, <code>hub: __DATE__ …</code>), and the site re-renders in about two minutes. Set a token once under Settings. On the laptop, <code>git pull</code> before <code>studyplan serve</code>.</p>
<details id="settings" hidden>
  <summary>Settings · GitHub token <span id="token-state"></span></summary>
  <div class="box">
    <p>Writes go to <span class="scope" id="scope"></span> on branch <span class="scope" id="scope-branch"></span> through the GitHub Contents API.
    Create a <b>fine-grained personal access token</b> (GitHub → Settings → Developer settings → Fine-grained tokens) with
    <b>Repository access: only this repository</b> and <b>Permissions → Contents: Read and write</b>, 90-day expiry, and paste it here.
    It is kept in this browser's localStorage for this site only; it is never written into the repo, the page payload or the logs.</p>
    <input type="password" id="token" autocomplete="off" placeholder="github_pat_…" aria-label="GitHub token">
    <button type="button" id="token-save">Save token</button><button type="button" id="token-forget">Forget token</button>
    <span class="warn" id="token-msg"></span>
  </div>
</details>
<div id="writes" hidden></div>
</header>
<div class="panes">
  <div class="tl" id="tl" tabindex="-1"><div id="timeline"><div class="grid" id="grid"></div><div class="chips" id="chips"></div></div></div>
  <aside class="pane" id="pane" aria-label="detail">
    <div class="tabs" role="tablist">
      <button class="ib back" id="back" aria-label="back to the timeline"><svg class="i"><use href="#i-left"/></svg></button>
      <button class="tab on" role="tab" data-tab="study" id="tab-b-study">Study</button>
      <button class="tab" role="tab" data-tab="jobs" id="tab-b-jobs">Jobs</button>
      <button class="tab" role="tab" data-tab="events" id="tab-b-events">Events</button>
      <a class="ib out" id="tab-out" target="_blank" rel="noopener" title="open in its own tab" aria-label="open in its own tab"><svg class="i"><use href="#i-out"/></svg></a>
    </div>
    <section class="tb on" id="tab-study" role="tabpanel" tabindex="-1"></section>
    <section class="tb" id="tab-jobs" role="tabpanel"><div class="rhead" id="jobs-head"></div></section>
    <section class="tb" id="tab-events" role="tabpanel"><div class="rhead" id="events-head"></div></section>
  </aside>
</div>
</div>
__PANELS__
<div id="toast"></div>
<script id="plan-data" type="application/json">__PLAN__</script>
<script id="hub-data" type="application/json">__HUB__</script>
<script id="reports-data" type="application/json">__REPORTS__</script>
<script>
__TIMELINE_JS__
</script>
__SCRIPTS__
<script>
"use strict";
let PLAN = JSON.parse(document.getElementById("plan-data").textContent);
const HUB = JSON.parse(document.getElementById("hub-data").textContent);     // null on the local server
const REPORTS = JSON.parse(document.getElementById("reports-data").textContent);
const HUB_FILES = HUB && HUB.files ? HUB.files : {};
const WRITE = HUB && HUB.write && typeof HubWriter !== "undefined" ? HUB.write : null;   // GitHub as the backend
const MODE = !HUB ? "live" : (WRITE ? "github" : "static");
const READONLY = MODE === "static";
const DATE = PLAN.date;
const SNAP = 5, T = TimelineLayout, STRIP_H = 22, MIN_CHIP = 28, TITLE_ONLY = 18, TINY = 40, GUTTER = 56, RIGHT = 8;
/* Lane hues: one cool family around the reports' ultramarine (266), spaced so they stay apart at the shared
   lightness and chroma, never a warm hue (those are the semantic ones: overdue ochre, now-line brick). */
const LANES = { design: 266, leetcode: 215, news: 300, stack: 172, stories: 330, debrief: 330, chores: 245 };
const REDUCE = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const $ = (s, r) => (r || document).querySelector(s);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
const icon = name => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("class", "i");
  const u = document.createElementNS("http://www.w3.org/2000/svg", "use"); u.setAttribute("href", "#i-" + name); s.appendChild(u); return s; };
const mins = hhmm => { const [h, m] = String(hhmm || "0:0").split(":"); return (+h) * 60 + (+m || 0); };
const hhmm = m => { m = Math.max(0, Math.min(24 * 60 - SNAP, Math.round(m))); return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"); };
const snap = m => Math.round(m / SNAP) * SNAP;
const todayIso = () => { const d = new Date(); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };
const IS_TODAY = DATE === todayIso();
const nowMin = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); };
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtDate(iso, year) { const [y, m, d] = iso.split("-").map(Number); const dt = new Date(y, m - 1, d); return DAYS[dt.getDay()] + " " + d + " " + MONTHS[m - 1] + (year ? " " + y : ""); }
function shiftDate(iso, by) { const [y, m, d] = iso.split("-").map(Number); const dt = new Date(y, m - 1, d + by); return dt.getFullYear() + "-" + String(dt.getMonth() + 1).padStart(2, "0") + "-" + String(dt.getDate()).padStart(2, "0"); }
function laneOf(id) { const key = Object.keys(LANES).find(k => id === k || id.startsWith(k + "-")); return key ? LANES[key] : 262; }

/* ---------------------------------------------------------------- page state: selection, tab, collapsing */
const state = { sel: null, tab: "study", showAll: false, expanded: [], hot: null, foldTimer: null };
try { state.showAll = localStorage.getItem("board.allhours") === "1"; } catch (e) { /* private mode */ }
let geo = null;          // pixel geometry of the current timeline rows

function toast(msg, bad) {
  const t = $("#toast"); t.textContent = msg; t.className = "on" + (bad ? " bad" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.className = ""; }, bad ? 5000 : 1800);
}
function copy(text, what) {
  const done = () => toast("copied " + (what || ""));
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => toast("could not copy", true));
  else { const ta = el("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); done(); } catch (e) { toast("could not copy", true); } ta.remove(); }
}

/* ---------------------------------------------------------------- the hosted write path (hub mode) */
const TOKEN_KEY = "hub.token";
function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; } }
function setToken(t) { try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); } catch (e) { /* private mode */ } tokenState(); }
function tokenState() {
  const has = !!getToken();
  const st = $("#token-state"); if (st) st.textContent = has ? "· set" : "· not set";
  const w = $("#hosted-note"); if (w && !has) w.textContent = "This is the hosted copy: ticks, moves and debriefs commit to the repo through the GitHub API. Add a token under Settings first.";
}
function writer() {
  const token = getToken();
  if (!token) return null;
  return new HubWriter({ repo: WRITE.repo, branch: WRITE.branch || "main", api: WRITE.api, token });
}
function needToken() {
  const s = $("#settings"); if (s) { s.open = true; s.scrollIntoView({ block: "center" }); }
  toast("add a GitHub token under Settings first", true);
}
function noteWrite(res) {
  const box = $("#writes"); if (!box) return;
  box.hidden = false;
  const line = el("div");
  line.appendChild(el("span", null, "written " + new Date().toLocaleTimeString() + " · " + res.path + " · " + res.message + " · the site re-renders in ~2 min"));
  if (res.line) line.appendChild(el("code", null, res.line));
  box.prepend(line);
}
function isNA(it) { const ds = it.decisions || []; return ds.length > 0 && ds[ds.length - 1].state === "not_applicable"; }
function activeItems(b) { return b.items.filter(i => !isNA(i)); }
function resolved(b) { return b.done || (b.items.some(isNA) && b.items.every(i => i.done || isNA(i))); }
function remainingMinutes(b) {
  if (resolved(b)) return 0;
  if (!b.items.some(isNA)) return b.minutes;
  const pending = activeItems(b).filter(i => !i.done);
  return pending.reduce((sum, i) => { const m = i.text.match(/\b(\d+)\s*min(?:ute)?s?\b/i); return sum + (m ? Number(m[1]) : b.minutes / b.items.length); }, 0);
}
function itemSummary(b) { const active = activeItems(b), skipped = b.items.length - active.length; return active.filter(i => i.done).length + "/" + active.length + " items" + (skipped ? " · " + skipped + " 暂不做" : "") + (!b.done && resolved(b) ? " · 无待做项" : ""); }
/* Apply a write to PLAN before the network round trip, so the tick shows at once; the caller reverts on failure. */
function applyLocal(path, body) {
  const b = PLAN.blocks.find(x => x.id === body.block);
  if (!b) return;
  if (path === "/api/done" || path === "/api/undo") {
    const on = path === "/api/done";
    if (body.item) { const it = b.items.find(i => i.id === body.item); if (it) it.done = on; } else b.done = on;
  } else if (path === "/api/item-decision") {
    const it = b.items.find(i => i.id === body.item);
    if (it) (it.decisions ||= []).push({state: body.state, reason: body.reason, ts: new Date().toISOString(), source: "hub"});
  } else if (path === "/api/block") {
    if (body.start != null) b.start = hhmm(snap(mins(body.start)));
    if (body.minutes != null) b.minutes = Math.max(SNAP, snap(body.minutes));
    b.end = hhmm(mins(b.start) + b.minutes);
    PLAN.arranged = true;
  }
}
async function postHosted(path, body) {
  const w = writer();
  if (!w) { draw(); needToken(); throw new Error("no token"); }
  const before = JSON.stringify(PLAN);
  applyLocal(path, body);
  draw();
  const b = PLAN.blocks.find(x => x.id === body.block) || {};
  try {
    let res;
    if (path === "/api/done" || path === "/api/undo") {
      const it = body.item ? (b.items || []).find(i => i.id === body.item) : null;
      res = await w.tick({ date: DATE, block: body.block, item: body.item, text: it ? it.text : "", undo: path === "/api/undo",
                           minutes: body.item ? 0 : (body.minutes == null ? b.minutes : body.minutes), note: body.note || "" });
    } else if (path === "/api/item-decision") {
      res = await w.itemDecision({ date: DATE, ...body });
    } else if (path === "/api/block") {
      res = await w.moveBlock({ date: DATE, block: body.block, start: b.start, minutes: b.minutes, title: body.title });
    } else throw new Error("no hosted route for " + path);
    noteWrite(res);
    $("#status").textContent = "committed " + (res.commit || "").slice(0, 7);
    return PLAN;
  } catch (e) {
    PLAN = JSON.parse(before); draw();
    toast("not written: " + (e.message || e), true);
    throw e;
  }
}

async function post(path, body) {
  if (READONLY) { toast("read-only here: open the day with studyplan serve to write", true); draw(); throw new Error("read-only"); }
  if (WRITE) return postHosted(path, body);
  const ctl = new AbortController(); const killer = setTimeout(() => ctl.abort(), 10000);
  let r;
  try {
    r = await fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ date: DATE, ...body }), signal: ctl.signal });
  } catch (e) {
    toast("board server isn't responding — restart `studyplan serve`", true); throw e;
  } finally { clearTimeout(killer); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { toast(data.error || ("HTTP " + r.status), true); throw new Error(data.error || r.status); }
  PLAN = data; draw();
  return data;
}

/* ---------------------------------------------------------------- markdown-ish inline rendering */
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function inline(s) {
  return esc(s)
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => '<a href="' + esc(fileHref(url)) + '" target="_blank" rel="noopener">' + label + '</a>')
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
}
function fileHref(raw, action = true) {
  if (!raw) return "";
  if (/^(?:https?:|mailto:|#)/i.test(raw)) return raw;
  if (/^[a-z][a-z0-9+.-]*:|^\/\//i.test(raw)) return "";
  // Exported destinations already point into the hub's files directory.
  if (MODE !== "live" && raw.startsWith("../files/")) return raw;
  let path = raw;
  if (path.startsWith("/files/")) path = path.slice(7);
  else if (path.startsWith("/")) return "";
  else if (path.startsWith("./") || path.startsWith("../")) path = "study-plan/" + path;
  else if (action && !/^(study-plan\/|profile\/(context|stories)\/|reports\/)/.test(path)) path = "study-plan/" + path;
  const resolved = new URL(path, "https://study.invalid/");
  path = decodeURIComponent(resolved.pathname.slice(1));
  if (path.split("/").some(p => p.startsWith("."))) return "";
  const key = path + resolved.hash;
  if (MODE === "live") return "/files/" + path.split("/").map(encodeURIComponent).join("/") + resolved.hash;
  return HUB_FILES[key] || (HUB_FILES[path] ? HUB_FILES[path].split("#")[0] + resolved.hash : "");
}
function firstUrl(s) {
  const md = /\]\(([^)]+)\)/.exec(s); if (md) return fileHref(md[1]);
  const bare = /(https?:\/\/[^\s<)`]+)/.exec(s); if (bare) return bare[1];
  const local = /(?:\.{1,2}\/|study-plan\/|profile\/|docs\/|reports\/|exercises\/)[^\s`<>]+/.exec(s);
  return local ? fileHref(local[0].replace(/[.,;]+$/, "")) : "";
}
function nextAction(b) {
  if (b.done) return null;
  const item = b.items.find(i => !i.done && !isNA(i));
  if (b.items.length && !item) return null;
  if (item && item.session) return {item, url: sessionHref(b, item), text: item.first_action || "Open study step"};
  const explicit = item && (item.action_url || firstUrl(item.first_action || ""));
  const first = item === b.items[0];
  const url = (item && item.action_url ? fileHref(item.action_url) : explicit) ||
    (first ? firstUrl(b.first_action) : "") ||
    (item && (item.refs || []).map(r => fileHref(r, false)).find(Boolean)) ||
    firstUrl(b.first_action) || (b.links.length ? fileHref(b.links[0].url) : "");
  const text = item && item.first_action ? item.first_action :
    (first && b.first_action ? b.first_action : item ? item.text : b.first_action);
  return {item, url, text};
}
function sessionHref(b, item) {
  return MODE === "live" ? "/study?" + new URLSearchParams({date:DATE, block:b.id, item:item.id}) :
    "../sessions/" + encodeURIComponent(DATE) + "/" + encodeURIComponent(item.id) + ".html";
}

/* The artifacts behind an item: a URL opens in a new tab; a repo path is shown as the file (the hub site links it). */
function refNodes(refs) {
  const wrap = el("span", "refs");
  refs.forEach(r => {
    if (/^https?:\/\//.test(r)) {
      const a = el("a", null, "↗ " + r.replace(/^https?:\/\/(www\.)?/, "").split("/")[0]);
      a.href = r; a.target = "_blank"; a.rel = "noopener"; a.title = r; wrap.appendChild(a);
    } else {
      const label = r.split("#")[0].split("/").pop() + (r.includes("#") ? " › " + r.split("#")[1] : "");
      const link = fileHref(r, false);
      if (link) { const a = el("a", null, label); a.href = link; a.title = r; wrap.appendChild(a); }
      else { const c = el("code", null, label); c.title = r; wrap.appendChild(c); }
    }
  });
  return wrap;
}

/* ---------------------------------------------------------------- timers (per block, per tab) */
const timers = {};   // block id -> {left: seconds, running: bool, elapsed: seconds}
function timerOf(b) { if (!timers[b.id]) timers[b.id] = { left: b.minutes * 60, running: false, elapsed: 0 }; return timers[b.id]; }
function fmtSec(s) { const neg = s < 0, a = Math.abs(s); return (neg ? "+" : "") + String(Math.floor(a / 60)).padStart(2, "0") + ":" + String(a % 60).padStart(2, "0"); }
function tick() {
  for (const id in timers) {
    const t = timers[id];
    if (t.running) { t.left -= 1; t.elapsed += 1; }
    const out = document.querySelector('[data-timer="' + CSS.escape(id) + '"]');
    const b = PLAN.blocks.find(x => x.id === id);
    if (out) {
      if (b && b.done && t.elapsed) out.textContent = fmtSec(t.elapsed) + " spent";
      else out.textContent = fmtSec(t.left);
      out.classList.toggle("over", t.left < 0 && !(b && b.done));
    }
    const chip = document.getElementById("blk-" + id);
    if (chip) chip.classList.toggle("running", t.running);
  }
}
setInterval(tick, 1000);

/* ---------------------------------------------------------------- header */
let counted = false;
function count(node, end) {
  if (REDUCE || counted || !(end > 0)) { node.textContent = String(end); return; }
  let t0 = null; const dur = 620;
  requestAnimationFrame(function step(t) { if (t0 === null) t0 = t; const p = Math.min(1, (t - t0) / dur); node.textContent = Math.round(end * (1 - Math.pow(1 - p, 3))); if (p < 1) requestAnimationFrame(step); });
}
function drawHeader() {
  $("#date-h").textContent = fmtDate(DATE, true);
  document.title = "Study Planner AI · " + DATE;
  const total = PLAN.blocks.reduce((a, b) => a + b.minutes, 0);
  const doneMin = PLAN.blocks.filter(b => b.done).reduce((a, b) => a + b.minutes, 0);
  count($("#k-planned"), total); count($("#k-done"), doneMin); counted = true;
  $("#bar").style.width = (total ? Math.round(100 * doneMin / total) : 0) + "%";
  const call = $("#call"); call.textContent = PLAN.call || "";
  requestAnimationFrame(() => { $("#more").hidden = !(call.scrollWidth > call.clientWidth + 1 || $("#callrow").classList.contains("open")); });
  const allday = $("#allday"); allday.innerHTML = "";
  (PLAN.events || []).filter(e => e.all_day).forEach(e => {
    const c = el("span", "e"); c.appendChild(icon("cal"));
    if (e.url) { const a = el("a", null, e.title); a.href = e.url; a.target = "_blank"; a.rel = "noopener"; c.appendChild(a); }
    else c.appendChild(el("span", null, e.title));
    allday.appendChild(c);
  });
  const w = $("#warnings"); w.innerHTML = "";
  PLAN.warnings.forEach(x => { const d = el("div", "warn"); d.appendChild(icon("alert")); d.appendChild(el("span", null, x)); w.appendChild(d); });
  const radar = $("#m-radar"); radar.innerHTML = "";
  if (PLAN.radar.length) { radar.appendChild(el("div", "h", "Radar")); const ul = el("ul"); PLAN.radar.forEach(r => { const li = el("li"); li.innerHTML = inline(r); ul.appendChild(li); }); radar.appendChild(ul); }
  // date navigation
  const prev = $("#prev"), next = $("#next");
  if (MODE === "live") { prev.href = "/?date=" + shiftDate(DATE, -1); next.href = "/?date=" + shiftDate(DATE, 1); }
  else { prev.hidden = !HUB.prev; next.hidden = !HUB.next; if (HUB.prev) prev.href = HUB.prev; if (HUB.next) next.href = HUB.next; }
  // the ⋯ menu
  const mj = $("#m-jobs"), me = $("#m-events");
  mj.hidden = !REPORTS.jobs; if (REPORTS.jobs) mj.href = REPORTS.jobs.url;
  me.hidden = !REPORTS.events; if (REPORTS.events) me.href = REPORTS.events.url;
  const mjson = $("#m-json"); mjson.hidden = MODE !== "live"; mjson.href = "/api/plan?date=" + DATE;
  const mmd = $("#m-md"); const mdlink = HUB_FILES["study-plan/plans/" + DATE + ".md"]; mmd.hidden = !mdlink; if (mdlink) mmd.href = mdlink;
  $("#allhours").classList.toggle("on", state.showAll); $("#allhours").setAttribute("aria-pressed", String(state.showAll));
}

/* ---------------------------------------------------------------- the timeline */
function tlItems() {
  const out = [];
  PLAN.blocks.forEach(b => out.push({ start: mins(b.start), end: mins(b.start) + b.minutes }));
  (PLAN.events || []).filter(e => !e.all_day).forEach(e => out.push({ start: mins(e.start), end: mins(e.start) + e.minutes }));
  return out;
}
function chipsOf() {
  const list = [];
  (PLAN.events || []).filter(e => !e.all_day).forEach(e => list.push({ kind: "ev", id: e.id, start: mins(e.start), minutes: e.minutes, data: e }));
  PLAN.blocks.forEach(b => list.push({ kind: "blk", id: b.id, start: mins(b.start), minutes: b.minutes, data: b }));
  return list.sort((a, b) => a.start - b.start || a.minutes - b.minutes);
}
function drawTimeline() {
  const chips = $("#chips"); chips.innerHTML = "";
  chipsOf().forEach(c => chips.appendChild(c.kind === "ev" ? eventNode(c.data) : blockNode(c.data)));
  positionTimeline();
}
/* Lay the rows out for the space we have: the grid layer is rebuilt, the chips are repositioned in place. */
function positionTimeline() {
  const tl = $("#tl"), grid = $("#grid");
  const now = IS_TODAY ? nowMin() : null;
  const lay = T.layout(tlItems(), now, { showAll: state.showAll, expanded: state.expanded });
  const openMin = lay.rows.filter(r => r.kind === "open").reduce((a, r) => a + r.to - r.from, 0);
  const strips = lay.rows.length - lay.rows.filter(r => r.kind === "open").length;
  const avail = tl.clientHeight - 18 - strips * STRIP_H;
  // the open hours stretch to fill the column (never below 48 px an hour), so the timeline ends where the pane does
  const ppm = state.showAll ? 0.8 : Math.max(0.8, Math.min(3, openMin ? Math.floor(avail) / openMin : 0.8));
  geo = T.geometry(lay.rows, ppm, STRIP_H);
  $("#timeline").style.height = geo.height + "px";
  grid.innerHTML = "";
  geo.rows.forEach(r => {
    if (r.kind === "strip") {
      const s = el("div", "strip" + (state.hot === r.from ? " hot" : ""), "· · ·  " + T.hhmm(r.from) + "–" + T.hhmm(r.to) + " · " + T.hours(r.to - r.from) + "  · · ·");
      s.style.top = r.y + "px"; s.dataset.from = r.from; s.dataset.to = r.to; s.title = "click to show these hours";
      s.addEventListener("click", () => { state.expanded.push([r.from, r.to]); positionTimeline(); });
      grid.appendChild(s);
      return;
    }
    for (let m = r.from; m <= r.to; m += T.SLOT) {
      if (m === r.to && m !== 24 * 60) continue;
      const y = r.y + (m - r.from) * ppm;
      const line = el("div", "hr" + (m % 60 ? " half" : "")); line.style.top = y + "px"; grid.appendChild(line);
      if (m % 60 === 0) { const lab = el("div", "hl", T.hhmm(m)); lab.style.top = y + "px"; grid.appendChild(lab); }
    }
  });
  state.expanded.forEach((x, i) => {
    const f = el("button", "fold", "collapse " + T.hhmm(x[0]) + "–" + T.hhmm(x[1]));
    f.style.top = (T.yOf(geo, x[1]) - 2) + "px"; f.type = "button";
    f.addEventListener("click", () => { state.expanded.splice(i, 1); positionTimeline(); });
    grid.appendChild(f);
  });
  if (now != null) { const n = el("div"); n.id = "now"; n.style.top = T.yOf(geo, now) + "px"; grid.appendChild(n); }
  // chips: proportional height with a floor; a short chip whose title still fits stops at the next chip's top
  // instead of running into it, and only true overlaps go side by side
  const nodes = Array.from($("#chips").children);
  const boxes = nodes.map(n => { const top = T.yOf(geo, +n.dataset.start), h = Math.max(MIN_CHIP, T.yOf(geo, +n.dataset.end) - top); return { top, bottom: top + h }; });
  const tops = boxes.map(b => b.top).sort((a, b) => a - b);
  boxes.forEach(b => {
    const next = tops.find(t => t > b.top);
    if (next !== undefined && b.bottom > next && next - b.top >= TITLE_ONLY) b.bottom = next;
  });
  const cols = T.columns(boxes);
  const W = tl.clientWidth - GUTTER - RIGHT;
  nodes.forEach((n, i) => {
    const b = boxes[i], c = cols[i], w = W / c.cols;
    n.style.top = b.top + "px"; n.style.height = (b.bottom - b.top) + "px";
    n.style.left = (GUTTER + c.col * w) + "px"; n.style.width = (w - (c.cols > 1 ? 3 : 0)) + "px";
    n.classList.toggle("tiny", b.bottom - b.top < TINY);
  });
}
function chipNode(kind, id, hue, start, end) {
  const n = el("div", "tc " + kind);
  n.id = (kind === "ev" ? "ev-node-" : "blk-") + id;
  n.dataset[kind === "ev" ? "event" : "block"] = id;
  n.dataset.start = start; n.dataset.end = end;
  n.style.setProperty("--h", hue);
  n.tabIndex = 0;
  n.classList.toggle("sel", state.sel === id);
  n.setAttribute("aria-selected", String(state.sel === id));
  return n;
}
function eventNode(e) {
  const n = chipNode("ev", e.id, 262, mins(e.start), mins(e.start) + e.minutes);
  if (!e.busy) n.classList.add("free");
  const txt = el("div", "txt");
  const t = el("div", "t"); t.appendChild(icon("cal")); t.appendChild(el("span", null, e.title)); txt.appendChild(t);
  txt.appendChild(el("div", "m", e.start + "–" + e.end + " · " + e.minutes + " min" + (e.busy ? "" : " · free")));
  n.appendChild(txt);
  n.title = e.title + "\n" + e.start + "–" + e.end + (e.location ? "\n" + e.location : "") + (e.calendar ? "\n" + e.calendar : "");
  n.addEventListener("click", () => select(e.id));
  n.addEventListener("keydown", chipKeys);
  return n;
}
function blockNode(b) {
  const n = chipNode("blk", b.id, laneOf(b.id), mins(b.start), mins(b.start) + b.minutes);
  const done = b.items.filter(i => i.done).length;
  const overdue = IS_TODAY && !resolved(b) && mins(b.start) + b.minutes < nowMin();
  if (b.done) n.classList.add("done");
  if (overdue) n.classList.add("over");
  if (timers[b.id] && timers[b.id].running) n.classList.add("running");
  const txt = el("div", "txt");
  const t = el("div", "t"); if (b.done) t.appendChild(icon("check")); t.appendChild(el("span", null, b.title)); txt.appendChild(t);
  txt.appendChild(el("div", "m", b.start + "–" + b.end + " · " + b.minutes + " min" + (b.items.length ? " · " + itemSummary(b) : "") + (overdue ? " · overdue" : "")));
  n.appendChild(txt);
  n.title = b.title + "\n" + b.start + "–" + b.end + " · " + b.minutes + " min" + (b.items.length ? " · " + itemSummary(b) : "");
  const rz = el("div", "rz"); n.appendChild(rz);
  n.addEventListener("pointerdown", e => { if (e.target === rz) startDrag(e, n, b, "resize"); else startDrag(e, n, b, "move"); });
  n.addEventListener("keydown", chipKeys);
  return n;
}
function chipKeys(e) {
  const n = e.currentTarget, id = n.dataset.block || n.dataset.event;
  if (e.key === "Enter") { e.preventDefault(); select(id); $("#tab-study").focus({ preventScroll: true }); return; }
  if (e.key === " ") { e.preventDefault(); if (n.dataset.block) { select(id); tickNext(id); } return; }
  if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    e.preventDefault();
    const all = Array.from($("#chips").children).sort((a, b) => (+a.dataset.start) - (+b.dataset.start));
    const i = all.indexOf(n), j = Math.max(0, Math.min(all.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)));
    const m = all[j]; select(m.dataset.block || m.dataset.event); m.focus({ preventScroll: true }); m.scrollIntoView({ block: "nearest" });
  }
}
function tickNext(id) {
  const b = PLAN.blocks.find(x => x.id === id); if (!b) return;
  const it = b.items.find(i => !i.done && !isNA(i));
  if (!it) { toast("没有待做项"); return; }
  const streakInfo = handleItemDone(it.id);
  fxHeroCelebrate(it.text, streakInfo, false);
  post("/api/done", { block: b.id, item: it.id }).then(() => toast("logged: " + it.text.slice(0, 40))).catch(() => {});
}

/* ---------------------------------------------------------------- drag and resize, with strips that open on hover */
function startDrag(e, node, b, mode) {
  if (e.button !== 0 && e.pointerType === "mouse") return;
  if (READONLY) { if (mode === "move") select(b.id); return; }
  e.preventDefault();
  try { node.setPointerCapture(e.pointerId); } catch (_) { /* synthetic pointers have no capture */ }
  const tl = $("#tl");
  const y0 = e.clientY, start0 = mins(b.start), min0 = b.minutes;
  const grab = y0 - (node.getBoundingClientRect().top);                 // where in the chip the pointer took hold
  let moved = false, start = start0, minutes = min0, hover = null, hoverTimer = null;
  const meta = node.querySelector(".m");
  const tlY = clientY => clientY - tl.getBoundingClientRect().top + tl.scrollTop - $("#timeline").offsetTop;
  const move = ev => {
    if (Math.abs(ev.clientY - y0) > 3) { moved = true; node.classList.add("dragging"); }
    if (!moved) return;
    const at = T.minuteAt(geo, tlY(ev.clientY) - (mode === "move" ? grab : 0));
    if (at.row.kind === "strip") {
      if (hover !== at.row.from) {
        hover = at.row.from; clearTimeout(hoverTimer); state.hot = at.row.from; positionTimeline();
        hoverTimer = setTimeout(() => { state.expanded.push([at.row.from, at.row.to]); state.hot = null; positionTimeline(); }, 400);
      }
      return;
    }
    if (hover !== null) { hover = null; clearTimeout(hoverTimer); state.hot = null; positionTimeline(); }
    if (mode === "move") { start = Math.max(0, Math.min(24 * 60 - minutes, snap(at.minute))); node.style.top = T.yOf(geo, start) + "px"; }
    else { minutes = Math.max(SNAP, snap(at.minute - start0)); node.style.height = Math.max(MIN_CHIP, T.yOf(geo, start0 + minutes) - T.yOf(geo, start0)) + "px"; }
    if (meta) meta.textContent = hhmm(start) + "–" + hhmm(start + minutes) + " · " + minutes + " min";
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    node.classList.remove("dragging");
    clearTimeout(hoverTimer); state.hot = null;
    if (state.expanded.length) {                                        // a strip opened for this drag closes again if nothing landed
      clearTimeout(state.foldTimer);
      state.foldTimer = setTimeout(() => { state.expanded = []; positionTimeline(); }, 5000);
    }
    if (start === start0 && minutes === min0) { if (!moved && mode === "move") select(b.id); else positionTimeline(); return; }
    post("/api/block", { block: b.id, start: hhmm(start), minutes }).then(() => toast(b.id + " → " + hhmm(start) + " · " + minutes + " min")).catch(() => {});
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
}

/* ---------------------------------------------------------------- selection, tabs, hash */
function select(id) {
  state.sel = id; state.tab = "study";
  Array.from($("#chips").children).forEach(n => { const on = (n.dataset.block || n.dataset.event) === id; n.classList.toggle("sel", on); n.setAttribute("aria-selected", String(on)); });
  setHash(id);
  drawPane();
}
function clearSel() { state.sel = null; select(null); }
function setHash(v) {
  const url = location.pathname + location.search + (v ? "#" + v : "");
  if (url !== location.pathname + location.search + location.hash) history.replaceState(null, "", url);
}
function readHash() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (!h) return;
  if (h === "jobs" || h === "events") { state.tab = h; return; }
  const id = h.replace(/^blk-/, "");
  if (PLAN.blocks.some(b => b.id === id) || (PLAN.events || []).some(e => e.id === id)) { state.sel = id; state.tab = "study"; }
}
function setTab(t) {
  state.tab = t;
  if (t !== "study") setHash(t); else setHash(state.sel);
  drawPane();
}
function drawPane() {
  document.querySelectorAll(".tab").forEach(b => { b.classList.toggle("on", b.dataset.tab === state.tab); b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)); });
  document.querySelectorAll(".tb").forEach(s => s.classList.toggle("on", s.id === "tab-" + state.tab));
  const out = $("#tab-out"), rep = state.tab === "study" ? null : REPORTS[state.tab];
  out.classList.toggle("on", !!rep); if (rep) out.href = rep.url;
  $("#pane").classList.toggle("open", state.tab !== "study" || !!state.sel);
  if (state.tab === "study") drawStudy(); else drawReport(state.tab);
}
function drawReport(kind) {
  const sec = $("#tab-" + kind), head = $("#" + kind + "-head"), rep = REPORTS[kind];
  const label = kind === "jobs" ? "Job report" : "Event report";
  head.innerHTML = "";
  if (!rep) {
    head.appendChild(el("span", null, "No " + (kind === "jobs" ? "job" : "event") + " report was rendered on " + DATE + "."));
    if (!sec.querySelector(".none")) {
      const p = el("div", "none"); p.appendChild(el("span", null, "Nothing on or before this day. "));
      const a = el("a", null, "The report folder on GitHub ↗"); a.href = REPORTS.folders[kind]; a.target = "_blank"; a.rel = "noopener"; p.appendChild(a);
      sec.appendChild(p);
    }
    return;
  }
  if (rep.date === DATE) head.appendChild(el("span", null, label + " · " + fmtDate(rep.date, true)));
  else { head.appendChild(el("span", "old", "Latest report: " + fmtDate(rep.date))); head.appendChild(el("span", null, "none on " + fmtDate(DATE))); }
  const a = el("a"); a.href = rep.url; a.target = "_blank"; a.rel = "noopener"; a.appendChild(el("span", null, "open in its own tab")); a.appendChild(icon("out")); head.appendChild(a);
  let frame = sec.querySelector("iframe");
  if (!frame) { frame = el("iframe"); frame.id = "frame-" + kind; frame.title = label; frame.src = rep.url; sec.appendChild(frame); }   // loaded once, on first view
}

/* ---------------------------------------------------------------- the study pane */
function reveal(node) {
  if (REDUCE) { node.classList.add("in"); return; }
  requestAnimationFrame(() => requestAnimationFrame(() => node.classList.add("in")));
  setTimeout(() => node.classList.add("in"), 700);                                  // never parked invisible
}
function drawStudy() {
  const sec = $("#tab-study"); sec.innerHTML = "";
  const b = PLAN.blocks.find(x => x.id === state.sel);
  const e = b ? null : (PLAN.events || []).find(x => x.id === state.sel);
  const box = el("div", "det");
  if (b) blockDetail(box, b); else if (e) eventDetail(box, e); else summary(box);
  sec.appendChild(box);
  tick();                                                                          // the timer reads its node once attached
  reveal(box);
}
function descNodes(lines) {
  const wrap = el("div", "desc");
  let fence = null;
  lines.forEach(line => {
    if (line.startsWith("```")) {
      if (fence) { wrap.appendChild(codeNode(fence.join("\n"))); fence = null; } else fence = [];
      return;
    }
    if (fence) { fence.push(line); return; }
    if (!line.trim()) return;
    const p = el("p"); p.innerHTML = inline(line); wrap.appendChild(p);
  });
  if (fence) wrap.appendChild(codeNode(fence.join("\n")));
  return wrap;
}
function codeNode(text) {
  const c = el("div", "code"); const pre = el("pre"); pre.textContent = text; c.appendChild(pre);
  const cp = el("button", "cp"); cp.type = "button"; cp.appendChild(icon("copy")); cp.appendChild(el("span", null, "copy"));
  cp.addEventListener("click", () => copy(text, "the code")); c.appendChild(cp);
  return c;
}
function blockDetail(box, b) {
  const done = b.items.filter(i => i.done).length;
  const dh = el("div", "dh"); dh.style.setProperty("--h", laneOf(b.id));
  dh.appendChild(el("h2", null, b.title));
  const meta = el("div", "meta");
  meta.appendChild(el("span", null, b.start + "–" + b.end + " · " + b.minutes + " min" + (b.items.length ? " · " + itemSummary(b) : "") + (b.done ? " · done" : "")));
  const t = timerOf(b);
  const timer = el("span", "timer");
  const pb = el("button", "ib"); pb.type = "button"; pb.title = t.running ? "pause" : "start the timer"; pb.setAttribute("aria-label", pb.title); pb.appendChild(icon(t.running ? "pause" : "play"));
  pb.addEventListener("click", () => { t.running = !t.running; drawStudy(); tick(); });
  const out = el("span", "t"); out.dataset.timer = b.id;
  const rb = el("button", "ib"); rb.type = "button"; rb.title = "reset"; rb.setAttribute("aria-label", "reset the timer"); rb.appendChild(icon("reset"));
  rb.addEventListener("click", () => { timers[b.id] = { left: b.minutes * 60, running: false, elapsed: 0 }; drawStudy(); tick(); });
  timer.appendChild(pb); timer.appendChild(out); timer.appendChild(rb);
  meta.appendChild(timer);
  dh.appendChild(meta); box.appendChild(dh);
  const action = nextAction(b);
  if (action) {
    const a = el(action.url ? "a" : "div", action.url ? "first" : "action-note"); a.id = "first";
    if (action.url) { a.href = action.url; a.target = "_blank"; a.rel = "noopener"; }
    a.dataset.item = action.item ? action.item.id : "";
    a.appendChild(icon("go"));
    const label = el("span"); label.innerHTML = "Next action · " + inline(action.text).replace(/<a [^>]*>|<\/a>/g, "");
    a.appendChild(label);
    if (action.item) a.appendChild(el("small", null, "Step " + (b.items.indexOf(action.item) + 1) + " of " + b.items.length));
    box.appendChild(a);
    if (!action.url) box.appendChild(el("p", "muted", "Do this step here; no document is attached."));
  } else if (b.items.length) {
    const finished = el("p", "action-note", b.done ? "Block completed." : "All steps resolved. Record the block when you are ready.");
    finished.id = "actions-complete"; box.appendChild(finished);
  }
  if (b.description.length) box.appendChild(descNodes(b.description));
  if (b.items.length) {
    const ul = el("ul", "items");
    b.items.forEach(it => {
      const li = el("li", it.done ? "on" : action && action.item === it ? "current-item" : null);
      if (action && action.item === it) li.setAttribute("aria-current", "step");
      const cb = el("input"); cb.type = "checkbox"; cb.checked = it.done; cb.id = "it-" + it.id; cb.disabled = READONLY || isNA(it);
      cb.addEventListener("change", () => {
        if (cb.checked) {
          const streakInfo = handleItemDone(it.id);
          fxHeroCelebrate(it.text, streakInfo, false);
          post("/api/done", { block: b.id, item: it.id })
            .then(() => toast("logged: " + it.text.slice(0, 40)))
            .catch(() => {});
        } else {
          post("/api/undo", { block: b.id, item: it.id })
            .then(() => toast("undone"))
            .catch(() => {});
        }
      });
      const lab = el("label"); lab.htmlFor = cb.id; lab.style.flex = "1"; lab.style.cursor = READONLY ? "default" : "pointer";
      const sp = el("span"); sp.innerHTML = inline(it.text);
      if (!it.session && it.refs && it.refs.length) sp.appendChild(refNodes(it.refs));
      lab.appendChild(sp);
      if (it.first_action || it.action_url) {
        const target = it.session ? sessionHref(b, it) : fileHref(it.action_url) || firstUrl(it.first_action || "");
        if (target) {
          const open = el("a", "item-action", it.first_action || "Open this step");
          open.href = target; open.target = "_blank"; open.rel = "noopener";
          lab.appendChild(open);
        }
      }
      if (isNA(it)) {
        lab.prepend(el("strong", "muted", "暂不做 / N/A · "));
        lab.appendChild(el("div", "muted", it.decisions[it.decisions.length - 1].reason));
      }
      const decision = el("button", "pill", isNA(it) ? "恢复待做" : "暂不做");
      decision.type = "button"; decision.disabled = READONLY;
      decision.addEventListener("click", () => {
        const reason = window.prompt(isNA(it) ? "为什么重新安排这项？" : "为什么暂不做？保留原因，帮助以后排计划。");
        if (!reason || !reason.trim()) return;
        post("/api/item-decision", {block: b.id, item: it.id, state: isNA(it) ? "applicable" : "not_applicable", reason: reason.trim()}).catch(() => {});
      });
      lab.appendChild(decision);
      li.appendChild(cb); li.appendChild(lab); ul.appendChild(li);
    });
    box.appendChild(ul);
  }
  if (b.links.length) {
    const focused = b.items.some(i => i.session);
    const links = el("div", "links");
    b.links.forEach(l => { const a = el("a", "pill"); a.href = fileHref(l.url); a.target = "_blank"; a.rel = "noopener"; a.appendChild(icon("out")); a.appendChild(el("span", null, focused ? l.label.replace(/^Required today: /, "Archive: ") : l.label)); links.appendChild(a); });
    if (focused) { const archive = el("details"); archive.appendChild(el("summary", "muted", "Full packet · reference archive")); archive.appendChild(links); box.appendChild(archive); }
    else box.appendChild(links);
  }
  const tools = el("div", "tools");
  const doneBtn = el("button", "btn" + (b.done ? " on" : "")); doneBtn.type = "button"; doneBtn.id = "done-btn"; doneBtn.disabled = READONLY;
  doneBtn.appendChild(icon("check")); doneBtn.appendChild(el("span", null, b.done ? "Done" : "Mark block done"));
  doneBtn.addEventListener("click", () => {
    if (b.done) {
      post("/api/undo", { block: b.id })
        .then(() => toast("block reopened"))
        .catch(() => {});
    } else {
      let minutes = b.minutes;
      if (b.items.some(isNA)) {
        const answer = window.prompt("包含暂不做项：实际学习了多少分钟？", "0");
        if (answer === null) return;
        minutes = Number(answer);
        if (!answer.trim() || !Number.isInteger(minutes) || minutes < 0) { toast("请输入非负整数分钟", true); return; }
      }
      const streakInfo = handleItemDone(b.id);
      fxSingularitySupernova(b.title, b.minutes, streakInfo, doneBtn);
      post("/api/done", { block: b.id, minutes })
        .then(() => toast("logged " + minutes + " min of " + b.id))
        .catch(() => {});
    }
  });
  tools.appendChild(doneBtn);
  if (b.gcal) { const a = el("a", "btn"); a.href = b.gcal; a.target = "_blank"; a.rel = "noopener"; a.appendChild(icon("cal")); a.appendChild(el("span", null, "Add to Google Calendar")); tools.appendChild(a); }
  const form = el("form"); form.id = "move-form";
  form.appendChild(el("span", null, "Move"));
  const start = el("input"); start.type = "time"; start.step = SNAP * 60; start.value = b.start; start.setAttribute("aria-label", "start time for " + b.title);
  const dur = el("input"); dur.type = "number"; dur.step = SNAP; dur.min = SNAP; dur.value = b.minutes; dur.setAttribute("aria-label", "minutes for " + b.title);
  const apply = el("button", "btn", "Apply"); apply.type = "submit"; apply.disabled = READONLY;
  form.appendChild(start); form.appendChild(dur); form.appendChild(el("span", null, "min")); form.appendChild(apply);
  form.addEventListener("submit", ev => { ev.preventDefault(); post("/api/block", { block: b.id, start: start.value, minutes: Number(dur.value) }).then(() => toast("moved " + b.id)).catch(() => {}); });
  tools.appendChild(form);
  box.appendChild(tools);
  if (b.log_hint) {
    const h = el("div", "hint"); h.appendChild(el("span", null, "Log:")); const c = el("code", null, b.log_hint); c.title = b.log_hint; h.appendChild(c);
    const cp = el("button", "cp"); cp.type = "button"; cp.appendChild(icon("copy")); cp.appendChild(el("span", null, "copy")); cp.addEventListener("click", () => copy(b.log_hint, "the log command")); h.appendChild(cp);
    box.appendChild(h);
  }
}
function eventDetail(box, e) {
  const dh = el("div", "dh"); dh.style.setProperty("--h", 262); dh.style.borderLeftColor = "var(--faint)";
  dh.appendChild(el("h2", null, e.title));
  const meta = el("div", "meta");
  meta.appendChild(el("span", null, (e.all_day ? "all day" : e.start + "–" + e.end + " · " + e.minutes + " min") + (e.busy ? "" : " · free")));
  if (e.calendar) meta.appendChild(el("span", null, e.calendar));
  dh.appendChild(meta); box.appendChild(dh);
  if (e.location) { const p = el("p", "muted"); p.appendChild(icon("pin")); p.appendChild(el("span", null, " " + e.location)); box.appendChild(p); }
  const links = el("div", "links");
  if (e.url) { const a = el("a", "pill"); a.href = e.url; a.target = "_blank"; a.rel = "noopener"; a.appendChild(icon("out")); a.appendChild(el("span", null, "Open in the calendar")); links.appendChild(a); }
  box.appendChild(links);
  const p = el("p", "muted", "A calendar event: read-only here, the study blocks are laid around it. To prepare for it as a block: uv run studyplan plan promote-event " + e.id);
  box.appendChild(p);
}
function summary(box) {
  box.classList.add("empty");
  const total = PLAN.blocks.reduce((a, b) => a + b.minutes, 0), doneMin = PLAN.blocks.filter(b => b.done).reduce((a, b) => a + b.minutes, 0);
  box.appendChild(el("h2", null, fmtDate(DATE, true) + " · " + total + " min planned · " + Math.ceil(PLAN.blocks.reduce((sum, b) => sum + remainingMinutes(b), 0)) + " min left"));
  const now = IS_TODAY ? nowMin() : -1;
  const byTime = PLAN.blocks.slice().sort((a, b) => mins(a.start) - mins(b.start));
  const next = byTime.find(b => !resolved(b) && mins(b.start) + b.minutes > now) || byTime.find(b => !resolved(b));
  if (next) {
    box.appendChild(el("h3", null, "Next"));
    const card = el("div", "next"); card.tabIndex = 0; card.setAttribute("role", "button");
    card.appendChild(el("b", null, next.title));
    card.appendChild(el("span", "sub", next.start + "–" + next.end + " · " + next.minutes + " min"));
    const upcoming = nextAction(next);
    if (upcoming) { const s = el("span", "sub"); s.innerHTML = "Next action · " + inline(upcoming.text); card.appendChild(s); }
    const open = () => { select(next.id); $("#blk-" + CSS.escape(next.id)) && $("#blk-" + CSS.escape(next.id)).scrollIntoView({ block: "nearest" }); };
    card.addEventListener("click", open); card.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } });
    box.appendChild(card);
  }
  box.appendChild(el("h3", null, "Lanes"));
  const lanes = el("div", "lanes");
  PLAN.blocks.forEach(b => {
    const n = el("span", "n", b.title); lanes.appendChild(n);
    const bar = el("span", "bar"); bar.style.setProperty("--h", laneOf(b.id)); const i = el("i"); i.style.width = (b.done ? 100 : (b.items.length ? Math.round(100 * activeItems(b).filter(x => x.done).length / Math.max(1, activeItems(b).length)) : 0)) + "%"; bar.appendChild(i); lanes.appendChild(bar);
    lanes.appendChild(el("span", "v", b.minutes + " min" + (b.done ? " · done" : (b.items.length ? " · " + itemSummary(b) : ""))));
  });
  box.appendChild(lanes);
  if (PLAN.radar.length) { box.appendChild(el("h3", null, "Radar")); const ul = el("ul"); PLAN.radar.forEach(r => { const li = el("li"); li.innerHTML = inline(r); ul.appendChild(li); }); box.appendChild(ul); }
  box.appendChild(el("h3", null, "Keys"));
  box.appendChild(el("p", "muted", "Click a chip, or ↑ ↓ to move between them; Enter opens, Space ticks the next item, Esc comes back here. Drag a chip to move it, its bottom edge to resize."));
}

/* ---------------------------------------------------------------- celebration effects (procedural audio, kinetic recoil, floating praise & particles) */
const FX_COLORS = ["#38bdf8", "#34d399", "#fbbf24", "#f472b6", "#a78bfa", "#facc15"];
const FX_BURSTS = [
  ["#ffd166", "#f4a261", "#fff3b0"],
  ["#38bdf8", "#34d399", "#a7f3d0"],
  ["#f472b6", "#c084fc", "#fbcfe8"],
  ["#facc15", "#fb923c", "#fef08a"]
];
const FX_OFF = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const fx = { cv: null, ctx: null, parts: [], raf: 0 };

let audioCtx = null;
function fxAudio(type = "item", pitchShift = 1.0) {
  if (FX_OFF) return;
  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return;
    if (!audioCtx) audioCtx = new AudioCtx();
    if (audioCtx.state === "suspended") audioCtx.resume();
    const t = audioCtx.currentTime;

    if (type === "item") {
      const sub = audioCtx.createOscillator(), subG = audioCtx.createGain();
      sub.type = "sine";
      sub.frequency.setValueAtTime(150, t);
      sub.frequency.exponentialRampToValueAtTime(40, t + 0.08);
      subG.gain.setValueAtTime(0.18, t);
      subG.gain.exponentialRampToValueAtTime(0.001, t + 0.08);
      sub.connect(subG); subG.connect(audioCtx.destination);
      sub.start(t); sub.stop(t + 0.08);

      const baseNotes = [523.25, 659.25, 783.99, 1046.50];
      baseNotes.forEach((f, i) => {
        const nt = t + i * 0.05;
        const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
        osc.type = "triangle";
        osc.frequency.setValueAtTime(f * pitchShift, nt);
        g.gain.setValueAtTime(0, nt);
        g.gain.linearRampToValueAtTime(0.11, nt + 0.012);
        g.gain.exponentialRampToValueAtTime(0.001, nt + 0.38);
        osc.connect(g); g.connect(audioCtx.destination);
        osc.start(nt); osc.stop(nt + 0.38);
      });
    } else if (type === "cyber") {
      for (let k = 0; k < 6; k++) {
        const ct = t + k * 0.045;
        const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
        osc.type = "sawtooth";
        const freq = 1200 + ((k * 373) % 1400);
        osc.frequency.setValueAtTime(freq, ct);
        osc.frequency.exponentialRampToValueAtTime(freq * 0.7, ct + 0.035);
        g.gain.setValueAtTime(0.05, ct);
        g.gain.exponentialRampToValueAtTime(0.001, ct + 0.035);
        osc.connect(g); g.connect(audioCtx.destination);
        osc.start(ct); osc.stop(ct + 0.035);
      }
    } else if (type === "block") {
      const chords = [
        { f: 261.63, d: 0.0, len: 0.5 },
        { f: 329.63, d: 0.05, len: 0.5 },
        { f: 392.00, d: 0.10, len: 0.55 },
        { f: 523.25, d: 0.16, len: 0.65 },
        { f: 659.25, d: 0.22, len: 0.75 },
        { f: 1046.50, d: 0.28, len: 0.95 }
      ];
      chords.forEach(c => {
        const nt = t + c.d;
        const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
        osc.type = "triangle";
        osc.frequency.setValueAtTime(c.f, nt);
        g.gain.setValueAtTime(0, nt);
        g.gain.linearRampToValueAtTime(0.14, nt + 0.015);
        g.gain.exponentialRampToValueAtTime(0.001, nt + c.len);
        osc.connect(g); g.connect(audioCtx.destination);
        osc.start(nt); osc.stop(nt + c.len);
      });
    } else if (type === "singularity_suck") {
      const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(280, t);
      osc.frequency.exponentialRampToValueAtTime(34, t + 0.48);
      g.gain.setValueAtTime(0.01, t);
      g.gain.linearRampToValueAtTime(0.28, t + 0.38);
      g.gain.exponentialRampToValueAtTime(0.001, t + 0.48);
      osc.connect(g); g.connect(audioCtx.destination);
      osc.start(t); osc.stop(t + 0.48);
    } else if (type === "supernova_drop") {
      const sub = audioCtx.createOscillator(), subG = audioCtx.createGain();
      sub.type = "sine";
      sub.frequency.setValueAtTime(135, t);
      sub.frequency.exponentialRampToValueAtTime(28, t + 1.1);
      subG.gain.setValueAtTime(0.38, t);
      subG.gain.exponentialRampToValueAtTime(0.001, t + 1.1);
      sub.connect(subG); subG.connect(audioCtx.destination);
      sub.start(t); sub.stop(t + 1.1);

      const snap = audioCtx.createOscillator(), snapG = audioCtx.createGain();
      snap.type = "triangle";
      snap.frequency.setValueAtTime(920, t);
      snap.frequency.exponentialRampToValueAtTime(110, t + 0.09);
      snapG.gain.setValueAtTime(0.22, t);
      snapG.gain.exponentialRampToValueAtTime(0.001, t + 0.09);
      snap.connect(snapG); snapG.connect(audioCtx.destination);
      snap.start(t); snap.stop(t + 0.09);

      const chords = [
        { f: 196.00, d: 0.02, len: 0.8 },
        { f: 261.63, d: 0.05, len: 0.9 },
        { f: 329.63, d: 0.09, len: 0.9 },
        { f: 392.00, d: 0.13, len: 1.0 },
        { f: 523.25, d: 0.18, len: 1.2 },
        { f: 659.25, d: 0.24, len: 1.3 },
        { f: 1046.50, d: 0.30, len: 1.5 }
      ];
      chords.forEach(c => {
        const nt = t + c.d;
        const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
        osc.type = "triangle";
        osc.frequency.setValueAtTime(c.f, nt);
        g.gain.setValueAtTime(0, nt);
        g.gain.linearRampToValueAtTime(0.13, nt + 0.015);
        g.gain.exponentialRampToValueAtTime(0.001, nt + c.len);
        osc.connect(g); g.connect(audioCtx.destination);
        osc.start(nt); osc.stop(nt + c.len);
      });
    } else if (type === "sparkle") {
      const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(1400 + Math.random() * 400, t);
      osc.frequency.exponentialRampToValueAtTime(700, t + 0.14);
      g.gain.setValueAtTime(0.07, t);
      g.gain.exponentialRampToValueAtTime(0.001, t + 0.14);
      osc.connect(g); g.connect(audioCtx.destination);
      osc.start(t); osc.stop(t + 0.14);
    }
  } catch (_) { /* audio policy or headless browser: fail silently */ }
}

function fxShake(elm) {
  if (FX_OFF) return;
  const target = elm || document.querySelector(".board") || document.body;
  target.classList.remove("fx-shake");
  void target.offsetWidth;
  target.classList.add("fx-shake");
  setTimeout(() => target.classList.remove("fx-shake"), 240);
}

// Unique items completed today - prevents toggling streak exploits
const completedToday = new Set();
let dailyStreak = 0;
let currentTrackedDay = null;

function initCompletedTracker() {
  const dateKey = typeof DATE !== "undefined" && DATE ? DATE : (PLAN && PLAN.day ? PLAN.day : "today");
  if (currentTrackedDay === dateKey) return;
  currentTrackedDay = dateKey;
  completedToday.clear();
  try {
    const saved = JSON.parse(localStorage.getItem("study_completed_" + dateKey) || "[]");
    saved.forEach(id => completedToday.add(id));
  } catch (_) {}
  if (PLAN && PLAN.blocks) {
    PLAN.blocks.forEach(b => {
      if (b.items) b.items.forEach(it => { if (it.done) completedToday.add(it.id); });
      if (b.done) completedToday.add(b.id);
    });
  }
  try {
    const savedStreak = sessionStorage.getItem("study_streak_" + dateKey);
    dailyStreak = savedStreak !== null ? (parseInt(savedStreak, 10) || 0) : 0;
  } catch (_) {}
}

function handleItemDone(itemId) {
  initCompletedTracker();
  const isFirstTimeToday = !completedToday.has(itemId);
  if (isFirstTimeToday) {
    completedToday.add(itemId);
    dailyStreak++;
    const dateKey = typeof DATE !== "undefined" && DATE ? DATE : (PLAN && PLAN.day ? PLAN.day : "today");
    try {
      localStorage.setItem("study_completed_" + dateKey, JSON.stringify(Array.from(completedToday)));
      sessionStorage.setItem("study_streak_" + dateKey, String(dailyStreak));
    } catch (_) {}
  }
  return { isFirstTimeToday, streak: dailyStreak };
}

const RPG_PHRASES = [
  "+150 INTELLECT 🧠",
  "KNOWLEDGE SECURED ⚡",
  "NEURONS ACTIVATED ✨",
  "DEVOURED! 🔥",
  "CORTEX UPGRADED 💾",
  "SYNAPSES OVERCLOCKED 🚀",
  "SYSTEM DESIGN +1 LVL 📈",
  "BRAIN EXPANDED 🌌",
  "DEEP DIVE COMPLETE 🎯",
  "ALGORITHM CONQUERED ⚔️",
  "NEURAL PATHWAY FORMED 🔮",
  "WISDOM BANKED 💎",
  "MENTAL MUSCLE +1 💪",
  "CRITICAL INSIGHT! 💥",
  "100% INGESTED 🧬"
];

const BLOCK_PHRASES = [
  "BLOCK CONQUERED! 🏆",
  "MASTERED & BANKED! ⚡",
  "MAXIMUM OVERDRIVE! 🚀",
  "DEEP WORK COMPLETED! 🎯",
  "CORTEX DOMAIN SECURED! 🧠",
  "FEVER MODE UNLOCKED! 🔥"
];

function fxCanvas() {
  if (fx.cv) return fx.cv;
  const c = document.createElement("canvas"); c.id = "fx";
  document.body.appendChild(c); fx.cv = c; fx.ctx = c.getContext("2d");
  const fit = () => { c.width = innerWidth; c.height = innerHeight; };
  fit(); addEventListener("resize", fit);
  return c;
}

function fxStep() {
  const ctx = fx.ctx, cv = fx.cv;
  ctx.clearRect(0, 0, cv.width, cv.height);
  const out = [];
  for (const p of fx.parts) {
    if (p.kind === "collapse_ring") {
      p.r += p.vr; p.life--;
      if (p.r > 2 && p.life > 0) out.push(p);
      continue;
    }
    if (p.kind === "suck") {
      p.x += p.vx; p.y += p.vy; p.life--;
      if (p.life > 0) out.push(p);
      continue;
    }
    if (p.kind === "ring") {
      p.r += p.vr; p.life--;
      if (p.r < p.maxR && p.life > 0) out.push(p);
      continue;
    }
    if (p.kind === "fire") {
      p.x += Math.sin(p.life * 0.18) * 0.6;
      p.y += p.vy;
      p.life--;
      if (p.life > 0 && p.y > 0) out.push(p);
      continue;
    }
    p.vy += 0.22; p.vx *= 0.99; p.x += p.vx; p.y += p.vy; p.r += p.vr; p.life--;
    if (p.life > 0 && p.y < cv.height + 20) out.push(p);
  }
  fx.parts = out;
  for (const p of fx.parts) {
    if (p.kind === "collapse_ring") {
      ctx.save(); ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(1, p.r), 0, Math.PI * 2);
      ctx.strokeStyle = p.c; ctx.lineWidth = 3; ctx.shadowColor = "#38bdf8"; ctx.shadowBlur = 12;
      ctx.stroke(); ctx.restore(); continue;
    }
    if (p.kind === "suck") {
      ctx.save(); ctx.fillStyle = p.c; ctx.shadowColor = p.c; ctx.shadowBlur = 6;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.s, 0, Math.PI * 2); ctx.fill();
      ctx.restore(); continue;
    }
    if (p.kind === "ring") {
      const alpha = Math.max(0, 1 - p.r / p.maxR);
      ctx.save(); ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.strokeStyle = p.c; ctx.lineWidth = Math.max(1, (1 - p.r / p.maxR) * 3);
      ctx.globalAlpha = alpha * 0.85; ctx.stroke(); ctx.restore();
      continue;
    }
    if (p.kind === "fire") {
      ctx.save(); ctx.globalAlpha = Math.min(1, p.life / 20);
      ctx.fillStyle = p.c; ctx.shadowColor = p.c; ctx.shadowBlur = 8;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.s, 0, Math.PI * 2); ctx.fill();
      ctx.restore(); continue;
    }
    if (p.kind === "s") {
      ctx.save(); ctx.globalAlpha = Math.min(1, p.life / 25); ctx.strokeStyle = p.c; ctx.lineWidth = 2; ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(p.x - p.vx * 2.2, p.y - p.vy * 2.2); ctx.stroke();
      ctx.restore(); continue;
    }
    if (p.kind === "star") {
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.r);
      ctx.globalAlpha = Math.min(1, p.life / 25); ctx.fillStyle = p.c;
      const s = p.s;
      ctx.beginPath();
      ctx.moveTo(0, -s); ctx.quadraticCurveTo(0, 0, s, 0);
      ctx.quadraticCurveTo(0, 0, 0, s); ctx.quadraticCurveTo(0, 0, -s, 0);
      ctx.quadraticCurveTo(0, 0, 0, -s); ctx.fill();
      ctx.restore(); continue;
    }
  }
  fx.raf = fx.parts.length ? requestAnimationFrame(fxStep) : 0;
  if (!fx.parts.length) ctx.clearRect(0, 0, cv.width, cv.height);
}

function fxPop(elm) {
  if (!elm || FX_OFF) return;
  elm.classList.remove("pop"); void elm.offsetWidth; elm.classList.add("pop");
}

/* Center-stage, screen-filling hero celebration */
function fxHeroCelebrate(itemTitle, streakInfo, isBlock = false) {
  if (FX_OFF) return;
  const isFirst = !streakInfo || streakInfo.isFirstTimeToday;
  const isStreak = isFirst && streakInfo && streakInfo.streak >= 2;
  const streakCount = streakInfo ? streakInfo.streak : 1;
  const pitch = Math.pow(1.122, Math.min(streakCount - 1, 6));

  // 1. Multi-sensory Web Audio & Screen Shake
  if (isFirst) {
    fxAudio(isBlock ? "block" : "item", pitch);
    fxAudio("cyber");
    fxShake(document.querySelector(".board") || document.body);
  } else {
    fxAudio("sparkle");
  }

  // 2. Shockwaves + 4-pointed stars on canvas
  const cx = innerWidth / 2, cy = innerHeight * 0.40;
  fxCanvas();
  if (isFirst) {
    fx.parts.push({
      kind: "ring", x: cx, y: cy, r: 14, vr: 16,
      maxR: Math.max(innerWidth, innerHeight) * 0.48,
      c: isStreak ? "#f59e0b" : "#38bdf8", life: 40
    });
    setTimeout(() => {
      fx.parts.push({
        kind: "ring", x: cx, y: cy, r: 10, vr: 12,
        maxR: Math.max(innerWidth, innerHeight) * 0.35,
        c: "#a855f7", life: 35
      });
    }, 90);
  }

  const particleCount = isFirst ? 40 : 16;
  for (let k = 0; k < particleCount; k++) {
    const a = Math.random() * Math.PI * 2;
    const sp = 2 + Math.random() * 9;
    const isStar = Math.random() < 0.65;
    if (isStar) {
      fx.parts.push({
        kind: "star", x: cx, y: cy, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp - 2,
        s: 5 + Math.random() * 6, r: Math.random() * Math.PI, vr: (Math.random() - 0.5) * 0.35,
        c: FX_COLORS[(Math.random() * FX_COLORS.length) | 0], life: 48 + ((Math.random() * 25) | 0)
      });
    } else {
      fx.parts.push({
        kind: "s", x: cx, y: cy, vx: Math.cos(a) * sp * 1.2, vy: Math.sin(a) * sp * 1.2 - 1.5,
        s: 2.8, r: 0, vr: 0, c: isStreak ? "#f59e0b" : "#38bdf8", life: 35 + ((Math.random() * 20) | 0)
      });
    }
    if (isStreak && k < 12) {
      fx.parts.push({
        kind: "fire", x: Math.random() * innerWidth, y: innerHeight + 10,
        vx: (Math.random() - 0.5) * 1.5, vy: -(2.5 + Math.random() * 3.5),
        s: 4 + Math.random() * 5, c: Math.random() < 0.5 ? "#f59e0b" : "#ef4444", life: 45 + Math.random() * 25
      });
    }
  }
  if (!fx.raf) fx.raf = requestAnimationFrame(fxStep);

  // 3. Center-Stage Hero Overlay Card
  const existing = document.getElementById("fx-hero-wrap");
  if (existing) existing.remove();

  const wrap = document.createElement("div");
  wrap.id = "fx-hero-wrap";
  wrap.className = "fx-hero-wrap";

  const card = document.createElement("div");
  card.className = "fx-hero-card" + (isStreak ? " fever-card" : "");

  const pill = document.createElement("div");
  pill.className = "fx-hero-pill";
  if (isFirst) {
    pill.textContent = "⚡ SYNAPSE LINKED · 100% INGESTED";
  } else {
    pill.textContent = "✔ RE-CHECKED";
  }
  card.appendChild(pill);

  const phrases = RPG_PHRASES;
  const chosenPhrase = isFirst ? phrases[(Math.random() * phrases.length) | 0] : "ALREADY LOGGED TODAY";
  const titleEl = document.createElement("h2");
  titleEl.className = "fx-hero-title";
  titleEl.textContent = chosenPhrase;
  card.appendChild(titleEl);

  const sub = document.createElement("div");
  sub.className = "fx-hero-scramble";
  const rawText = (itemTitle || "").replace(/<[^>]+>/g, "").slice(0, 60);
  const GLYPHS = "010101<>/{}\\[]=+*!@#$%^&_~0123456789ABCDEF";
  let ticks = 0;
  const scrambleTimer = setInterval(() => {
    ticks++;
    let scrambled = "";
    for (let i = 0; i < Math.min(rawText.length, 28); i++) {
      scrambled += GLYPHS[(Math.random() * GLYPHS.length) | 0];
    }
    sub.textContent = "0x" + scrambled;
    if (ticks > 9) {
      clearInterval(scrambleTimer);
      sub.textContent = "✔ " + rawText;
      sub.style.color = "#f1f5f9";
      sub.style.fontWeight = "600";
    }
  }, 32);
  card.appendChild(sub);

  if (isStreak) {
    const streakEl = document.createElement("div");
    streakEl.className = "fx-hero-streak";
    streakEl.textContent = "🔥 " + streakInfo.streak + "x STREAK · FEVER MODE ACTIVE";
    card.appendChild(streakEl);
  } else if (!isFirst) {
    const noteEl = document.createElement("div");
    noteEl.style.fontSize = "13px";
    noteEl.style.color = "#94a3b8";
    noteEl.style.marginTop = "3px";
    noteEl.style.fontWeight = "500";
    noteEl.textContent = "(Already logged today — streak unaffected)";
    card.appendChild(noteEl);
  }

  wrap.appendChild(card);
  document.body.appendChild(wrap);
  setTimeout(() => { if (wrap.parentNode) wrap.remove(); }, 2400);
}

/* ---------------------------------------------------------------- The Singularity Supernova */
function fxSingularitySupernova(blockTitle, blockMinutes, streakInfo, originEl) {
  if (FX_OFF) return;
  const isFirst = !streakInfo || streakInfo.isFirstTimeToday;
  const isStreak = isFirst && streakInfo && streakInfo.streak >= 2;

  // Determine singularity focal point (center of target button/element or center of screen)
  let cx = innerWidth / 2, cy = innerHeight * 0.42;
  if (originEl && originEl.getBoundingClientRect) {
    const rect = originEl.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.left >= 0 && rect.bottom <= innerHeight && rect.right <= innerWidth) {
      cx = rect.left + rect.width / 2;
      cy = rect.top + rect.height / 2;
    }
  }

  // Phase 1: The Singularity (0ms - 480ms)
  // Cinematic darkening vignette & deep board suction implosion
  const vignette = document.createElement("div");
  vignette.className = "fx-supernova-vignette";
  document.body.appendChild(vignette);

  const boardEl = document.querySelector(".board") || document.body;
  boardEl.classList.remove("fx-board-explode", "fx-shake-heavy");
  boardEl.classList.add("fx-board-implode");

  // Reverse vacuum suction audio frequency drop (0.48s)
  fxAudio("singularity_suck");

  // Inward collapsing accretion rings & gravitational suction streams
  fxCanvas();
  for (let r = 0; r < 4; r++) {
    fx.parts.push({
      kind: "collapse_ring", x: cx, y: cy,
      r: 180 + r * 90, vr: -7 - r * 2.8, life: 28,
      c: r % 2 === 0 ? "#38bdf8" : "#a855f7"
    });
  }
  for (let i = 0; i < 65; i++) {
    const ang = Math.random() * Math.PI * 2;
    const dist = 120 + Math.random() * 380;
    const speed = dist / 26; // reaches center in ~26 frames (~450ms)
    fx.parts.push({
      kind: "suck",
      x: cx + Math.cos(ang) * dist,
      y: cy + Math.sin(ang) * dist,
      vx: -Math.cos(ang) * speed,
      vy: -Math.sin(ang) * speed,
      s: 2.5 + Math.random() * 3,
      c: Math.random() < 0.4 ? "#38bdf8" : (Math.random() < 0.5 ? "#a855f7" : "#fef08a"),
      life: 27
    });
  }
  if (!fx.raf) fx.raf = requestAnimationFrame(fxStep);

  // Phase 2: Detonation! Full Screen Unobstructed (at 480ms)
  setTimeout(() => {
    // 1. Remove implosion, trigger explosion and heavy kinetic screen shake
    boardEl.classList.remove("fx-board-implode");
    boardEl.classList.add("fx-board-explode", "fx-shake-heavy");
    setTimeout(() => {
      boardEl.classList.remove("fx-board-explode", "fx-shake-heavy");
    }, 550);

    // 2. Blinding Supernova Flash across the entire screen
    const flash = document.createElement("div");
    flash.className = "fx-supernova-flash";
    document.body.appendChild(flash);
    setTimeout(() => {
      if (flash.parentNode) flash.remove();
      if (vignette.parentNode) vignette.remove();
    }, 650);

    // 3. Massive Sub-Bass 808 Drop + Fanfare
    fxAudio("supernova_drop");

    // 4. Expanding shockwave rings & 160+ radial stars / spark beams filling the screen
    fx.parts.push({
      kind: "ring", x: cx, y: cy, r: 20, vr: 16,
      maxR: Math.max(innerWidth, innerHeight) * 1.25,
      c: "#f59e0b", life: 80
    });
    fx.parts.push({
      kind: "ring", x: cx, y: cy, r: 14, vr: 12,
      maxR: Math.max(innerWidth, innerHeight) * 1.0,
      c: "#38bdf8", life: 75
    });
    fx.parts.push({
      kind: "ring", x: cx, y: cy, r: 10, vr: 8.5,
      maxR: Math.max(innerWidth, innerHeight) * 0.75,
      c: "#a855f7", life: 70
    });

    for (let k = 0; k < 160; k++) {
      const a = Math.random() * Math.PI * 2;
      const sp = 3 + Math.random() * 16;
      const isStar = Math.random() < 0.7;
      if (isStar) {
        fx.parts.push({
          kind: "star", x: cx, y: cy,
          vx: Math.cos(a) * sp, vy: Math.sin(a) * sp - 2,
          s: 7 + Math.random() * 11, r: Math.random() * Math.PI,
          vr: (Math.random() - 0.5) * 0.35,
          c: FX_COLORS[(Math.random() * FX_COLORS.length) | 0],
          life: 75 + ((Math.random() * 35) | 0)
        });
      } else {
        fx.parts.push({
          kind: "s", x: cx, y: cy,
          vx: Math.cos(a) * sp * 1.35, vy: Math.sin(a) * sp * 1.35 - 1.5,
          s: 3.5, r: 0, vr: 0,
          c: Math.random() < 0.5 ? "#f59e0b" : "#38bdf8",
          life: 55 + ((Math.random() * 30) | 0)
        });
      }
    }
    if (!fx.raf) fx.raf = requestAnimationFrame(fxStep);
  }, 480);

  // Phase 3: Only after the supernova effect has finished, display the frosted hero card! (at 1450ms)
  setTimeout(() => {
    fxAudio("cyber");

    const existing = document.getElementById("fx-hero-wrap");
    if (existing) existing.remove();

    const wrap = document.createElement("div");
    wrap.id = "fx-hero-wrap";
    wrap.className = "fx-hero-wrap";

    const card = document.createElement("div");
    card.className = "fx-hero-card supernova-card" + (isStreak ? " fever-card" : "");

    const pill = document.createElement("div");
    pill.className = "fx-hero-pill";
    pill.textContent = isFirst ? "🌌 SINGULARITY COLLAPSED · SUPERNOVA DETONATED" : "🌌 SUPERNOVA (RE-CHECKED)";
    card.appendChild(pill);

    const phrases = BLOCK_PHRASES;
    const chosenPhrase = isFirst ? phrases[(Math.random() * phrases.length) | 0] : "BLOCK COMPLETED (RE-CHECKED)";
    const titleEl = document.createElement("h2");
    titleEl.className = "fx-hero-title";
    titleEl.textContent = chosenPhrase;
    card.appendChild(titleEl);

    const sub = document.createElement("div");
    sub.className = "fx-hero-scramble";
    const cleanTitle = (blockTitle || "Deep Study Block").replace(/<[^>]+>/g, "").slice(0, 50);
    const minSuffix = blockMinutes ? " (+" + blockMinutes + "m BANKED)" : "";
    const rawText = cleanTitle + minSuffix;
    const GLYPHS = "010101<>/{}\\[]=+*!@#$%^&_~0123456789ABCDEF";
    let ticks = 0;
    const scrambleTimer = setInterval(() => {
      ticks++;
      let scrambled = "";
      for (let i = 0; i < Math.min(rawText.length, 32); i++) {
        scrambled += GLYPHS[(Math.random() * GLYPHS.length) | 0];
      }
      sub.textContent = "0x" + scrambled;
      if (ticks > 9) {
        clearInterval(scrambleTimer);
        sub.textContent = "✔ " + rawText;
        sub.style.color = "#f1f5f9";
        sub.style.fontWeight = "600";
      }
    }, 32);
    card.appendChild(sub);

    if (isStreak) {
      const streakEl = document.createElement("div");
      streakEl.className = "fx-hero-streak";
      streakEl.textContent = "🔥 " + streakInfo.streak + "x STREAK · FEVER MODE ACTIVE";
      card.appendChild(streakEl);
    } else if (!isFirst) {
      const noteEl = document.createElement("div");
      noteEl.style.fontSize = "13px";
      noteEl.style.color = "#94a3b8";
      noteEl.style.marginTop = "3px";
      noteEl.style.fontWeight = "500";
      noteEl.textContent = "(Already logged today — streak unaffected)";
      card.appendChild(noteEl);
    }

    wrap.appendChild(card);
    document.body.appendChild(wrap);
    setTimeout(() => { if (wrap.parentNode) wrap.remove(); }, 3800);
  }, 1450);
}

/* ---------------------------------------------------------------- everything */
function draw() {
  initCompletedTracker();
  const tl = $("#tl"), keep = tl.scrollTop;
  drawHeader();
  drawTimeline();
  tl.scrollTop = keep;
  drawPane();
}

/* ---------------------------------------------------------------- refresh (live mode) */
async function refresh() {
  try {
    const r = await fetch("/api/plan?date=" + encodeURIComponent(DATE));
    if (!r.ok) return;
    const data = await r.json();
    if (JSON.stringify(data) !== JSON.stringify(PLAN)) { PLAN = data; draw(); }
    $("#status").textContent = "synced " + new Date().toLocaleTimeString();
  } catch (e) { $("#status").textContent = "offline"; }
}
async function refreshBusy() {
  try {
    const r = await fetch("/api/busy?date=" + encodeURIComponent(DATE));
    if (!r.ok) return;
    const data = await r.json();
    const warn = data.busy_warnings || [];
    delete data.changed; delete data.busy_warnings;
    if (JSON.stringify(data) !== JSON.stringify(PLAN)) { PLAN = data; draw(); }
    $("#status").textContent = "calendar checked " + new Date().toLocaleTimeString() + (warn.length ? " · " + warn[0] : "");
  } catch (e) { /* offline: the 15-second poll reports it */ }
}
if (MODE === "live") {
  setInterval(refresh, 15000); window.addEventListener("focus", refresh);
  setInterval(refreshBusy, 5 * 60 * 1000); window.addEventListener("focus", refreshBusy);
}
if (READONLY) { $("#readonly-note").hidden = false; $("#status").textContent = "published copy"; }
if (WRITE) {
  $("#hosted-note").hidden = false; $("#settings").hidden = false; $("#status").textContent = "hosted copy";
  $("#scope").textContent = WRITE.repo; $("#scope-branch").textContent = WRITE.branch || "main";
  tokenState();
  $("#token-save").addEventListener("click", () => {
    const t = $("#token").value.trim();
    if (!t) { $("#token-msg").textContent = "paste a token first"; return; }
    setToken(t); $("#token").value = ""; $("#token-msg").textContent = "saved in this browser"; toast("token saved");
  });
  $("#token-forget").addEventListener("click", () => { setToken(""); $("#token-msg").textContent = "forgotten"; });
  const form = $("#debrief-form");
  if (form) form.addEventListener("submit", async e => {
    e.preventDefault();
    const w = writer(); if (!w) { needToken(); return; }
    const f = new FormData(form);
    const btn = form.querySelector("button[type=submit]"); btn.disabled = true;
    try {
      const res = await w.debrief({ date: DATE, event: f.get("event") || "", did: f.get("did") || "", learned: f.get("learned") || "", story: f.get("story") || "" });
      noteWrite(res);
      const done = el("p", "muted", "written to study-plan/profile/notes.md as \"" + res.message + "\"; the coach reads it next session and this page shows it after the re-render.");
      done.id = "debrief-written";
      form.replaceWith(done);
      toast("debrief committed");
    } catch (err) { toast("not written: " + (err.message || err), true); btn.disabled = false; }
  });
}

/* ---------------------------------------------------------------- controls */
$("#today").addEventListener("click", () => {
  if (IS_TODAY) { const n = $("#now"); if (n) { const tl = $("#tl"); tl.scrollTo({ top: Math.max(0, n.offsetTop - tl.clientHeight / 3), behavior: REDUCE ? "auto" : "smooth" }); } return; }
  if (MODE === "live") location.href = "/?date=" + todayIso();
  else if (HUB.today_href) location.href = HUB.today_href; else toast("no page for today");
});
$("#allhours").addEventListener("click", () => {
  state.showAll = !state.showAll; try { localStorage.setItem("board.allhours", state.showAll ? "1" : "0"); } catch (e) { /* private mode */ }
  $("#allhours").classList.toggle("on", state.showAll); $("#allhours").setAttribute("aria-pressed", String(state.showAll)); positionTimeline();
});
$("#more").addEventListener("click", () => { const r = $("#callrow"); r.classList.toggle("open"); $("#more").textContent = r.classList.contains("open") ? "less" : "more"; });
$("#m-copylog").addEventListener("click", () => { copy(PLAN.blocks.map(b => b.log_hint).filter(Boolean).join("\n"), "the log commands"); $("#menu").open = false; });
document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));
$("#n-jobs").addEventListener("click", () => setTab("jobs")); $("#n-events").addEventListener("click", () => setTab("events"));
$("#back").addEventListener("click", () => { if (state.tab !== "study") setTab("study"); else clearSel(); });
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if ($("#menu").open) { $("#menu").open = false; return; }
  if (state.tab !== "study") { setTab("study"); return; }
  if (state.sel) { clearSel(); $("#tl").focus({ preventScroll: true }); }
});
document.addEventListener("click", e => { const m = $("#menu"); if (m.open && !m.contains(e.target)) m.open = false; });
window.addEventListener("hashchange", () => { state.sel = null; state.tab = "study"; readHash(); draw(); });
window.addEventListener("resize", () => positionTimeline());
if (IS_TODAY) setInterval(() => { const n = $("#now"); if (n && geo) n.style.top = T.yOf(geo, nowMin()) + "px"; }, 30000);

readHash();
draw();
setTimeout(() => {
  const n = state.sel ? document.getElementById("blk-" + state.sel) || document.getElementById("ev-node-" + state.sel) : null;
  if (n) n.scrollIntoView({ block: "nearest" });
  else if (IS_TODAY && $("#now")) { const tl = $("#tl"), y = $("#now").offsetTop; if (y > tl.clientHeight) tl.scrollTop = Math.max(0, y - tl.clientHeight / 3); }
}, 0);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- the server

def make_handler(root: Path, default_date: str):
    parent = parent_root(root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "studyplan-board"
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: Any) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _date(self, query: dict[str, list[str]]) -> str:
            return (query.get("date") or [default_date])[0]

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise BoardError("request body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _report(self, kind: str, date: str) -> None:
            """One of the two daily reports, read-only. The route regex is the whole allowlist: kind is `jobs`
            or `events`, the name is `YYYY-MM-DD.html`, nothing else under /reports/ resolves."""
            path = report_dir(parent, kind) / f"{date}.html"
            if not path.is_file():
                self._json(404, {"error": f"no {kind} report for {date}"})
                return
            self._send(200, path.read_bytes(), "text/html; charset=utf-8")

        def _file(self, ref: str) -> None:
            """Render one non-hidden repo artifact through the same read-only pages as the hosted study hub."""
            ref = unquote(ref).lstrip("/")
            parts = Path(ref).parts
            if not parts or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
                self._json(404, {"error": "file not available"})
                return
            from .hub import render_file_page  # local import avoids board ↔ hub import cycle
            page = render_file_page(ref, root, parent, live=True)
            if page is None:
                self._json(404, {"error": "file not available"})
                return
            self._send(200, page.encode(), "text/html; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                route = _REPORT_ROUTE.match(url.path)
                file_route = _FILE_ROUTE.match(url.path)
                legacy_profile_route = _LEGACY_PROFILE_ROUTE.match(url.path)
                if url.path in ("/", "/index.html"):
                    date = self._date(query)
                    try:
                        payload = plan_payload(root, date)
                    except BoardError as e:
                        self._send(404, render_no_plan_html(date, str(e)).encode(), "text/html; charset=utf-8")
                        return
                    self._send(200, render_html(payload, reports=reports_payload(parent, date)).encode(), "text/html; charset=utf-8")
                elif route:
                    self._report(route.group(1), route.group(2))
                elif file_route:
                    self._file(file_route.group(1))
                elif legacy_profile_route:
                    ref = legacy_profile_route.group(1)
                    self._file("study-plan/" + ref if ref == "profile/notes.md" else ref)
                elif url.path == "/api/plan":
                    self._json(200, plan_payload(root, self._date(query)))
                elif url.path in ("/study", "/api/session"):
                    from .session import session_payload, render_session
                    payload = session_payload(root, self._date(query), (query.get('block') or [''])[0],
                                              (query.get('item') or [''])[0], follow=query.get('follow') == ['1'])
                    if url.path == '/study':
                        self._send(200, render_session(payload).encode(), 'text/html; charset=utf-8')
                    else:
                        self._json(200, payload)
                elif url.path == "/favicon.ico":
                    self._send(200, b"\xf0\x9f\x93\x85", "text/plain; charset=utf-8")
                elif url.path == "/api/busy":
                    self._json(200, api_busy(root, self._date(query)))
                elif url.path == "/api/log":
                    self._json(200, {"date": self._date(query), "rows": log_rows(root, self._date(query))})
                else:
                    self._json(404, {"error": f"no route {url.path}"})
            except (BoardError, ValueError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:                                        # noqa: BLE001 - the page shows the message
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self) -> None:  # noqa: N802
            from .session import api_session
            routes = {"/api/item-decision": api_item_decision, "/api/done": api_done, "/api/undo": api_undo, "/api/block": api_block,
                      "/api/session": api_session}
            url = urlparse(self.path)
            fn = routes.get(url.path)
            if fn is None:
                self._json(404, {"error": f"no route {url.path}"})
                return
            try:
                if url.path == '/api/session':
                    origin = self.headers.get('Origin')
                    if origin and urlparse(origin).netloc != self.headers.get('Host'):
                        raise BoardError('Use the study board on this computer to update a session.')
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise BoardError('Session updates require JSON.')
                body = self._body()
                body.setdefault("date", default_date)
                self._json(200, fn(root, body))
            except BoardError as e:
                self._json(400, {"error": str(e)})
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"error": f"{type(e).__name__}: {e}"})
            except Exception as e:                                        # noqa: BLE001
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, fmt: str, *args: Any) -> None:              # quiet by default
            if root.joinpath(".board-verbose").exists():
                super().log_message(fmt, *args)

    return Handler


def _check_loopback(host: str) -> None:
    """The board writes to the repo and has no auth, so it may only ever listen on the loopback interface."""
    try:
        addr = ipaddress.ip_address(host if host not in ("localhost",) else "127.0.0.1")
    except ValueError as e:
        raise ValueError(f"refusing to bind {host!r}: the board is loopback-only (use 127.0.0.1)") from e
    if not addr.is_loopback:
        raise ValueError(f"refusing to bind {host!r}: the board is loopback-only (use 127.0.0.1)")


def make_server(root: Path, date: str, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    _check_loopback(host)
    server = ThreadingHTTPServer((host, port), make_handler(root, date))
    server.daemon_threads = True
    return server


def serve(root: Path, date: str, port: int = 8765, host: str = "127.0.0.1") -> int:
    server = make_server(root, date, port, host)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"study board for {date} on {url}  (ctrl-c to stop)")
    print(f"  plan: {root / 'plans' / (date + '.json')}\n  log:  {root / 'data' / 'log.jsonl'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


def serve_in_thread(root: Path, date: str, port: int = 0, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, str]:
    """For tests: a running server and its base URL. Call `shutdown()` and `server_close()` when done."""
    server = make_server(root, date, port, host)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


__all__ = ["api_block", "api_busy", "api_done", "api_undo", "make_server", "plan_payload", "render_html", "report_for",
           "reports_payload", "serve", "serve_in_thread"]
