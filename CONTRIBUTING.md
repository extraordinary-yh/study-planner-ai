# Contributing

1. Fork and clone the repository; run `uv sync`.
2. Reproduce a bug using a small **fictional** fixture. Do not attach resumes, calendars, private postings, access tokens, or personal records.
3. Make the smallest useful change and run `uv run pytest -q`. Browser checks require `uv run playwright install chromium` (on Linux CI, add `--with-deps`).
4. Run `uv run python scripts/privacy_check.py` after staging and inspect `git diff --cached`.
5. Explain the user-visible change and the checks you ran in your contribution.

Keep the CLI deterministic; agent reasoning belongs in visible workflow guidance and saved evidence. Never infer a completed application, approved claim, or mastered concept from file existence alone.
