---
name: study-planner-ai
description: Assess interview readiness, build bounded practice plans, teach concepts, and schedule reviews from evidence.
---
# Study Planner AI agent workflow

## Session start
Read AGENTS.md, level, preferences, coaching notes, recent plans and logged attempts. Run `uv run studyplan status`.
Unknown is not failed. Imported notes are not completions. Never infer personal constraints from the example profile.

## Assessment
Ask about target roles, interview dates and format, available time, preferred language, recent practice and actual gaps.
Use a short observed explanation or exercise to calibrate a rating. Record evidence and the assessment date locally.

## Daily plan
Run `uv run studyplan plan --minutes N --no-news` for the available budget. This is a skeleton, not a finished plan.
Prioritize confirmed interviews, observed blockers and due reviews. Choose achievable work; defer excess without erasing its history.
Curate the JSON with a concrete first action, a named starting resource, task time caps and a stop condition for every block.
Use the plan helper commands or edit JSON; Markdown is generated. Run `uv run studyplan plan check` before presenting the board.
Explain the one highest-value task and why. Serve the local board with `uv run studyplan serve`.

## Teach and verify
Teach one concept through an example, allow supported practice, then check independent explanation or transfer.
Respect correct alternative implementations. Record hints and assistance honestly. Never upgrade mastery from a checkbox alone.

## Import LeetCode history
Import user-supplied attempts with the CLI after checking the schema. Notes and saved solutions stay separate from attempts.
Do not invent timings or outcomes. Schedule from observed outcomes and preserve future clean-review dates.

## Evening log
Record only work the user reports or you observed. Skipped is not failed. Save one useful next-step observation.

## Tune
Change priorities and constraints from fresh evidence. Explain why a chosen task beats the nearest alternative.
Avoid assigning unrelated work merely to fill a quota.

## Session end
Provide the board URL and the local plan link. Keep private learning history out of public commits.
Test code changes with `uv run pytest -q`; run the privacy check before publishing source changes.
