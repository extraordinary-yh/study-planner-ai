"""Copy fictional examples into an ignored workspace, without overwriting your files."""
from pathlib import Path
import shutil
root = Path(__file__).resolve().parents[1]
for folder in ("profile", "config"):
    source = root / "examples" / folder
    if not source.exists():
        continue
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        target = root / folder / path.relative_to(source)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
print("Workspace ready. Customize profile/ locally; it is ignored by Git.")
