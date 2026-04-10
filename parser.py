"""
parser.py — Parse Claude Code JSONL session transcripts.

Three extraction tiers:
  1. extract_metadata_lite   — head/tail read only (fast discovery)
  2. extract_index_metadata  — full stream, index-level fields
  3. extract_session_detail  — full parse for the detail/chat view
"""

import json
import os
import re
import glob as glob_mod
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Pricing (Opus 4.6, update when Anthropic changes)
# ---------------------------------------------------------------------------

PRICE_PER_M = {
    "input": 15.0,
    "output": 75.0,
    "cache_create": 18.75,
    "cache_read": 1.50,
}

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

LITE_READ_BUF_SIZE = 65536


def stream_jsonl(path: str):
    """Yield parsed records one at a time without loading the full file."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def read_head_tail(path: str):
    """Read first and last 64KB of a file. Returns (head_str, tail_str, file_size)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head_bytes = f.read(LITE_READ_BUF_SIZE)
        head = head_bytes.decode("utf-8", errors="replace")

        if size <= LITE_READ_BUF_SIZE:
            return head, head, size

        f.seek(max(0, size - LITE_READ_BUF_SIZE))
        tail_bytes = f.read(LITE_READ_BUF_SIZE)
        tail = tail_bytes.decode("utf-8", errors="replace")

    return head, tail, size


def extract_json_field(text: str, key: str):
    """Extract a JSON string field value without full parsing.
    Matches '"key":"value"' or '"key": "value"' patterns."""
    for pattern in [f'"{key}":"', f'"{key}": "']:
        idx = text.find(pattern)
        if idx < 0:
            continue
        start = idx + len(pattern)
        i = start
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                return text[start:i]
            i += 1
    return None


def parse_ts(ts_str: str | None) -> datetime | None:
    """Parse ISO 8601 timestamp string to datetime."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _compute_cost(tokens: dict) -> float:
    """Compute estimated USD cost from a token usage dict."""
    return (
        tokens.get("input_tokens", 0) / 1_000_000 * PRICE_PER_M["input"]
        + tokens.get("output_tokens", 0) / 1_000_000 * PRICE_PER_M["output"]
        + tokens.get("cache_creation_input_tokens", 0) / 1_000_000 * PRICE_PER_M["cache_create"]
        + tokens.get("cache_read_input_tokens", 0) / 1_000_000 * PRICE_PER_M["cache_read"]
    )


def _get_content_blocks(content) -> list[dict]:
    """Normalise message content to a list of block dicts."""
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _extract_text_from_content(content) -> str:
    """Extract plain text from message content (string or block list)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in _get_content_blocks(content):
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _is_system_noise(text: str) -> bool:
    """Detect system-injected content that shouldn't appear in conversation view."""
    s = text.lstrip()
    return (
        s.startswith("<system-reminder>")
        or s.startswith("<local-command-caveat>")
        or s.startswith("$begin plugin")
        or s.startswith("(no content)")
    )


def _is_user_text_message(content) -> bool:
    """Return True if a user message has real text (not just tool_results)."""
    if isinstance(content, str):
        return bool(content.strip()) and not _is_system_noise(content)
    for block in _get_content_blocks(content):
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text and not _is_system_noise(text):
                return True
    return False


# ---------------------------------------------------------------------------
# Repo derivation from file paths
# ---------------------------------------------------------------------------

_CMD_PATH_PATTERNS = [
    # git -C <path>, go -C <path>, make -C <path>
    re.compile(r"(?:git|go|make)\s+-C\s+([^\s;|&]+)"),
    # npm --prefix <path>
    re.compile(r"npm\s+--prefix\s+([^\s;|&]+)"),
    # terraform -chdir=<path>
    re.compile(r"terraform\s+-chdir=([^\s;|&]+)"),
]

# Cache of path -> git root lookups (populated lazily)
_git_root_cache: dict[str, str | None] = {}


def _find_git_root(path: str) -> str | None:
    """Walk up from path to find the nearest .git directory.
    Returns the repo root directory, or None if not in a git repo.
    Uses a cache to avoid repeated filesystem walks."""
    expanded = os.path.expanduser(path)

    # If it's a file, start from its parent
    if not os.path.isdir(expanded):
        expanded = os.path.dirname(expanded)

    # Check cache for this path or any parent
    check = expanded
    while check and check != "/":
        if check in _git_root_cache:
            return _git_root_cache[check]
        check = os.path.dirname(check)

    # Walk up looking for .git (stop before home dir — dotfiles repos aren't projects)
    home = os.path.expanduser("~")
    current = expanded
    while current and current != "/" and current != home:
        if os.path.isdir(os.path.join(current, ".git")):
            # Cache this and all intermediate paths
            cache_path = expanded
            while cache_path != current and cache_path != "/":
                _git_root_cache[cache_path] = current
                cache_path = os.path.dirname(cache_path)
            _git_root_cache[current] = current
            return current
        current = os.path.dirname(current)

    # No git root found — cache the miss
    _git_root_cache[expanded] = None
    return None


def _path_to_repo(path: str) -> str | None:
    """Map a file/directory path to a repo name.

    First tries to find a git root on disk (fast, cached). If the path
    no longer exists (e.g., deleted worktree from a past session), falls
    back to heuristic extraction from the path structure.
    """
    if not path:
        return None

    expanded = os.path.expanduser(path)

    # Skip paths inside ~/.claude — label as "claude-config"
    if "/.claude/" in expanded:
        return "claude-config"

    # Try filesystem-based git root detection first
    git_root = _find_git_root(expanded)
    if git_root:
        return os.path.basename(git_root)

    # Path doesn't exist on disk — extract repo name heuristically.
    # Only match paths under common development directories.
    home = os.path.expanduser("~")
    if not expanded.startswith(home):
        return None

    rel = expanded[len(home):].strip("/")
    parts = rel.split("/")
    if len(parts) < 2:
        return None

    # Only consider paths under known dev-like top-level dirs
    dev_roots = {"dev", "projects", "repos", "src", "code", "workspace", "go"}
    if parts[0] not in dev_roots:
        return None

    # ~/dev/<repo>/... -> repo  OR  ~/dev/<org>/<repo>/... -> repo
    # Take the deepest directory that's at most 3 levels under home
    # and has siblings (i.e., it's not a leaf file)
    if len(parts) >= 3:
        return parts[2]  # ~/dev/org/repo
    if len(parts) >= 2:
        return parts[1]  # ~/dev/repo

    return None


def _collect_repos(file_paths: set[str], bash_commands: list[str]) -> list[str]:
    """Derive sorted unique repo names from file paths and bash commands."""
    raw_repos: set[str] = set()

    for fp in file_paths:
        repo = _path_to_repo(fp)
        if repo:
            raw_repos.add(repo)

    for cmd in bash_commands:
        for pat in _CMD_PATH_PATTERNS:
            for m in pat.finditer(cmd):
                repo = _path_to_repo(m.group(1))
                if repo:
                    raw_repos.add(repo)

    # Normalize worktree names: worktrees are named <repo>-<branch-slug>.
    # If a name contains a dash, check if its prefix (up to any dash)
    # matches an existing git repo on disk. If so, use the base name.
    repos: set[str] = set()
    for name in raw_repos:
        normalized = _normalize_to_base_repo(name)
        repos.add(normalized)

    # Filter out common non-repo noise
    noise = {"$repo", "os", "src", "tmp"}
    repos -= noise

    return sorted(repos)


def _normalize_to_base_repo(name: str) -> str:
    """If name looks like a worktree (<repo>-<branch>), find the base repo.

    Checks progressively shorter dash-delimited prefixes to see if a git
    repo with that name exists on disk (under common dev directories).
    """
    if "-" not in name:
        return name

    home = os.path.expanduser("~")
    # Common places repos live — check if a shorter prefix is a real repo
    dev_dirs = [
        os.path.join(home, "dev"),
    ]
    # Also check sibling directories of known dev subdirs
    for sub in ("magicinternet", "fun"):
        d = os.path.join(home, "dev", sub)
        if os.path.isdir(d):
            dev_dirs.append(d)

    parts = name.split("-")
    # Try progressively shorter prefixes (longest first, stop before 0)
    for i in range(len(parts) - 1, 0, -1):
        candidate = "-".join(parts[:i])
        for dev_dir in dev_dirs:
            candidate_path = os.path.join(dev_dir, candidate)
            if os.path.isdir(os.path.join(candidate_path, ".git")):
                return candidate
    return name


# ---------------------------------------------------------------------------
# Tool call summaries
# ---------------------------------------------------------------------------

def _tool_summary(name: str, tool_input: dict) -> str:
    """Generate a one-line summary for a tool call."""
    if name == "Bash":
        cmd = tool_input.get("command", "")
        return f"$ {cmd[:80]}"
    elif name == "Read":
        fp = tool_input.get("file_path", "")
        return f"Read {os.path.basename(fp)}" if fp else "Read"
    elif name == "Write":
        fp = tool_input.get("file_path", "")
        return f"Write {os.path.basename(fp)}" if fp else "Write"
    elif name == "Edit":
        fp = tool_input.get("file_path", "")
        return f"Edit {os.path.basename(fp)}" if fp else "Edit"
    elif name == "Grep":
        pat = tool_input.get("pattern", "")
        return f'Grep "{pat[:40]}"'
    elif name == "Glob":
        pat = tool_input.get("pattern", "")
        return f'Glob "{pat[:40]}"'
    elif name in ("Agent", "Task"):
        stype = tool_input.get("subagent_type", "general")
        desc = tool_input.get("description", "")
        return f'Agent ({stype}): "{desc[:50]}"'
    elif name == "Skill":
        skill = tool_input.get("skill", "")
        return f"Skill: {skill}"
    elif name in ("TaskCreate", "TaskUpdate", "TaskList", "TaskOutput"):
        subj = tool_input.get("description", tool_input.get("subject", tool_input.get("id", "")))
        return f"Task: {str(subj)[:60]}"
    elif name.startswith("mcp__"):
        return name
    else:
        return name


# ---------------------------------------------------------------------------
# Tool result extraction
# ---------------------------------------------------------------------------

def _extract_tool_result_text(content) -> tuple[str, int, bool]:
    """Extract (text, size_bytes, is_error) from a tool_result content field."""
    is_error = False  # is_error lives on the block, not content — handled by caller
    if isinstance(content, str):
        size = len(content.encode("utf-8", errors="replace"))
        return content, size, False
    elif isinstance(content, list):
        texts = []
        size = 0
        for rb in content:
            if isinstance(rb, dict):
                t = rb.get("text", "")
                if t:
                    texts.append(t)
                    size += len(t.encode("utf-8", errors="replace"))
                data = rb.get("data", "")
                if data:
                    size += len(data)
            elif isinstance(rb, str):
                texts.append(rb)
                size += len(rb.encode("utf-8", errors="replace"))
        return "\n".join(texts), size, False
    else:
        dumped = json.dumps(content)
        return dumped, len(dumped.encode("utf-8")), False


# ---------------------------------------------------------------------------
# Tier 1: extract_metadata_lite
# ---------------------------------------------------------------------------

def extract_metadata_lite(path: str) -> dict:
    """Extract session metadata from head/tail only — no full parse."""
    head, tail, size = read_head_tail(path)

    session_id = extract_json_field(head, "sessionId")
    cwd = extract_json_field(head, "cwd")
    git_branch = extract_json_field(head, "gitBranch")
    version = extract_json_field(head, "version")
    start_time = extract_json_field(head, "timestamp")

    # Extract last timestamp from tail
    end_time = extract_json_field(tail, "timestamp")
    for line in reversed(tail.split("\n")):
        ts = extract_json_field(line, "timestamp")
        if ts:
            end_time = ts
            break

    # First user message for verification
    first_prompt = None
    for line in head.split("\n"):
        if '"role":"user"' not in line and '"role": "user"' not in line:
            continue
        if '"tool_result"' in line:
            continue
        # Try block-style text field first, then raw string content
        text = extract_json_field(line, "text")
        if not text:
            # For string-content user messages, extract from "content" field
            # but only if it looks like a plain string (not a JSON array)
            content_val = extract_json_field(line, "content")
            if content_val and not content_val.startswith("["):
                text = content_val
        if text and not text.startswith("<system-reminder>"):
            first_prompt = text[:200]
            break

    duration_seconds = None
    if start_time and end_time:
        start = parse_ts(start_time)
        end = parse_ts(end_time)
        if start and end:
            duration_seconds = round((end - start).total_seconds())

    return {
        "session_id": session_id,
        "cwd": cwd,
        "git_branch": git_branch,
        "version": version,
        "start_time": start_time,
        "end_time": end_time,
        "duration_seconds": duration_seconds,
        "file_size_bytes": size,
        "first_prompt": first_prompt,
    }


# ---------------------------------------------------------------------------
# Tier 2: extract_index_metadata
# ---------------------------------------------------------------------------

def extract_index_metadata(path: str) -> dict:
    """Stream the full JSONL, extract index-level metadata for session list
    and search index. Middle tier between lite and full detail."""

    session_id = None
    project_cwd = None
    git_branch = None
    branches_seen: set[str] = set()
    version = None
    start_time = None
    end_time = None
    first_prompt = None

    token_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    turn_count = 0

    tool_counts: Counter = Counter()
    file_paths: set[str] = set()
    bash_commands: list[str] = []
    search_text_parts: list[str] = []

    for rec in stream_jsonl(path):
        # Session metadata (first occurrence wins for most fields)
        if rec.get("sessionId") and session_id is None:
            session_id = rec["sessionId"]
        if rec.get("cwd") and project_cwd is None:
            project_cwd = rec["cwd"]
        if rec.get("gitBranch"):
            if git_branch is None:
                git_branch = rec["gitBranch"]
            branches_seen.add(rec["gitBranch"])
        if rec.get("version") and version is None:
            version = rec["version"]

        ts = rec.get("timestamp")
        if ts:
            if start_time is None:
                start_time = ts
            end_time = ts

        msg = rec.get("message", {})
        if not msg:
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        usage = msg.get("usage", {})

        # Token usage (assistant messages only)
        if usage and role == "assistant":
            token_usage["input_tokens"] += usage.get("input_tokens", 0)
            token_usage["output_tokens"] += usage.get("output_tokens", 0)
            token_usage["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens", 0)
            token_usage["cache_read_input_tokens"] += usage.get("cache_read_input_tokens", 0)
            turn_count += 1

        # Process user messages
        if role == "user":
            text = _extract_text_from_content(content)
            text = text.strip()

            # Skip tool_result-only messages and system reminders
            if text and not _is_system_noise(text):
                # First prompt
                if first_prompt is None and _is_user_text_message(content):
                    first_prompt = text[:200]
                # Search text: user messages (no tool_results, no system reminders)
                if _is_user_text_message(content):
                    search_text_parts.append(text)

        # Process assistant messages
        if role == "assistant":
            blocks = _get_content_blocks(content)
            for block in blocks:
                btype = block.get("type")

                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        search_text_parts.append(text)

                elif btype == "tool_use":
                    name = block.get("name", "unknown")
                    tool_input = block.get("input", {})
                    tool_counts[name] += 1

                    # Collect file paths for repos_touched
                    fp = tool_input.get("file_path", "")
                    if fp:
                        file_paths.add(fp)

                    # Collect bash commands for repos_touched
                    if name == "Bash":
                        cmd = tool_input.get("command", "")
                        if cmd:
                            bash_commands.append(cmd)

    # Compute duration
    duration_seconds = None
    if start_time and end_time:
        start_dt = parse_ts(start_time)
        end_dt = parse_ts(end_time)
        if start_dt and end_dt:
            duration_seconds = round((end_dt - start_dt).total_seconds())

    # Compute cost
    cost = _compute_cost(token_usage)

    # Derive repos_touched
    repos_touched = _collect_repos(file_paths, bash_commands)

    # Detect subagents
    session_dir = Path(path).with_suffix("")
    subagents_path = session_dir / "subagents"
    has_subagents = subagents_path.is_dir() and any(subagents_path.iterdir())

    file_size_bytes = os.path.getsize(path)

    return {
        "session_id": session_id,
        "project_cwd": project_cwd,
        "git_branch": git_branch,
        "branches_seen": sorted(branches_seen),
        "version": version,
        "start_time": start_time,
        "end_time": end_time,
        "duration_seconds": duration_seconds,
        "first_prompt": first_prompt,
        "turn_count": turn_count,
        "token_usage": token_usage,
        "estimated_cost_usd": round(cost, 4),
        "repos_touched": repos_touched,
        "tool_counts": dict(tool_counts.most_common()),
        "search_text": "\n".join(search_text_parts),
        "has_subagents": has_subagents,
        "file_size_bytes": file_size_bytes,
    }


# ---------------------------------------------------------------------------
# Tier 3: extract_session_detail
# ---------------------------------------------------------------------------

def extract_session_detail(path: str, subagents_dir: str | None = None) -> dict:
    """Full parse for the detail view. Returns structured conversation
    suitable for rendering as a chat UI, plus session metadata."""

    # We build conversation as a flat list, then pair tool_results at the end.
    session_id = None
    project_cwd = None
    git_branch = None
    branches_seen: set[str] = set()
    version = None
    start_time = None
    end_time = None
    first_prompt = None

    token_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    turn_count = 0
    tool_counts: Counter = Counter()
    file_paths: set[str] = set()
    bash_commands: list[str] = []
    search_text_parts: list[str] = []

    # Conversation building: collect assistant messages and pending tool_use blocks
    conversation: list[dict] = []
    # Map tool_use_id -> (conversation_idx, block_idx) for result pairing
    pending_tool_uses: dict[str, tuple[int, int]] = {}
    # Track if previous user message was a command-message (skill invocation)
    _prev_was_command = False

    for rec in stream_jsonl(path):
        # Session metadata
        if rec.get("sessionId") and session_id is None:
            session_id = rec["sessionId"]
        if rec.get("cwd") and project_cwd is None:
            project_cwd = rec["cwd"]
        if rec.get("gitBranch"):
            if git_branch is None:
                git_branch = rec["gitBranch"]
            branches_seen.add(rec["gitBranch"])
        if rec.get("version") and version is None:
            version = rec["version"]

        ts = rec.get("timestamp")
        if ts:
            if start_time is None:
                start_time = ts
            end_time = ts

        msg = rec.get("message", {})
        if not msg:
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        usage = msg.get("usage", {})

        # Token usage
        if usage and role == "assistant":
            token_usage["input_tokens"] += usage.get("input_tokens", 0)
            token_usage["output_tokens"] += usage.get("output_tokens", 0)
            token_usage["cache_creation_input_tokens"] += usage.get("cache_creation_input_tokens", 0)
            token_usage["cache_read_input_tokens"] += usage.get("cache_read_input_tokens", 0)
            turn_count += 1

        # --- User messages ---
        if role == "user":
            blocks = _get_content_blocks(content)

            # Pair tool_results with preceding assistant tool_use blocks
            for block in blocks:
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    is_error = block.get("is_error", False)
                    result_content = block.get("content", "")
                    text, size_bytes, _ = _extract_tool_result_text(result_content)

                    if tool_use_id and tool_use_id in pending_tool_uses:
                        conv_idx, blk_idx = pending_tool_uses[tool_use_id]
                        conversation[conv_idx]["blocks"][blk_idx]["result"] = {
                            "text": text,
                            "size_bytes": size_bytes,
                            "is_error": is_error,
                        }
                        del pending_tool_uses[tool_use_id]

            # Check if this is a real user text message (not just tool_results)
            text = ""
            if isinstance(content, str):
                text = content.strip()
            elif _is_user_text_message(content):
                text = _extract_text_from_content(content).strip()

            if text and not _is_system_noise(text):
                # Detect command-message (skill invocation trigger)
                if "<command-message>" in text:
                    _prev_was_command = True
                    if first_prompt is None:
                        first_prompt = text[:200]
                    search_text_parts.append(text)
                    conversation.append({
                        "type": "user",
                        "timestamp": ts,
                        "content": text,
                    })
                elif _prev_was_command:
                    # This is the skill prompt content — mark it as collapsible
                    _prev_was_command = False
                    search_text_parts.append(text)
                    conversation.append({
                        "type": "skill_prompt",
                        "timestamp": ts,
                        "content": text,
                    })
                else:
                    _prev_was_command = False
                    if first_prompt is None:
                        first_prompt = text[:200]
                    search_text_parts.append(text)
                    conversation.append({
                        "type": "user",
                        "timestamp": ts,
                        "content": text,
                    })

        # --- Assistant messages ---
        elif role == "assistant":
            blocks = _get_content_blocks(content)
            if not blocks:
                # String content (rare for assistant)
                if isinstance(content, str) and content.strip():
                    search_text_parts.append(content.strip())
                    conversation.append({
                        "type": "assistant",
                        "timestamp": ts,
                        "blocks": [{"type": "text", "text": content.strip()}],
                    })
                continue

            # Check if this is a continuation of the last assistant message
            # (Claude Code streams assistant messages as multiple JSONL records
            # with the same requestId / parent message ID)
            merged = False
            if conversation and conversation[-1]["type"] == "assistant":
                # Merge into the existing assistant message
                last = conversation[-1]
                for block in blocks:
                    btype = block.get("type")
                    if btype == "thinking":
                        last["blocks"].append({
                            "type": "thinking",
                            "text": block.get("thinking", ""),
                        })
                    elif btype == "text":
                        text = block.get("text", "")
                        if text.strip():
                            search_text_parts.append(text.strip())
                        last["blocks"].append({
                            "type": "text",
                            "text": text,
                        })
                    elif btype == "tool_use":
                        name = block.get("name", "unknown")
                        tool_input = block.get("input", {})
                        tool_use_id = block.get("id", "")
                        tool_counts[name] += 1

                        fp = tool_input.get("file_path", "")
                        if fp:
                            file_paths.add(fp)
                        if name == "Bash":
                            cmd = tool_input.get("command", "")
                            if cmd:
                                bash_commands.append(cmd)

                        tool_block = {
                            "type": "tool_use",
                            "name": name,
                            "summary": _tool_summary(name, tool_input),
                            "input": tool_input,
                            "result": None,
                            "tool_use_id": tool_use_id,
                            "subagent_id": None,
                            "_timestamp": ts,
                        }
                        blk_idx = len(last["blocks"])
                        last["blocks"].append(tool_block)

                        if tool_use_id:
                            pending_tool_uses[tool_use_id] = (len(conversation) - 1, blk_idx)
                merged = True

            if not merged:
                conv_entry = {
                    "type": "assistant",
                    "timestamp": ts,
                    "blocks": [],
                }
                for block in blocks:
                    btype = block.get("type")
                    if btype == "thinking":
                        conv_entry["blocks"].append({
                            "type": "thinking",
                            "text": block.get("thinking", ""),
                        })
                    elif btype == "text":
                        text = block.get("text", "")
                        if text.strip():
                            search_text_parts.append(text.strip())
                        conv_entry["blocks"].append({
                            "type": "text",
                            "text": text,
                        })
                    elif btype == "tool_use":
                        name = block.get("name", "unknown")
                        tool_input = block.get("input", {})
                        tool_use_id = block.get("id", "")
                        tool_counts[name] += 1

                        fp = tool_input.get("file_path", "")
                        if fp:
                            file_paths.add(fp)
                        if name == "Bash":
                            cmd = tool_input.get("command", "")
                            if cmd:
                                bash_commands.append(cmd)

                        tool_block = {
                            "type": "tool_use",
                            "name": name,
                            "summary": _tool_summary(name, tool_input),
                            "input": tool_input,
                            "result": None,
                            "tool_use_id": tool_use_id,
                            "subagent_id": None,
                            "_timestamp": ts,
                        }
                        blk_idx = len(conv_entry["blocks"])
                        conv_entry["blocks"].append(tool_block)

                        if tool_use_id:
                            pending_tool_uses[tool_use_id] = (len(conversation), blk_idx)

                conversation.append(conv_entry)

    # --- Post-processing ---

    # Compute duration
    duration_seconds = None
    if start_time and end_time:
        start_dt = parse_ts(start_time)
        end_dt = parse_ts(end_time)
        if start_dt and end_dt:
            duration_seconds = round((end_dt - start_dt).total_seconds())

    cost = _compute_cost(token_usage)
    repos_touched = _collect_repos(file_paths, bash_commands)

    # Auto-detect subagents directory
    if subagents_dir is None:
        session_dir = Path(path).with_suffix("")
        candidate = session_dir / "subagents"
        if candidate.is_dir():
            subagents_dir = str(candidate)

    has_subagents = bool(
        subagents_dir
        and os.path.isdir(subagents_dir)
        and any(
            f.endswith(".jsonl")
            for f in os.listdir(subagents_dir)
        )
    )

    file_size_bytes = os.path.getsize(path)

    session_meta = {
        "session_id": session_id,
        "project_cwd": project_cwd,
        "git_branch": git_branch,
        "branches_seen": sorted(branches_seen),
        "version": version,
        "start_time": start_time,
        "end_time": end_time,
        "duration_seconds": duration_seconds,
        "first_prompt": first_prompt,
        "turn_count": turn_count,
        "token_usage": token_usage,
        "estimated_cost_usd": round(cost, 4),
        "repos_touched": repos_touched,
        "tool_counts": dict(tool_counts.most_common()),
        "search_text": "\n".join(search_text_parts),
        "has_subagents": has_subagents,
        "file_size_bytes": file_size_bytes,
    }

    # --- Subagent matching ---
    subagents_list = []
    if subagents_dir and os.path.isdir(subagents_dir):
        subagents_list = _build_subagents_list(subagents_dir)
        _match_subagents_to_conversation(conversation, subagents_list, subagents_dir)

    # Strip internal _timestamp from tool_use blocks
    for entry in conversation:
        if entry["type"] == "assistant":
            for block in entry.get("blocks", []):
                block.pop("_timestamp", None)

    return {
        "session": session_meta,
        "conversation": conversation,
        "subagents": subagents_list,
    }


def _build_subagents_list(subagents_dir: str) -> list[dict]:
    """Build the subagents metadata list from a subagents directory."""
    subagent_files = sorted(glob_mod.glob(os.path.join(subagents_dir, "*.jsonl")))
    result = []
    for sa_file in subagent_files:
        basename = os.path.basename(sa_file)
        agent_id = basename.replace(".jsonl", "")

        meta = None
        meta_file = sa_file.replace(".jsonl", ".meta.json")
        if os.path.exists(meta_file):
            try:
                with open(meta_file) as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        result.append({
            "id": agent_id,
            "meta": meta,
            "has_transcript": True,
        })

    return result


def _match_subagents_to_conversation(
    conversation: list[dict],
    subagents_list: list[dict],
    subagents_dir: str,
) -> None:
    """Match subagent files to Agent tool_use blocks in the conversation
    using timestamp proximity (within 60s window)."""
    MAX_MATCH_WINDOW_S = 60

    # Collect subagent start times
    subagent_times: list[tuple[int, datetime | None]] = []
    for idx, sa in enumerate(subagents_list):
        sa_file = os.path.join(subagents_dir, sa["id"] + ".jsonl")
        sa_start = None
        if os.path.exists(sa_file):
            for rec in stream_jsonl(sa_file):
                if "timestamp" in rec:
                    sa_start = parse_ts(rec["timestamp"])
                    break
        subagent_times.append((idx, sa_start))

    # Collect Agent tool_use blocks with their timestamps and locations
    agent_blocks: list[tuple[int, int, datetime | None]] = []
    for conv_idx, entry in enumerate(conversation):
        if entry["type"] != "assistant":
            continue
        for blk_idx, block in enumerate(entry.get("blocks", [])):
            if block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                # Use per-block timestamp if available, fall back to entry timestamp
                block_ts = block.get("_timestamp") or entry.get("timestamp")
                dispatch_time = parse_ts(block_ts)
                agent_blocks.append((conv_idx, blk_idx, dispatch_time))

    # Match by timestamp proximity
    matched_subagents: set[int] = set()
    matched_dispatches: set[tuple[int, int]] = set()

    for sa_idx, sa_start in subagent_times:
        if sa_start is None:
            continue
        best_match = None
        best_delta = None

        for conv_idx, blk_idx, dispatch_time in agent_blocks:
            if (conv_idx, blk_idx) in matched_dispatches:
                continue
            if dispatch_time is None:
                continue
            delta = abs((sa_start - dispatch_time).total_seconds())
            if delta <= MAX_MATCH_WINDOW_S and (best_delta is None or delta < best_delta):
                best_match = (conv_idx, blk_idx)
                best_delta = delta

        if best_match is not None:
            conv_idx, blk_idx = best_match
            conversation[conv_idx]["blocks"][blk_idx]["subagent_id"] = subagents_list[sa_idx]["id"]
            matched_dispatches.add(best_match)
            matched_subagents.add(sa_idx)
