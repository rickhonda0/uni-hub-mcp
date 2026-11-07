# Uni hub: Canvas and Ed study corpus with a read-only MCP server

This folder syncs University of Sydney Canvas course material and Ed Discussion threads into a
local, searchable corpus, and exposes it to AI agents through a read-only MCP server.

## What gets synced

- **Canvas** (`canvas.sydney.edu.au`): every module item. That covers lecture slides, tutorials,
  labs, assignments, quizzes, pages, links and discussions. It also includes any file linked
  from inside a page, an assignment or an announcement, plus the announcements and syllabus.
  Students cannot list course files or pages directly, so the sync reaches them through
  modules, the same way the Canvas UI does.
- **Ed Discussion** (`edstem.org/api`): every thread in the unit's Ed course, with answers and
  comments. People are reduced to their role (Staff, Tutor, Student, Anonymous).
- **Local files** (`uni-hub ingest`): lecture transcripts (`.vtt`/`.srt`/`.txt`), your own
  notes, and any other supported file.

Files are converted to markdown in `corpus/<UNIT>/<source>/`. Originals are kept in
`raw/<UNIT>/`, and the index and metadata are in `state/hub.db`. Each document gets a unit, a
kind (lecture, tutorial, exam, assignment, quiz, forum, ...) and a teaching week, all inferred
from module names and titles. Tags flag worked solutions (`solutions`), PDFs with no text layer
(`needs-vision`) and staff-answered Ed threads (`staff-reply`).

## Commands

Run these from this folder, or call `.venv/Scripts/uni-hub.exe` by its full path.

```
uv sync                                   # first-time setup
uv run uni-hub courses                    # Canvas units the hub can see
uv run uni-hub sync                       # current semester's units (incremental)
uv run uni-hub sync --unit MATH1902       # a specific (e.g. past) unit; repeatable
uv run uni-hub ingest <path> --unit MATH2070 --kind transcript
uv run uni-hub status
uv run uni-hub search "complementary slackness" --unit MATH2070
```

Re-running `sync` only fetches what changed.

## MCP server

Tools: `list_units`, `unit_outline`, `search`, `read_doc`, `get_deadlines` (live) and
`get_grades` (live). Every tool is read-only, and the server has no code path that can submit,
post or mark anything.

Register it for every Claude Code session:

```
claude mcp add uni-hub --scope user -- C:/Users/RickHonda/personal/uni/ED_CANVAS_API_HUB/.venv/Scripts/uni-hub-mcp.exe
```

Remove it with `claude mcp remove uni-hub --scope user`.

## Security notes

- `.env` holds `CANVAS_API_KEY` and `EDSTEM_API_KEY`. Both tokens carry your full account
  rights, including submitting work and posting. The code sends them only to
  `canvas.sydney.edu.au` and `edstem.org`, and drops the Canvas token on redirects to file
  storage.
- If a token leaks, revoke it. For Canvas: Account > Settings > Approved Integrations. For Ed:
  Settings > API Tokens.
- `corpus/` and `raw/` contain copyrighted course material. Keep them private.
- Fetching an Ed thread through the API may mark it as read in Ed's web UI.
