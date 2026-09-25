# Optional integrations

The planner works alone. To include job context, set `JOBRADAR_ROOT` to a Job Radar AI checkout. Without it, reads are confined to an empty `integrations/job-radar/` directory in this project, never a parent directory chosen by accident.

Calendar import is read-only. Use `studyplan plan import-events` with exported JSON or set `STUDYPLAN_ICAL_URLS` in the environment. Calendar URLs are secrets; never commit them. The board does not create calendar events automatically.

News feeds in `config/news.yaml` start disabled. Enable selected feeds explicitly or keep `--no-news`.

The optional RemNote importer needs your endpoint, `REMNOTE_MCP_AUTH`, and an ignored `config/remnote.yaml` containing `collections: {Label: your-document-id}`. No account identifiers ship with the template. Imported notes are exposure only.

`studyplan hub build --read-only` builds a local static archive. Its internal `study-plan/` references are a virtual file namespace, not a required parent checkout. Keep this archive private: it can contain your plans and notes. Hosted GitHub writeback from the predecessor monorepo is disabled in this standalone edition; use the local board for editing.
