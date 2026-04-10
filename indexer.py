"""
indexer.py — Scan Claude Code session directories, extract metadata, store in SQLite.

Uses parser.extract_index_metadata for per-file extraction and db.upsert_session
for storage. Designed for incremental re-indexing: only new/modified files are
re-parsed unless force=True.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from db import get_stale_files, upsert_session
from parser import extract_index_metadata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project name derivation
# ---------------------------------------------------------------------------


def derive_project_name(cwd: str) -> str:
    """Derive a friendly project name from a CWD path.

    Uses the last meaningful segment of the path. For real paths like
    /Users/x/dev/myproject, returns "myproject". For encoded-CWD paths
    like -Users-x-dev-myproject (used by Claude Code), decodes and does
    the same.
    """
    if not cwd:
        return "unknown"

    expanded = os.path.expanduser(cwd)

    # Real path — use basename, or parent basename if it's a hidden dir
    if "/" in expanded:
        p = Path(expanded).resolve() if os.path.exists(expanded) else Path(expanded)
        name = p.name
        if name.startswith("."):
            return p.parent.name or "unknown"
        return name or "unknown"

    # Encoded CWD (dashes instead of slashes): -Users-x-dev-myproject
    if cwd.startswith("-"):
        parts = [p for p in cwd.strip("-").split("-") if p]
        if parts:
            return parts[-1]

    return "unknown"


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def _find_jsonl_files(projects_dir: Path) -> list[tuple[str, float]]:
    """Walk projects_dir, return list of (file_path, mtime) for all .jsonl files.

    Skips any file under a 'subagents/' directory.
    """
    results: list[tuple[str, float]] = []

    for root, dirs, files in os.walk(projects_dir):
        # Skip subagents directories entirely (prune from walk)
        if "subagents" in dirs:
            dirs.remove("subagents")

        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            full_path = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(full_path)
            except OSError:
                continue
            results.append((full_path, mtime))

    return results


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


def scan_and_index(conn, claude_dir: str = "~/.claude", force: bool = False) -> dict:
    """Scan Claude Code session directories and index metadata into SQLite.

    Args:
        conn: SQLite connection (from db.init_db).
        claude_dir: Path to the .claude directory.
        force: If True, re-index all files regardless of mtime.

    Returns:
        dict with keys: indexed, new, updated, duration_ms.
    """
    t0 = time.monotonic()

    claude_path = Path(os.path.expanduser(claude_dir))
    projects_dir = claude_path / "projects"

    if not projects_dir.is_dir():
        logger.warning("Projects directory not found: %s", projects_dir)
        return {"indexed": 0, "new": 0, "updated": 0, "duration_ms": 0}

    # Step 1-2: Find all JSONL files
    all_files = _find_jsonl_files(projects_dir)
    logger.info("Found %d JSONL files in %s", len(all_files), projects_dir)

    # Step 3-4: Determine which files need indexing
    if force:
        stale_paths = [fp for fp, _ in all_files]
        mtime_map = {fp: mt for fp, mt in all_files}
    else:
        stale_paths = get_stale_files(conn, all_files)
        mtime_map = {fp: mt for fp, mt in all_files}

    logger.info("%d files need indexing (force=%s)", len(stale_paths), force)

    # Track stats: check which are new vs updated
    existing_paths = set()
    if stale_paths:
        for path in stale_paths:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE file_path = ?", (path,)
            ).fetchone()
            if row is not None:
                existing_paths.add(path)

    indexed = 0
    new_count = 0
    updated_count = 0

    # Step 5: Extract and upsert each stale file
    for file_path in stale_paths:
        try:
            meta = extract_index_metadata(file_path)
        except Exception:
            logger.warning("Failed to parse %s, skipping", file_path, exc_info=True)
            continue

        if not meta.get("session_id"):
            logger.warning("No session_id in %s, skipping", file_path)
            continue

        # Derive project_name from project_cwd
        project_cwd = meta.get("project_cwd") or ""
        meta["project_name"] = derive_project_name(project_cwd)

        # Convert list/dict fields to JSON strings
        for key in ("branches_seen", "token_usage", "repos_touched", "tool_counts"):
            val = meta.get(key)
            if val is not None and not isinstance(val, str):
                meta[key] = json.dumps(val)

        # Add indexer-level fields
        meta["file_path"] = file_path
        meta["file_mtime"] = mtime_map.get(file_path, 0.0)
        meta["indexed_at"] = datetime.now(timezone.utc).isoformat()

        try:
            upsert_session(conn, meta)
        except Exception:
            logger.warning("Failed to upsert session from %s", file_path, exc_info=True)
            continue

        indexed += 1
        if file_path in existing_paths:
            updated_count += 1
        else:
            new_count += 1

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "Indexing complete: %d indexed (%d new, %d updated) in %dms",
        indexed, new_count, updated_count, elapsed_ms,
    )

    return {
        "indexed": indexed,
        "new": new_count,
        "updated": updated_count,
        "duration_ms": elapsed_ms,
    }


def check_staleness(conn, claude_dir: str = "~/.claude") -> bool:
    """Quick check: are there any new/modified JSONL files since last scan?

    Returns True if re-indexing is needed, False otherwise.
    """
    claude_path = Path(os.path.expanduser(claude_dir))
    projects_dir = claude_path / "projects"

    if not projects_dir.is_dir():
        return False

    all_files = _find_jsonl_files(projects_dir)
    if not all_files:
        return False

    stale = get_stale_files(conn, all_files)
    return len(stale) > 0
