"""Fetch RSS/Atom feeds, keep the last N hours, score by topic keywords."""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

UA = "studyplan/0.1 (+https://github.com/example-user/job-radar)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    raw = "".join(el.itertext()) if el.text is None else (el.text or "")
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw))).strip()


def _when(s: str) -> dt.datetime | None:
    s = s.strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def parse_feed(xml_text: str, source: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    for el in root.iter():
        kind = _local(el.tag)
        if kind not in ("item", "entry"):
            continue
        fields = {_local(c.tag): c for c in el}
        title = _text(fields.get("title"))
        link = ""
        if kind == "entry":
            for c in el:
                if _local(c.tag) == "link" and c.get("rel") in (None, "alternate"):
                    link = c.get("href", "")
                    break
        else:
            link = _text(fields.get("link")) or (fields.get("guid").text or "" if fields.get("guid") is not None else "")
        summary = _text(fields.get("description")) or _text(fields.get("summary")) or _text(fields.get("content")) or _text(fields.get("encoded"))
        published = _when(_text(fields.get("pubDate")) or _text(fields.get("published")) or _text(fields.get("updated")) or _text(fields.get("date")))
        if title and link:
            items.append({"source": source, "title": title, "url": link.strip(), "summary": summary[:400], "published": published})
    return items


def score_item(item: dict[str, Any], topics: dict[str, float]) -> tuple[int, list[str]]:
    title, summary = item["title"].lower(), item.get("summary", "").lower()
    score, hits = 0.0, []
    for kw, w in topics.items():
        k = kw.lower()
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])")
        if pat.search(title):
            score += float(w)
            hits.append(kw)
        elif pat.search(summary):
            score += float(w) / 2
            hits.append(kw)
    return int(round(score)), hits


def fetch_feeds(feeds: list[dict[str, Any]], *, fixtures: Path | None = None, timeout: float = 15.0,
                raw_dir: Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    items, warnings = [], []
    client = None if fixtures else httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA})
    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        key = feed["key"]
        try:
            if fixtures:
                path = fixtures / f"{key}.xml"
                if not path.exists():
                    warnings.append(f"{key}: no fixture {path.name}")
                    continue
                body = path.read_text()
            else:
                r = client.get(feed["url"])
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}")
                body = r.text
                if raw_dir:
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    (raw_dir / f"{key}.xml").write_text(body)
            parsed = parse_feed(body, feed.get("name", key))
            if not parsed:
                warnings.append(f"{key}: feed parsed but held no items")
            items.extend(parsed)
        except Exception as e:  # noqa: BLE001 - one bad feed is a warning, never a failed command
            note = " (URL unverified)" if feed.get("unverified") else ""
            warnings.append(f"{key}: {type(e).__name__}: {str(e)[:120]}{note}")
    if client:
        client.close()
    return items, warnings


def rank(items: list[dict[str, Any]], topics: dict[str, float], *, now: dt.datetime, hours: int, limit: int) -> list[dict[str, Any]]:
    cutoff = now - dt.timedelta(hours=hours)
    out = []
    seen: set[str] = set()
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        if it.get("published") and it["published"] < cutoff:
            continue
        score, hits = score_item(it, topics)
        out.append({**it, "score": score, "hits": hits})
    out.sort(key=lambda i: (-i["score"], -(i["published"].timestamp() if i.get("published") else 0), i["title"]))
    return out[:limit]


def format_items(items: list[dict[str, Any]]) -> str:
    lines = []
    for it in items:
        when = it["published"].astimezone(dt.timezone.utc).strftime("%m-%d %H:%M") if it.get("published") else "     ?    "
        lines.append(f"{it['score']:>3}  {when}  {it['title']}  [{it['source']}]")
        lines.append(f"      {it['url']}" + (f"  · {', '.join(it['hits'])}" if it["hits"] else ""))
    return "\n".join(lines)
