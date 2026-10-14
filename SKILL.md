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

## Chat-led learning flow

For a study request, reuse the existing daily plan and learner steps. Run `uv run studyplan --date YYYY-MM-DD session show`, read the matching step in `sessions/YYYY-MM-DD.json`, and resume the saved question and checkpoint. If there is no prepared day, curate it first; do not treat a generated skeleton as a completed lesson.

Ask the current question directly in the chat. Update it with `session question --block BLOCK --item ITEM --text "Question"`, using the global `--date` before `session`. Interpret each reply against the question actually asked, offer the feedback needed for another attempt, and ask one next question. Keep a multi-question exercise active until it is actually complete.

Save observed evidence with `session checkpoint --text "Evidence and remaining gap" --assistance guided|independent|unrecorded`. Complete only after an actual attempt or an explicit completion report. Pause, skip with a reason, and undo preserve history. A checkbox and an expired timer do not prove mastery or actual study time. Respect requests to pause or stop.

Open `http://127.0.0.1:8765/study?date=YYYY-MM-DD` alongside the chat. The learner page shows the current question, optional persistent timer and quiet controls; the calendar remains available. Asking a question does not start the timer. Start/Pause controls timing, and Restart at zero starts the same step's full timebox. This page does not send messages to an agent.

Learner content lives in ignored `sessions/YYYY-MM-DD.json`, keyed by plan item ID. Keep private case facts and answer keys in ignored `coaching/`; never put them in learner prompts, resources, or checkpoints. During mocks, reveal only requested facts and reserve assessment for debrief. See [the session format](docs/study-sessions.md). Exported hub pages are read-only views with a link back to the local session.
