# Session Browser

Browse and search your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) conversation history in a local web UI.

![Session list](https://github.com/user-attachments/assets/placeholder-list.png)

## Features

- **Session list** with sortable columns: date, repos touched, first prompt, branch, duration, cost, turns
- **Full-text search** across all user messages and assistant responses (SQLite FTS5)
- **Filter** by workspace/repo, date range (today / 7d / 30d / all)
- **Session detail view** with chat-style conversation rendering:
  - Markdown rendering with syntax-highlighted code blocks
  - Collapsible thinking blocks
  - Collapsible tool calls with one-line summaries (Bash commands, file operations, agent dispatches)
  - Tool results with large output truncation
  - Subagent transcript navigation
- **Auto-indexing** with incremental re-scan (only re-indexes changed files)
- **Repo detection** from file paths in tool calls (auto-discovers git repos)
- **Dark/light theme** toggle
- **No LLM involved** -- pure data extraction and rendering

## How it works

Claude Code stores conversation transcripts as JSONL files in `~/.claude/projects/`. Session Browser indexes these files into a local SQLite database and serves a web UI for browsing them.

```
~/.claude/projects/**/*.jsonl  ->  [Indexer]  ->  SQLite  ->  [FastAPI]  ->  [Browser]
```

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/giannimassi/session-browser.git
cd session-browser
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Usage

```bash
.venv/bin/python3 server.py
```

Open http://127.0.0.1:8787 in your browser.

On first launch, all sessions are indexed (takes 2-5 seconds for ~300 sessions). Subsequent launches only re-index new or modified files.

## Data

Session Browser is **read-only**. It never modifies your Claude Code data. The SQLite index is stored at `~/.cache/session-browser/sessions.db` and can be safely deleted (it will be rebuilt on next launch).

### What gets indexed

Per session:
- Session ID, project, git branch, timestamps, duration
- First user prompt
- Token usage and estimated cost (Opus pricing)
- Tool call counts
- Repos touched (auto-detected from file paths in Read/Write/Edit/Bash tool calls)
- Full text of user messages and assistant responses (for search)

### What gets parsed on detail view

- Complete conversation with user/assistant messages
- Thinking blocks (collapsed by default)
- Tool calls with inputs and results
- Subagent transcripts (linked from Agent tool calls)

## Tech stack

- **Python** -- FastAPI + uvicorn
- **SQLite** with FTS5 -- session index and full-text search
- **Vanilla JS + CSS** -- no build step, no node_modules
- **marked.js** + **highlight.js** -- vendored for markdown/code rendering

## Configuration

The server binds to `127.0.0.1:8787` by default. The Claude data directory defaults to `~/.claude`. Both can be changed in `server.py`.

## License

MIT
