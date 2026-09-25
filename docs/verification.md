# Verification

Initial extraction checks:

- Gitleaks 8.30.1 scan of the staged public source: zero findings.
- Public-payload privacy checks: zero findings.
- Full local suite: **106 passed**, including available browser checks.
- Documented offline demo executed successfully.
- Public source starts with fresh Git history; private predecessor history was never copied.
- Live profiles, calendars, records and generated documents are excluded from the public payload.
- Screenshots were generated from fictional examples.

The CI badge reflects checks on the current public branch. Optional sidecar tests are skipped when the external Node checkout is not installed.

Fresh staged-file export, with no personal workspace or predecessor checkout: **106 passed**. `uv sync --locked` and the documented offline demo both succeeded.
