# Focused study sessions

Use your existing plan item IDs to prepare `sessions/YYYY-MM-DD.json` locally. Every item in a focused day needs a learner step. For example:

```json
{
  "steps": {
    "design-1": {
      "title": "Trace one request",
      "goal": "Explain a request and one failure boundary.",
      "prompt": "How does an HTTP request reach your application?",
      "minutes": 10,
      "resources": [{"label": "HTTP overview", "url": "https://developer.mozilla.org/en-US/docs/Web/HTTP/Overview"}]
    }
  }
}
```

This is fictional learner content. Use optional `coach_ref: study-plan/coaching/case.json` only for a local coach file; it is not loaded into the learner payload. The `study-plan/` prefix is a virtual resource namespace pointing to this checkout, even when it is installed independently. `./docs/example.md` works for local first actions. Parent job-search references require an explicit `JOBRADAR_ROOT`.

Run `uv run studyplan --date YYYY-MM-DD plan check`, then `uv run studyplan --date YYYY-MM-DD serve`. Open `/study?date=YYYY-MM-DD` on the local board. Session commands include show, question, checkpoint, start, pause, reset_timer, complete, skip and undo. Session updates persist in `data/active-study/`; ordinary plan state remains the completion source.

The fictional demo includes prepared learner steps: `uv run python scripts/demo.py --serve`. Never use real learner history for screenshots or UI tests.
