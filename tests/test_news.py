import datetime as dt
from pathlib import Path

from studyplan.news import fetch_feeds, format_items, parse_feed, rank, score_item

FEEDS = Path(__file__).resolve().parent / "fixtures" / "feeds"
TOPICS = {"mcp": 3, "agents": 3, "inference": 2, "rate limit": 1, "postgres": 1, "openai": 2, "claude": 2, "benchmark": 1}
NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)


def test_parse_atom_and_rss():
    atom = parse_feed((FEEDS / "simonw.xml").read_text(), "Simon")
    assert [i["title"] for i in atom][:2] == ["Notes on building MCP servers for coding agents", "A recipe for sourdough"]
    assert atom[0]["url"] == "https://simonwillison.net/2026/Sep/6/mcp-agents/" and atom[0]["summary"] == "Tool use, evals and the agent loop."
    assert atom[0]["published"].isoformat() == "2026-09-06T08:00:00+00:00"
    rss = parse_feed((FEEDS / "hn_ai.xml").read_text(), "HN")
    assert rss[0]["title"] == "OpenAI ships a new inference stack for agents" and rss[0]["url"] == "https://example.com/openai-inference"
    assert "Benchmark results" in rss[0]["summary"] and "<p>" not in rss[0]["summary"]
    assert rss[0]["published"].hour == 10


def test_score_rank_dedupe_window():
    feeds = [{"key": "simonw", "name": "Simon"}, {"key": "hn_ai", "name": "HN"}, {"key": "missing", "name": "Missing", "unverified": True}]
    items, warnings = fetch_feeds(feeds, fixtures=FEEDS)
    assert len(items) == 6 and warnings == ["missing: no fixture missing.xml"]
    ranked = rank(items, TOPICS, now=NOW, hours=36, limit=10)
    titles = [r["title"] for r in ranked]
    assert titles[0] == "OpenAI ships a new inference stack for agents"                 # openai 2 + inference 2 + agents 3 + benchmark 0.5
    assert ranked[0]["score"] == 8 and set(ranked[0]["hits"]) == {"openai", "inference", "agents", "benchmark"}
    assert titles.count("Notes on building MCP servers for coding agents") == 1          # same URL in both feeds
    assert "Old post about Claude" not in titles                                        # outside the window
    assert titles[-1] == "A recipe for sourdough" and ranked[-1]["score"] == 0
    score, hits = score_item({"title": "Rate limits", "summary": ""}, TOPICS)
    assert score == 0 and hits == []                                                    # "rate limits" != "rate limit" whole-word
    out = format_items(ranked)
    assert "  8  09-06 10:15  OpenAI ships" in out and "https://example.com/pg-rate-limit" in out
