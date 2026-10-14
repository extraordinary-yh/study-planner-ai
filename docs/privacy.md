# Privacy model

Public: code, tests with synthetic fixtures, setup guides, fictional examples, and screenshots made from those examples.

Local and Git-ignored: `profile/`, `data/`, `plans/`, `reports/`, `build/`, `site/`, `integrations/`, `sessions/`, `coaching/`, `Applied Job Resumes/`, and `.env*` except `.env.example`. Never force-add them to a public fork. Use a separate private repository or encrypted backup if you want to version your career workspace.

The demo reads `examples/` and writes only `build/demo/`. Initialization copies missing files, never overwrites your existing profile. Test runs use temporary directories and fictional examples.

There is no telemetry. Live scans contact the configured public job boards. Calendar URLs, optional model tools, and notification services contact their configured providers only when you use them. A coding agent has its own data-handling settings: review those before giving it private documents.

Before publishing: inspect `git diff --cached`, run `scripts/privacy_check.py`, and inspect any binary artifacts. The script catches common tokens and accidental workspace paths, not every kind of personal information. If a secret reaches Git history, rotate it and follow your hosting provider's history-removal process.

The privacy check includes untracked, non-ignored files before staging and rejects public symlinks. Coach-only facts and active learning state stay local. Review file contents as well as the automated results.
