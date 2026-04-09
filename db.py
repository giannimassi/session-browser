"""
db.py — SQLite operations for session-browser, including FTS5 full-text search.
"""

import json
import sqlite3
from typing import Any

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    project_cwd         TEXT,
    project_name        TEXT,
    git_branch          TEXT,
    branches_seen       TEXT,
    version             TEXT,
    start_time          TEXT,
    end_time            TEXT,
    duration_seconds    INTEGER,
    first_prompt        TEXT,
    turn_count          INTEGER,
    token_usage         TEXT,
    estimated_cost_usd  REAL,
    repos_touched       TEXT,
    tool_counts         TEXT,
    has_subagents       BOOLEAN,
    file_size_bytes     INTEGER,
    file_path           TEXT,
    file_mtime          REAL,
    indexed_at          TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    first_prompt,
    search_text,
    repos_touched,
    git_branch,
    content=sessions,
    content_rowid=rowid
);
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_db(db_path: str) -> sqlite3.Connection:
    """Create tables if not exist, enable WAL mode, return a connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


def upsert_session(conn: sqlite3.Connection, session_data: dict) -> None:
    """Insert or replace a session row and keep the FTS index in sync.

    ``session_data`` keys match the ``sessions`` column names.  The extra key
    ``search_text`` is written to the FTS virtual table only — it is not stored
    in the base table.

    Implementation note: SQLite's ``INSERT OR REPLACE`` deletes the old row and
    inserts a new one, which changes the rowid.  To keep the FTS content table
    consistent we must:

    1. Fetch the *old* rowid (if any) and delete its FTS entry.
    2. Run the base-table upsert (which may assign a new rowid).
    3. Fetch the *new* rowid and insert the fresh FTS entry.
    """
    # Separate the FTS-only field before touching the base table.
    search_text: str = session_data.pop("search_text", "") or ""

    columns = [
        "session_id", "project_cwd", "project_name", "git_branch",
        "branches_seen", "version", "start_time", "end_time",
        "duration_seconds", "first_prompt", "turn_count", "token_usage",
        "estimated_cost_usd", "repos_touched", "tool_counts", "has_subagents",
        "file_size_bytes", "file_path", "file_mtime", "indexed_at",
    ]

    row: dict[str, Any] = {col: session_data.get(col) for col in columns}

    placeholders = ", ".join(["?"] * len(columns))
    col_list = ", ".join(columns)
    values = [row[c] for c in columns]
    session_id = row["session_id"]

    with conn:
        # Step 1: grab the old rowid + FTS content (if the row already exists)
        # so we can delete the stale FTS entry *before* the rowid changes.
        old = conn.execute(
            "SELECT rowid, first_prompt, repos_touched, git_branch"
            " FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        if old is not None:
            conn.execute(
                "INSERT INTO sessions_fts"
                "(sessions_fts, rowid, first_prompt, search_text, repos_touched, git_branch)"
                " VALUES('delete', ?, ?, ?, ?, ?)",
                (
                    old["rowid"],
                    old["first_prompt"] or "",
                    "",   # we don't store search_text in sessions; use empty placeholder
                    old["repos_touched"] or "",
                    old["git_branch"] or "",
                ),
            )

        # Step 2: upsert the base row (may reassign rowid via DELETE+INSERT).
        conn.execute(
            f"INSERT OR REPLACE INTO sessions ({col_list}) VALUES ({placeholders})",
            values,
        )

        # Step 3: fetch the new rowid and insert the fresh FTS entry.
        new_rowid_row = conn.execute(
            "SELECT rowid FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if new_rowid_row is None:
            return
        new_rowid: int = new_rowid_row[0]

        conn.execute(
            "INSERT INTO sessions_fts"
            "(rowid, first_prompt, search_text, repos_touched, git_branch)"
            " VALUES(?, ?, ?, ?, ?)",
            (
                new_rowid,
                row.get("first_prompt") or "",
                search_text,
                row.get("repos_touched") or "",
                row.get("git_branch") or "",
            ),
        )


def search_sessions(
    conn: sqlite3.Connection,
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
) -> tuple[list[dict], int]:
    """Return (rows, total_count) matching the given filters.

    When ``q`` is provided the query uses FTS5 MATCH and ranks by relevance
    (when ``sort`` is ``"relevance"`` or ``sort`` is the default and ``q`` is
    set).  All other filters become WHERE clauses on the sessions table.
    """
    # Whitelist sort columns to prevent SQL injection.
    _allowed_sorts = {
        "start_time", "end_time", "duration_seconds", "estimated_cost_usd",
        "turn_count", "project_name", "git_branch", "relevance",
    }
    sort_col = sort if sort in _allowed_sorts else "start_time"
    order_dir = "DESC" if order.lower() == "desc" else "ASC"

    params: list[Any] = []
    where_clauses: list[str] = []

    if project:
        where_clauses.append("s.project_name = ?")
        params.append(project)

    if repo:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM json_each(s.repos_touched) WHERE value = ?)"
        )
        params.append(repo)

    if branch:
        where_clauses.append("s.git_branch = ?")
        params.append(branch)

    if date_from:
        where_clauses.append("s.start_time >= ?")
        params.append(date_from)

    if date_to:
        where_clauses.append("s.start_time <= ?")
        params.append(date_to)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    if q:
        # FTS path: join sessions with sessions_fts via rowid.
        # Quote each search term so hyphens etc. are treated as literals.
        fts_query = " ".join(f'"{t}"' for t in q.split() if t)
        fts_params = [fts_query] + params
        fts_where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        if sort_col == "relevance":
            order_expr = f"fts.rank {order_dir}"
        else:
            order_expr = f"s.{sort_col} {order_dir}"

        count_sql = f"""
            SELECT COUNT(*)
            FROM sessions_fts fts
            JOIN sessions s ON s.rowid = fts.rowid
            {fts_where}
            AND sessions_fts MATCH ?
        """
        # COUNT query: FTS param first, then filter params.
        count_params = [q] + params

        # For COUNT we need the MATCH before the WHERE join filters — rewrite:
        # Actually, the MATCH must appear as a WHERE filter on the FTS table.
        # Rebuild with the correct parameter order.
        count_sql = f"""
            SELECT COUNT(*)
            FROM sessions_fts fts
            JOIN sessions s ON s.rowid = fts.rowid
            WHERE fts.sessions_fts MATCH ?
            {"AND " + " AND ".join(where_clauses) if where_clauses else ""}
        """
        count_params = [fts_query] + params

        rows_sql = f"""
            SELECT s.*
            FROM sessions_fts fts
            JOIN sessions s ON s.rowid = fts.rowid
            WHERE fts.sessions_fts MATCH ?
            {"AND " + " AND ".join(where_clauses) if where_clauses else ""}
            ORDER BY {order_expr}
            LIMIT ? OFFSET ?
        """
        rows_params = [fts_query] + params + [limit, offset]

    else:
        # Plain path: query sessions table directly.
        if sort_col == "relevance":
            sort_col = "start_time"
        order_expr = f"s.{sort_col} {order_dir}"

        count_sql = f"SELECT COUNT(*) FROM sessions s {where_sql}"
        count_params = params[:]

        rows_sql = f"""
            SELECT s.*
            FROM sessions s
            {where_sql}
            ORDER BY {order_expr}
            LIMIT ? OFFSET ?
        """
        rows_params = params + [limit, offset]

    total: int = conn.execute(count_sql, count_params).fetchone()[0]
    raw_rows = conn.execute(rows_sql, rows_params).fetchall()
    rows = [dict(r) for r in raw_rows]

    return rows, total


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """Return a single session by ID, or None if not found."""
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def get_stale_files(
    conn: sqlite3.Connection,
    file_entries: list[tuple[str, float]],
) -> list[str]:
    """Given a list of (file_path, current_mtime), return paths that need
    re-indexing (either not yet in the DB or with a changed mtime).
    """
    if not file_entries:
        return []

    stale: list[str] = []
    for file_path, current_mtime in file_entries:
        row = conn.execute(
            "SELECT file_mtime FROM sessions WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is None or row["file_mtime"] != current_mtime:
            stale.append(file_path)

    return stale


def get_filter_options(conn: sqlite3.Connection) -> dict:
    """Return distinct values for filter dropdowns.

    Returns::

        {
            "projects": ["name1", ...],
            "repos":    ["repo1", ...],
            "branches": ["main", ...],
        }
    """
    projects = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT project_name FROM sessions"
            " WHERE project_name IS NOT NULL ORDER BY project_name"
        ).fetchall()
    ]

    # Expand the repos_touched JSON arrays into individual values.
    repos = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT je.value"
            " FROM sessions, json_each(sessions.repos_touched) je"
            " WHERE je.value IS NOT NULL ORDER BY je.value"
        ).fetchall()
    ]

    branches = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT git_branch FROM sessions"
            " WHERE git_branch IS NOT NULL ORDER BY git_branch"
        ).fetchall()
    ]

    return {"projects": projects, "repos": repos, "branches": branches}


def get_last_scan_time(conn: sqlite3.Connection) -> float | None:
    """Return the maximum ``indexed_at`` timestamp across all sessions, or None."""
    row = conn.execute("SELECT MAX(indexed_at) FROM sessions").fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None
