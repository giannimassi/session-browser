"""
server.py — FastAPI server for the session-browser web app.

Serves the HTML shell, static assets, and REST API endpoints backed by
SQLite (via db.py) and the indexer (via indexer.py / parser.py).
"""

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import db
import indexer
import parser as session_parser

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_DB_DIR = Path.home() / ".cache" / "session-browser"
_DB_PATH = str(_DB_DIR / "sessions.db")
_STATIC_DIR = str(_HERE / "static")

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure cache directory exists
    _DB_DIR.mkdir(parents=True, exist_ok=True)

    # Initialize DB
    conn = db.init_db(_DB_PATH)
    app.state.conn = conn

    # Run initial scan
    indexer.scan_and_index(conn)
    app.state.last_scan_ts = time.time()

    yield

    # Cleanup
    conn.close()


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="session-browser", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8787",
        "http://127.0.0.1:8787",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

_INDEX_HTML = (_HERE / "templates" / "index.html").read_text()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCAN_DEBOUNCE_SECONDS = 30


def _maybe_reindex(request: Request) -> None:
    """Run an incremental re-index if more than DEBOUNCE seconds have passed."""
    now = time.time()
    if now - request.app.state.last_scan_ts > _SCAN_DEBOUNCE_SECONDS:
        conn = request.app.state.conn
        if indexer.check_staleness(conn):
            indexer.scan_and_index(conn, force=False)
        request.app.state.last_scan_ts = now


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def index():
    """Serve the HTML shell."""
    return HTMLResponse(_INDEX_HTML)


@app.get("/api/sessions")
async def list_sessions(
    request: Request,
    q: str | None = None,
    project: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "start_time",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
):
    """List sessions with optional filters, full-text search, and pagination."""
    _maybe_reindex(request)

    conn = request.app.state.conn
    rows, total = db.search_sessions(
        conn,
        q=q,
        project=project,
        repo=repo,
        branch=branch,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    filters = db.get_filter_options(conn)
    scanned_at = datetime.fromtimestamp(
        request.app.state.last_scan_ts, tz=timezone.utc
    ).isoformat()

    return JSONResponse({
        "sessions": rows,
        "total": total,
        "filters": filters,
        "scanned_at": scanned_at,
    })


@app.get("/api/sessions/{session_id}")
async def get_session(request: Request, session_id: str):
    """Return full parsed session detail including conversation and subagents."""
    conn = request.app.state.conn
    session_row = db.get_session(conn, session_id)
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    file_path = session_row.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Session file not found on disk")

    # Auto-detect subagents directory: same path as session file without extension
    session_file = Path(file_path)
    subagents_dir = str(session_file.with_suffix("") / "subagents")
    if not os.path.isdir(subagents_dir):
        subagents_dir = None

    detail = session_parser.extract_session_detail(file_path, subagents_dir=subagents_dir)
    return JSONResponse(detail)


@app.get("/api/sessions/{session_id}/subagents/{agent_id}")
async def get_subagent_transcript(
    request: Request,
    session_id: str,
    agent_id: str,
):
    """Return parsed transcript for a specific subagent within a session."""
    conn = request.app.state.conn
    session_row = db.get_session(conn, session_id)
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    file_path = session_row.get("file_path")
    if not file_path:
        raise HTTPException(status_code=404, detail="Session file path missing")

    # Construct subagent JSONL path: <session-dir>/<session_id>/subagents/<agent_id>.jsonl
    session_file = Path(file_path)
    subagent_path = session_file.parent / session_id / "subagents" / f"{agent_id}.jsonl"

    if not subagent_path.exists():
        raise HTTPException(status_code=404, detail="Subagent transcript not found")

    detail = session_parser.extract_session_detail(str(subagent_path))
    return JSONResponse(detail)


@app.post("/api/reindex")
async def force_reindex(request: Request):
    """Force a full re-index of all session files."""
    conn = request.app.state.conn
    stats = indexer.scan_and_index(conn, force=True)
    request.app.state.last_scan_ts = time.time()
    return JSONResponse(stats)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=8787,
        reload=False,
    )


if __name__ == "__main__":
    main()
