# Your first coaching session

Run `uv sync` and `uv run python scripts/init_workspace.py`.

Start with `profile/level.yaml`: goals, upcoming interviews, realistic time constraints, coding topics, system design, stack familiarity, and behavioral practice. `null` means unknown, not a low score. Ask your agent for a short assessment before assigning ratings.

`profile/preferences.yaml` controls available minutes, planning weights and spaced repetition. `config/curriculum.yaml` contains generic system-design lessons; `config/stack_terms.yaml` is a vocabulary, with all skill depths initially unknown (`none`). Add demonstrated depths in `profile/level.yaml` → `stack`.

Run `uv run studyplan plan --minutes 90 --no-news`. It creates a skeleton; ask the agent to replace placeholders with concrete exercises and links, then run `uv run studyplan plan check`. Edit plan JSON or use the CLI helpers; Markdown is generated from it.

`uv run studyplan serve` opens the local workflow at http://127.0.0.1:8765/. A block has a first action, bounded checklist, timer, and completion controls. The demo command prints the exact dated serve command for its isolated workspace.

Log real attempts with the LeetCode commands (`uv run studyplan leetcode --help`). Review schedules depend on outcomes, not imported notes. A completed reading item is exposure, not proof of independent understanding.
