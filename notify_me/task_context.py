"""Read current Codex task presentation metadata behind one small interface."""

import json
import os
import stat
from pathlib import Path


_MAX_INDEX_TAIL_BYTES = 2 * 1024 * 1024
_MAX_GLOBAL_STATE_BYTES = 4 * 1024 * 1024


def _code_home(env):
    configured = env.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _regular_file(path, max_bytes=None):
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        return False
    return max_bytes is None or info.st_size <= max_bytes


def _thread_title(index_path, thread_id):
    if not _regular_file(index_path):
        return None
    try:
        with index_path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - _MAX_INDEX_TAIL_BYTES)
            handle.seek(start)
            if start:
                handle.readline()
            data = handle.read(_MAX_INDEX_TAIL_BYTES)
    except OSError:
        return None
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    title = None
    for line in lines:
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if entry.get("id") == thread_id and isinstance(entry.get("thread_name"), str):
            title = entry["thread_name"]
    return title


def _project_name(global_state_path, thread_id):
    if not _regular_file(global_state_path, _MAX_GLOBAL_STATE_BYTES):
        return None
    try:
        state = json.loads(global_state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    projectless = state.get("projectless-thread-ids", [])
    if isinstance(projectless, list) and thread_id in projectless:
        return None
    assignments = state.get("thread-project-assignments", {})
    assignment = assignments.get(thread_id) if isinstance(assignments, dict) else None
    if not isinstance(assignment, dict) or assignment.get("projectKind") != "local":
        return None
    raw_path = assignment.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    return Path(raw_path).name or None


def resolve_task_context(env=None):
    """Return best-effort display metadata for the current top-level Codex task."""

    values = os.environ if env is None else env
    thread_id = values.get("CODEX_THREAD_ID")
    if not isinstance(thread_id, str) or not thread_id:
        return {"task_title": None, "project_name": None}
    home = _code_home(values)
    return {
        "task_title": _thread_title(home / "session_index.jsonl", thread_id),
        "project_name": _project_name(home / ".codex-global-state.json", thread_id),
    }
