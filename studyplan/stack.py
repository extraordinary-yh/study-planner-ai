"""Stack GCD: which technologies the target postings name, by scope, against the user's depth."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import is_gap
from .jobs import job_text, visible


@dataclass
class TermRow:
    name: str
    category: str
    depth: str
    counts: dict[str, int] = field(default_factory=dict)   # scope -> postings naming the term
    totals: dict[str, int] = field(default_factory=dict)   # scope -> postings in scope
    companies: list[str] = field(default_factory=list)     # applied-scope companies naming it

    @property
    def gap(self) -> bool:
        return is_gap(self.depth)

    def share(self, scope: str) -> float:
        return self.counts.get(scope, 0) / self.totals[scope] if self.totals.get(scope) else 0.0


def compile_terms(terms: list[dict[str, Any]], overrides: dict[str, str] | None = None) -> list[tuple[dict[str, Any], list[re.Pattern]]]:
    out = []
    for t in terms:
        depth = (overrides or {}).get(t["name"], t.get("depth", "none"))
        out.append(({**t, "depth": depth}, [re.compile(p, re.IGNORECASE) for p in t["patterns"]]))
    return out


def scopes_for(jobs: list[dict[str, Any]], applied_ids: set[str], top_min_score: int) -> dict[str, list[dict[str, Any]]]:
    vis = visible(jobs)
    applied = [j for j in jobs if j.get("id") in applied_ids]
    top = [j for j in vis if (j.get("score") or 0) >= top_min_score]
    return {"applied": applied, "top": top, "all": vis}


def count_terms(terms: list[dict[str, Any]], scopes: dict[str, list[dict[str, Any]]],
                overrides: dict[str, str] | None = None) -> list[TermRow]:
    compiled = compile_terms(terms, overrides)
    rows = {t["name"]: TermRow(t["name"], t.get("category", ""), t["depth"]) for t, _ in compiled}
    texts: dict[str, str] = {}
    for scope, jobs in scopes.items():
        for row in rows.values():
            row.totals[scope] = len(jobs)
        for job in jobs:
            jid = str(job.get("id"))
            text = texts.setdefault(jid, job_text(job))
            for t, patterns in compiled:
                if any(p.search(text) for p in patterns):
                    row = rows[t["name"]]
                    row.counts[scope] = row.counts.get(scope, 0) + 1
                    if scope == "applied" and job.get("company") and job["company"] not in row.companies:
                        row.companies.append(job["company"])
    out = list(rows.values())
    out.sort(key=lambda r: (-r.share("applied"), -r.share("top"), -r.share("all"), r.name))
    return out


def job_terms(terms: list[dict[str, Any]], job: dict[str, Any], overrides: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """(name, depth) for every term one posting names, gaps first."""
    text = job_text(job)
    hits = [(t["name"], t["depth"]) for t, patterns in compile_terms(terms, overrides) if any(p.search(text) for p in patterns)]
    hits.sort(key=lambda h: (not is_gap(h[1]), h[0]))
    return hits


def top_gaps(rows: list[TermRow], limit: int = 5, exclude: set[str] | None = None) -> list[TermRow]:
    return [r for r in rows if r.gap and r.name not in (exclude or set()) and (r.counts.get("applied") or r.counts.get("top"))][:limit]


def format_table(rows: list[TermRow], limit: int) -> str:
    totals = rows[0].totals if rows else {}
    head = f"{'term':<28} {'cat':<10} {'depth':<9} {'applied':>9} {'top':>9} {'all':>9}  companies (applied)"
    lines = [head, "-" * len(head)]
    for r in rows[:limit]:
        if not any(r.counts.values()):
            continue
        cells = [f"{r.counts.get(s, 0)}/{totals.get(s, 0)}" for s in ("applied", "top", "all")]
        mark = "GAP " if r.gap else "    "
        lines.append(f"{mark}{r.name:<24} {r.category:<10} {r.depth:<9} {cells[0]:>9} {cells[1]:>9} {cells[2]:>9}  {', '.join(r.companies[:4])}")
    return "\n".join(lines)
