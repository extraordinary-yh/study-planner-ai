import json

from studyplan.remnote import extract_collection, mark_history_coverage, merge_records, write_catalog


def test_extract_collection_keeps_note_evidence_separate():
    document = {
        "document_id": "collection-1",
        "document_last_updated_at_iso": "2026-09-08T00:00:00.000Z",
        "content": """- Company
    - [**1. Two Sum**](https://leetcode.com/problems/two-sum/) (Array, Hash Map)
        - Use a dictionary of complements.
    - [200. Number of Islands](https://leetcode.com/problems/number-of-islands)
        - Graph traversal with an adjacency view.
""",
    }
    rows = extract_collection(document, "Example")
    assert [row["slug"] for row in rows] == ["two-sum", "number-of-islands"]
    assert rows[0]["title"] == "Two Sum"
    assert rows[0]["status"] == "note_only" and rows[0]["remnote_doc_id"] is None
    assert "arrays_hashing" in rows[0]["inferred_topics"]
    assert "graphs_topo_union_find" in rows[1]["inferred_topics"]
    for forbidden in ("date", "difficulty", "minutes", "outcome", "stuck"):
        assert forbidden not in rows[0]


def test_merge_and_write_catalog(tmp_path):
    base = {
        "slug": "two-sum", "title": "Two Sum", "url": "https://leetcode.com/problems/two-sum/",
        "companies": ["Amazon"], "source_collection_rem_ids": ["a"],
        "source_collection_updated_at": {"a": "2026-01-01T00:00:00Z"},
        "inferred_topics": ["arrays_hashing"], "remnote_doc_id": "problem", "status": "note_only",
    }
    other = {**base, "companies": ["Meta"], "source_collection_rem_ids": ["m"],
             "source_collection_updated_at": {"m": "2026-02-01T00:00:00Z"},
             "inferred_topics": ["two_pointers_sliding_window"]}
    records = merge_records([base, other])
    assert len(records) == 1 and records[0]["companies"] == ["Amazon", "Meta"]
    path = tmp_path / "catalog.jsonl"
    write_catalog(path, records)
    assert json.loads(path.read_text())["status"] == "note_only"


def test_raw_collection_uses_embedded_rem_id():
    document = {
        "document_id": "collection-1",
        "document_last_updated_at_iso": "2026-09-08T00:00:00.000Z",
        "raw_document_text": (
            "Company — Rem:<rem>collection-1</rem>\n"
            "   - [^^**721. Accounts Merge**^^](https://leetcode.com/problems/accounts-merge/) "
            "(Union-Find) — Rem:<rem>problem-721</rem> IsCollapsed\n"
        ),
    }
    row = extract_collection(document, "Example")[0]
    assert row["title"] == "Accounts Merge" and row["remnote_doc_id"] == "problem-721"


def test_history_coverage_is_an_annotation_not_an_attempt():
    records = [{"slug": "two-sum", "status": "note_only"}, {"slug": "three-sum", "status": "note_only"}]
    mark_history_coverage(records, [{"slug": "two-sum", "outcome": "clean"}])
    assert [row["in_imported_history"] for row in records] == [True, False]
    assert all("outcome" not in row and row["status"] == "note_only" for row in records)
