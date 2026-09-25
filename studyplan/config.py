"""Locate the sub-project and the parent job-radar repo; load YAML config and profile files."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEPTHS = ("none", "limited", "familiar", "working", "strong", "expert")
GAP_BELOW = "working"          # a term below this depth is a gap


def repo_root() -> Path:
    env = os.environ.get("STUDYPLAN_ROOT")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "config" / "curriculum.yaml").exists():
            return candidate
    raise FileNotFoundError("no pyproject.toml with config/curriculum.yaml above cwd; set STUDYPLAN_ROOT")


def parent_root(root: Path) -> Path:
    """The job-radar checkout whose data/ and profile/ the coach reads. Override with JOBRADAR_ROOT."""
    env = os.environ.get("JOBRADAR_ROOT")
    if env:
        return Path(env).resolve()
    return root / "integrations" / "job-radar"


def _yaml(path: Path) -> dict[str, Any]:
    if not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text()) or {}


def load_preferences(root: Path) -> dict[str, Any]:
    return _yaml(root / "profile" / "preferences.yaml")


def load_level(root: Path) -> dict[str, Any]:
    return _yaml(root / "profile" / "level.yaml")


def save_level(root: Path, level: dict[str, Any]) -> None:
    path = root / "profile" / "level.yaml"
    path.write_text(yaml.safe_dump(level, sort_keys=False, allow_unicode=True))


def load_terms(root: Path) -> list[dict[str, Any]]:
    terms = _yaml(root / "config" / "stack_terms.yaml").get("terms", [])
    seen: set[str] = set()
    for t in terms:
        if t["name"] in seen:
            raise ValueError(f"duplicate stack term: {t['name']}")
        seen.add(t["name"])
        if t.get("depth", "none") not in DEPTHS:
            raise ValueError(f"bad depth {t.get('depth')!r} for {t['name']} (expected one of {DEPTHS})")
    return terms


def load_curriculum(root: Path) -> list[dict[str, Any]]:
    modules = _yaml(root / "config" / "curriculum.yaml").get("modules", [])
    seen: set[str] = set()
    for m in modules:
        for lesson in m.get("lessons", []):
            if lesson["id"] in seen:
                raise ValueError(f"duplicate lesson id: {lesson['id']}")
            seen.add(lesson["id"])
    return modules


def load_feeds(root: Path) -> list[dict[str, Any]]:
    return _yaml(root / "config" / "news.yaml").get("feeds", [])


def load_resources(root: Path) -> dict[str, dict[str, str]]:
    """Short curriculum keys -> a display label and URL.

    Older string entries remain valid; structured entries let a lesson name an
    exact starting resource rather than exposing an opaque short key on the board.
    """
    out: dict[str, dict[str, str]] = {}
    for key, value in ((_yaml(root / "config" / "curriculum.yaml").get("resources") or {}).items()):
        if isinstance(value, dict):
            out[str(key)] = {"label": str(value.get("label") or key), "url": str(value["url"])}
        else:
            out[str(key)] = {"label": str(key), "url": str(value)}
    return out


def load_catalog(root: Path) -> dict[int, tuple[str, str]]:
    """LeetCode number -> (difficulty, tags) from config/leetcode_catalog.yaml."""
    raw = _yaml(root / "config" / "leetcode_catalog.yaml").get("problems") or {}
    out: dict[int, tuple[str, str]] = {}
    for k, v in raw.items():
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            out[int(k)] = (str(v[0]).lower(), str(v[1]))
    return out


def depth_rank(depth: str | None) -> int:
    return DEPTHS.index(depth) if depth in DEPTHS else 0


def is_gap(depth: str | None) -> bool:
    return depth_rank(depth) < depth_rank(GAP_BELOW)
