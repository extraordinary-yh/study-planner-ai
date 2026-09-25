"""Build a note-only LeetCode catalog from selected RemNote collections.

The catalog is intentionally separate from ``data/leetcode.jsonl``. A note proves
exposure, not a timed attempt, and must never acquire fabricated attempt metadata.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

DEFAULT_ENDPOINT = "http://127.0.0.1:7788/mcp"
COLLECTIONS = {}  # Configure your own collection IDs; no account data is bundled.

LEETCODE_URL_RE = re.compile(r"https://leetcode\.com/problems/([a-z0-9-]+)(?:/)?", re.IGNORECASE)
COLLECTION_LINK_RE = re.compile(
    r"^ {3,4}-\s+\[(?P<title>[^]]+)\]\((?P<url>https://leetcode\.com/problems/[a-z0-9-]+/?)[^)]*\)(?P<suffix>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
REM_ID_RE = re.compile(r"Rem:<rem>([^<]+)</rem>")


def _tool_payload(response: httpx.Response) -> dict[str, Any]:
    """Unwrap one MCP JSON-RPC tool response, including simple SSE responses."""
    response.raise_for_status()
    raw = response.text.strip()
    if raw.startswith("event:") or raw.startswith("data:"):
        data = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
        if not data:
            raise ValueError("RemNote MCP returned an SSE response without data")
        raw = data[-1]
    envelope = json.loads(raw)
    if envelope.get("error"):
        raise RuntimeError(f"RemNote MCP error: {envelope['error']}")
    result = envelope.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"RemNote tool error: {result}")
    for item in result.get("content") or []:
        if item.get("type") == "text":
            payload = json.loads(item.get("text") or "{}")
            if payload.get("ok") is False:
                raise RuntimeError(f"RemNote tool error: {payload}")
            return payload
    raise ValueError("RemNote MCP tool response contained no JSON text payload")


def call_tool(endpoint: str, authorization: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Authorization": authorization,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    with httpx.Client(timeout=60) as client:
        return _tool_payload(client.post(endpoint, headers=headers, json=request))


def _clean_title(title: str) -> str:
    title = re.sub(r"[*_`^]", "", title).strip()
    return re.sub(r"^\d+\.\s*", "", title)


def infer_topics(title: str, suffix: str, body: str) -> list[str]:
    """Infer broad curriculum topics; these are hints, not canonical LC tags."""
    text = " ".join((title, suffix, body)).lower()
    rules = {
        "arrays_hashing": r"\barray|hash(?:map| map| table|set)|dictionary|counter\b|frequency map",
        "two_pointers_sliding_window": r"two[- ]pointer|sliding window|left and right pointer|\bwindow size\b",
        "binary_search": r"binary search|bisect_(?:left|right)|bisect\.",
        "stack_monotonic": r"monotonic stack|\bstack\b",
        "linked_list": r"linked list|listnode|\bhead\.next\b",
        "trees_bfs_dfs": r"binary tree|binary search tree|\bbst\b|treenode|tree traversal",
        "heaps": r"\bheap(?:q)?\b|priority queue",
        "graphs_topo_union_find": r"\bgraph\b|adjacency|topological|union[- ]find|disjoint set",
        "dynamic_programming": r"dynamic programming|\bdp\[|memoiz|tabulation",
        "backtracking": r"backtrack|permutation|combination search",
        "intervals_greedy": r"\bintervals?\b|\bgreedy\b",
        "tries": r"\btrie\b|prefix tree",
        "bit_manipulation": r"bit manipulation|bitwise|\bxor\b",
    }
    return [topic for topic, pattern in rules.items() if re.search(pattern, text)]


def extract_collection(document: dict[str, Any], company: str) -> list[dict[str, Any]]:
    """Extract the direct problem links in one company collection document."""
    content = str(document.get("raw_document_text") or document.get("content") or "")
    matches = list(COLLECTION_LINK_RE.finditer(content))
    rows = []
    for index, match in enumerate(matches):
        url = match.group("url")
        slug_match = LEETCODE_URL_RE.search(url)
        if not slug_match:
            continue
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        title = _clean_title(match.group("title"))
        raw_suffix = match.group("suffix").strip()
        rem_match = REM_ID_RE.search(raw_suffix)
        suffix = raw_suffix.split(" — Rem:", 1)[0].strip()
        rows.append({
            "slug": slug_match.group(1).lower(),
            "title": title,
            "url": f"https://leetcode.com/problems/{slug_match.group(1).lower()}/",
            "companies": [company],
            "source_collection_rem_ids": [str(document["document_id"])],
            "source_collection_updated_at": {
                str(document["document_id"]): document.get("document_last_updated_at_iso")
            },
            "inferred_topics": infer_topics(title, suffix, content[match.end():body_end]),
            "remnote_doc_id": rem_match.group(1) if rem_match else None,
            "status": "note_only",
        })
    return rows


def merge_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        slug = row["slug"]
        if slug not in merged:
            merged[slug] = row
            continue
        current = merged[slug]
        current["companies"] = sorted(set(current["companies"] + row["companies"]))
        current["source_collection_rem_ids"] = sorted(
            set(current["source_collection_rem_ids"] + row["source_collection_rem_ids"])
        )
        current["source_collection_updated_at"].update(row["source_collection_updated_at"])
        current["inferred_topics"] = sorted(set(current["inferred_topics"] + row["inferred_topics"]))
    return [merged[slug] for slug in sorted(merged)]


def attach_document_ids(records: list[dict[str, Any]], endpoint: str, authorization: str) -> None:
    """Resolve each problem's RemNote document id from its exact LeetCode slug."""
    for start in range(0, len(records), 10):
        batch = records[start:start + 10]
        payload = call_tool(endpoint, authorization, "search_docs", {"queries": [row["title"] for row in batch]})
        for hit in payload.get("matches") or []:
            haystack = "\n".join([str(hit.get("document") or ""), *map(str, hit.get("matches") or [])])
            slugs = {match.group(1).lower() for match in LEETCODE_URL_RE.finditer(haystack)}
            for row in batch:
                if row["slug"] in slugs:
                    row["remnote_doc_id"] = hit.get("doc_id")


def fetch_catalog(endpoint: str, authorization: str, collections=None) -> list[dict[str, Any]]:
    collections = collections or COLLECTIONS
    if not collections:
        raise ValueError("No collections configured; add config/remnote.yaml with collections: {label: id}")
    docs_payload = call_tool(endpoint, authorization, "read_docs_raw", {"ids": list(collections.values())})
    if docs_payload.get("truncated_documents"):
        raise ValueError(f"RemNote truncated collection documents: {docs_payload['truncated_documents']}")
    by_id = {doc["document_id"]: doc for doc in docs_payload.get("documents") or []}
    missing = set(collections.values()) - set(by_id)
    if missing:
        raise ValueError(f"RemNote did not return collection documents: {sorted(missing)}")
    rows = []
    for company, rem_id in collections.items():
        rows.extend(extract_collection(by_id[rem_id], company))
    # The raw tree is complete and carries direct Rem ids. The rendered documents are
    # word-limited, but their available solution bodies improve topic hints for the
    # problems they include. Merge those hints without storing either raw response.
    rendered = call_tool(endpoint, authorization, "read_docs", {"ids": list(collections.values())})
    rendered_by_id = {doc["document_id"]: doc for doc in rendered.get("documents") or []}
    for company, rem_id in collections.items():
        if rem_id in rendered_by_id:
            rows.extend(extract_collection(rendered_by_id[rem_id], company))
    records = merge_records(rows)
    if any(not row.get("remnote_doc_id") for row in records):
        attach_document_ids(records, endpoint, authorization)
    return records


def mark_history_coverage(records: list[dict[str, Any]], attempts: list[dict[str, Any]]) -> None:
    """Annotate catalog rows without turning them into attempt records."""
    attempted = {str(row.get("slug")) for row in attempts if row.get("slug")}
    for row in records:
        row["in_imported_history"] = row["slug"] in attempted


def write_catalog(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records))
