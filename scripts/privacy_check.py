"""Check tracked files for accidental local state and common secret formats.

This is a guardrail, not a replacement for reviewing the staged diff.
"""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
private = {"profile", "data", "plans", "reports", "build", "site", "integrations", "Applied Job Resumes", ".venv", "node_modules"}
patterns = [
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("provider key", re.compile(rb"(?:sk-[A-Za-z0-9_-]{32,}|AIza[A-Za-z0-9_-]{35})")),
    ("machine home path", re.compile(rb"/(?:Users|home)/[A-Za-z0-9_.-]+/")),
]
failures=[]
for name in filter(None,files):
    p=Path(name)
    if p.parts[0] in private or (p.name.startswith(".env") and p.name != ".env.example"):
        failures.append((name,"private workspace file"))
    path=root/name
    if not path.is_file(): continue
    for label,pattern in patterns:
        if pattern.search(path.read_bytes()): failures.append((name,label))
for name,reason in failures: print(f"FAIL {name}: {reason}")
print(f"Privacy check: {len([f for f in files if f])} tracked paths, {len(failures)} finding(s).")
sys.exit(bool(failures))
