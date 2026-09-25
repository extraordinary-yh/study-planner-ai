from pathlib import Path

from studyplan.leetcode import (append_attempts, derive_outcome, due_reviews, format_stats, load_attempts, parse_file,
                                parse_history_text, parse_rows, schedule, topic_key, topic_stats)

FIX = Path(__file__).resolve().parent / "fixtures" / "leetcode.csv"
BAR = {"easy": 15, "medium": 30, "hard": 45}
INTERVALS = {"gave_up": 1, "solution": 1, "hint": 3, "slow": 3, "clean": [7, 21, 60]}


def test_parse_csv_tolerant_columns():
    attempts = parse_file(FIX.read_text(), BAR, "2026-09-06")
    assert len(attempts) == 6
    two = [a for a in attempts if a["slug"] == "two-sum"]
    assert len(two) == 2 and two[1]["url"].endswith("/two-sum/")          # slug from title when the URL is missing
    assert two[0]["topics"] == ["arrays_hashing"] and two[0]["outcome"] == "clean"
    lis = next(a for a in attempts if a["slug"] == "longest-increasing-subsequence")
    assert lis["outcome"] == "slow" and lis["stuck"] and lis["minutes"] == 55 and "editorial" in lis["notes"]
    cs = next(a for a in attempts if a["slug"] == "course-schedule")
    assert cs["outcome"] == "gave_up" and cs["topics"] == ["graphs_topo_union_find"]
    mi = next(a for a in attempts if a["slug"] == "merge-intervals")
    assert mi["outcome"] == "clean" and set(mi["topics"]) == {"arrays_hashing", "intervals_greedy"}
    wb = next(a for a in attempts if a["slug"] == "word-break")
    assert wb["outcome"] == "hint" and wb["topics"] == ["dynamic_programming", "tries"]


def test_parse_json_and_outcomes():
    rows = parse_rows([{"title": "LRU Cache", "difficulty": "M", "tags": ["Hash Table", "Linked List"], "minutes": "1h", "result": "AC"},
                       {"problem": "Trap", "level": "hard", "time": "20", "stuck": "yes", "date": "09/01/2026"},
                       {"question": "", "url": ""}], BAR, "2026-09-06")
    assert len(rows) == 2
    assert rows[1]["difficulty"] == "medium" and rows[1]["minutes"] == 60 and rows[1]["outcome"] == "slow"
    assert rows[0]["date"] == "2026-09-01" and rows[0]["outcome"] == "slow" and rows[0]["difficulty"] == "hard"
    assert derive_outcome("read the solution", 10, False, "easy", BAR) == "solution"
    assert derive_outcome("", 10, False, "easy", BAR) == "clean"
    assert derive_outcome("wrong answer", 10, False, "easy", BAR) == "gave_up"
    assert topic_key("Heap (Priority Queue)") == "heaps" and topic_key("Math") is None and topic_key("Design") == "design"


def test_append_dedups(tmp_path):
    path = tmp_path / "lc.jsonl"
    attempts = parse_file(FIX.read_text(), BAR, "2026-09-06")
    assert append_attempts(path, attempts) == 6
    assert append_attempts(path, attempts) == 0
    assert len(load_attempts(path)) == 6


def test_schedule_and_due():
    attempts = parse_file(FIX.read_text(), BAR, "2026-09-06")
    rows = {r["slug"]: r for r in schedule(attempts, INTERVALS)}
    assert rows["two-sum"]["clean_streak"] == 2 and rows["two-sum"]["due"] == "2026-09-20"       # 21 days after the second clean
    assert rows["course-schedule"]["due"] == "2026-08-23"                                        # gave up: next day
    assert rows["longest-increasing-subsequence"]["due"] == "2026-08-24"                          # slow: 3 days
    assert rows["merge-intervals"]["due"] == "2026-09-08"                                         # first clean: 7 days
    due = due_reviews(attempts, INTERVALS, "2026-09-06")
    assert [r["slug"] for r in due] == ["course-schedule", "longest-increasing-subsequence", "word-break"]
    assert due[0]["overdue_days"] == 14
    assert [r["slug"] for r in due_reviews(attempts, INTERVALS, "2026-09-06", limit=1)] == ["course-schedule"]


def test_topic_stats_weakness():
    attempts = parse_file(FIX.read_text(), BAR, "2026-09-06")
    stats = {s["topic"]: s for s in topic_stats(attempts, "2026-09-06", 90, 3, {"heaps": 1, "arrays_hashing": 4, "tries": None})}
    assert stats["graphs_topo_union_find"]["weakness"] > stats["arrays_hashing"]["weakness"]
    assert stats["arrays_hashing"]["attempts"] == 3 and stats["arrays_hashing"]["recent_clean"] == 3 and stats["arrays_hashing"]["weakness"] == 0.0
    assert stats["heaps"]["attempts"] == 0 and stats["heaps"]["weakness"] == 1.5                  # rated 1, never tried
    assert stats["dynamic_programming"]["recent_clean"] == 0 and stats["dynamic_programming"]["weakness"] >= 1.0
    text = format_stats(list(stats.values()))
    assert "graphs_topo_union_find" in text and "weakness" in text


CATALOG = {69: ("easy", "Math, Binary Search"), 2: ("medium", "Linked List, Math"), 207: ("medium", "Graph, Topological Sort")}
HISTORY = """2026.02.01｜2. Add Two Numbers
2026.02.01｜13. Roman to Integer
Sep 3｜69. Sqrt(x) 2025.05.31｜643. Maximum Average Subarray I 2024.12.14｜207. Course Schedule"""


def test_history_format_and_catalog():
    rows = parse_history_text(HISTORY, "2026-09-08")
    assert [(r["date"], r["number"], r["problem"]) for r in rows] == [
        ("2026-02-01", 2, "Add Two Numbers"), ("2026-02-01", 13, "Roman to Integer"), ("2026-09-03", 69, "Sqrt(x)"),
        ("2025-05-31", 643, "Maximum Average Subarray I"), ("2024-12-14", 207, "Course Schedule")]
    attempts = parse_file(HISTORY, BAR, "2026-09-08", CATALOG)
    by = {a["slug"]: a for a in attempts}
    assert by["sqrt-x"]["difficulty"] == "easy" and by["sqrt-x"]["topics"] == ["binary_search"]      # Math ignored
    assert by["course-schedule"]["topics"] == ["graphs_topo_union_find"] and by["course-schedule"]["number"] == 207
    assert by["roman-to-integer"]["difficulty"] == "medium" and by["roman-to-integer"]["topics"] == []   # not in catalog
    assert all(a["outcome"] == "clean" and "history import" in a["notes"] for a in attempts)
    # a month-day later than today belongs to last year
    assert parse_history_text("Dec 25｜1. Two Sum", "2026-09-08")[0]["date"] == "2025-12-25"


def test_due_prefers_weak_topics():
    attempts = parse_file(FIX.read_text(), BAR, "2026-09-06")
    weak = {"dynamic_programming": 0.2, "intervals_greedy": 1.0, "arrays_hashing": 0.0, "tries": 0.1}
    due = due_reviews(attempts, INTERVALS, "2026-09-30", weakness=weak)
    assert due[0]["slug"] == "course-schedule"                                    # failed outcomes still first
    clean = [r["slug"] for r in due if r["last_outcome"] == "clean"]
    assert clean[0] == "merge-intervals"                                          # weakest topic among clean reviews
