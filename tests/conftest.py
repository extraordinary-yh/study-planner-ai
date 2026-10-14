import shutil
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
FIXTURES = HERE / "fixtures"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """A scratch copy of the sub-project (config + profile, empty data) next to a fixture job-radar parent."""
    root = tmp_path / "study-plan"
    root.mkdir()
    shutil.copytree(PROJECT / "config", root / "config")
    shutil.copytree(PROJECT / "examples" / "profile", root / "profile")
    import yaml
    term_path = root / "config" / "stack_terms.yaml"
    terms = yaml.safe_load(term_path.read_text())
    for term in terms["terms"]:
        term["depth"] = "none" if term["name"] in {"Java", "Distributed systems", "Go", "Kafka", "FastAPI", "Kubernetes", "LangChain/LangGraph", "RL environments", "Rust"} else "working"
    term_path.write_text(yaml.safe_dump(terms))

    shutil.copy(FIXTURES / "preferences.yaml", root / "profile" / "preferences.yaml")
    shutil.copy(FIXTURES / "level_blank.yaml", root / "profile" / "level.yaml")   # tests start from an unassessed learner
    (root / "profile" / "notes.md").unlink(missing_ok=True)
    (root / "profile" / "coaching.yaml").unlink(missing_ok=True)  # personal evidence is opt-in in scenario tests
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (root / "data").mkdir()
    (root / "plans").mkdir()
    parent = tmp_path / "job-radar"
    shutil.copytree(FIXTURES / "parent", parent)
    monkeypatch.setenv("STUDYPLAN_ROOT", str(root))
    monkeypatch.setenv("JOBRADAR_ROOT", str(parent))
    return root, parent


PW_FALLBACK = Path("/opt/pw-browsers/chromium")     # pre-installed build whose revision may not match the pin


def _launch(pw):
    """The bundled build first; a pre-installed Chromium second (revision mismatches are common on CI images)."""
    try:
        return pw.chromium.launch()
    except Exception:
        if not PW_FALLBACK.exists():
            raise
        return pw.chromium.launch(executable_path=str(PW_FALLBACK))


@pytest.fixture(scope="session")
def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            _launch(pw).close()
        return True
    except Exception:
        return False


@pytest.fixture
def browser(chromium_available):
    if not chromium_available:
        pytest.skip("Playwright Chromium cannot launch (uv run playwright install chromium)")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = _launch(pw)
        yield b
        b.close()
