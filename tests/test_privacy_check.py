"""Exercise public-payload checks without using any real credentials or profile data."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def payload(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/privacy_check.py", tmp_path / "scripts/privacy_check.py")
    return tmp_path

def check(root):
    return subprocess.run([sys.executable, "scripts/privacy_check.py"], cwd=root, capture_output=True, text=True)

@pytest.mark.parametrize("folder", ["profile", "data", "sessions", "coaching"])
def test_force_added_private_state_is_rejected(payload, folder):
    (payload / ".gitignore").write_text(f"/{folder}/\n")
    (payload / folder).mkdir()
    (payload / folder / "example.json").write_text("{}")
    assert check(payload).returncode == 0
    subprocess.run(["git", "add", "-f", f"{folder}/example.json"], cwd=payload, check=True)
    result = check(payload)
    assert result.returncode == 1 and "private workspace file" in result.stdout

def test_untracked_secret_is_checked_before_staging(payload):
    (payload / "accidental.txt").write_text("gh" + "p_" + "x" * 40)
    result = check(payload)
    assert result.returncode == 1 and "GitHub token" in result.stdout
    assert "x" * 40 not in result.stdout

def test_symlink_is_not_followed(payload):
    (payload / "alias.txt").symlink_to("missing-private-target")
    result = check(payload)
    assert result.returncode == 1 and "symlink" in result.stdout
