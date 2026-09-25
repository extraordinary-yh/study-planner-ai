from studyplan.config import load_terms
from studyplan.jobs import applications, latest_snapshot, upcoming_interviews, visible
from studyplan.stack import count_terms, format_table, job_terms, scopes_for, top_gaps


def _rows(roots):
    root, parent = roots
    _, jobs = latest_snapshot(parent)
    apps = applications(parent / "data" / "feedback.jsonl")
    return jobs, apps, count_terms(load_terms(root), scopes_for(jobs, set(apps), 75))


def test_applications_latest_state_and_interviews(roots):
    _, parent = roots
    _, jobs = latest_snapshot(parent)
    apps = applications(parent / "data" / "feedback.jsonl")
    assert set(apps) == {"stripe:2", "google:3", "mercor:1"}          # apple:4 was marked bad; kind rows ignored
    assert apps["mercor:1"] == {"stage": "phone", "stage_on": "2026-09-05", "applied_on": "2026-09-03"}
    assert apps["stripe:2"]["stage"] == "applied"
    ivs = upcoming_interviews(apps, jobs, "2026-09-07", 7, [{"company": "Figma", "stage": "onsite", "date": "2026-09-10"}])
    assert [(i["company"], i["stage"], i["dated"]) for i in ivs] == [("Figma", "onsite", True), ("Mercor", "phone", False)]
    assert upcoming_interviews(apps, jobs, "2026-09-20", 7) == []


def test_scopes_and_counts(roots):
    jobs, apps, rows = _rows(roots)
    assert len(visible(jobs)) == 4
    by = {r.name: r for r in rows}
    assert by["Java"].totals == {"applied": 3, "top": 3, "all": 4}
    assert by["Java"].counts == {"applied": 2, "top": 2, "all": 3} and by["Java"].companies == ["Stripe", "Google"]
    assert by["Kafka"].counts.get("applied", 0) == 1 and by["Kafka"].gap
    assert by["Go"].counts["applied"] == 2                             # Stripe and Google language lists
    assert by["Go"].counts["all"] == 2                                 # Apple's "Go to market" does not count
    assert by["Rust"].counts.get("all", 0) == 0                        # excluded job is out of every scope
    assert by["Temporal"].counts["applied"] == 1 and not by["Temporal"].gap
    assert rows[0].share("applied") >= rows[-1].share("applied")


def test_gaps_overrides_and_table(roots):
    root, parent = roots
    jobs, apps, rows = _rows(roots)
    gaps = top_gaps(rows, limit=3)
    assert gaps and all(g.gap for g in gaps) and {g.name for g in gaps[:2]} == {"Java", "Distributed systems"}   # both 2/3 applied
    assert "Java" not in {g.name for g in top_gaps(rows, limit=3, exclude={"Java"})}
    over = count_terms(load_terms(root), scopes_for(jobs, set(apps), 75), {"Java": "working"})
    assert not {r.name: r for r in over}["Java"].gap
    table = format_table(rows, 10)
    assert "GAP Java" in table and "Stripe, Google" in table
    mercor = next(j for j in jobs if j["id"] == "mercor:1")
    hits = job_terms(load_terms(root), mercor)
    names = [n for n, _ in hits]
    assert names[0] in ("FastAPI", "Kubernetes", "LangChain/LangGraph", "RL environments")   # gaps first
    assert "Temporal" in names and "PostgreSQL" in names
