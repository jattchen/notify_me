"""Fail-open Codex hook adapter for subscription context recovery."""

import hashlib
import json
import os
import time

from .errors import NotifyMeError
from .runtime import load_endpoint, scope_key
from .storage import StateStore, resolve_storage_paths
from .transport import BarkTransport


def _hook_scope(store, payload, env):
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        raise NotifyMeError("scope_unavailable", "Hook 缺少有效任务作用域")
    candidate = env.get("CODEX_THREAD_ID")
    if candidate and candidate != session_id:
        raise NotifyMeError("scope_conflict", "Hook 与 CLI 任务作用域不一致")
    return scope_key(store, session_id, db_timeout=0.1)


def _context(store, hashed_scope):
    if not store.subscriptions_enabled(db_timeout=0.1):
        return ""
    rows = [
        row
        for row in store.list_subscriptions(
            hashed_scope, include_inactive=False, limit=20, pending_only=True, db_timeout=0.1
        )
    ]
    if not rows:
        return ""
    stats = store.subscription_summary_stats(hashed_scope, db_timeout=0.1)
    canonical = json.dumps(
        {"stats": stats, "visible": rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    visible = rows[:20]
    lines = ["Notify Me 当前任务用户订阅（context_revision={}）：".format(revision)]
    for row in visible:
        mode = "重复" if row["mode"] == "repeating" else "一次性"
        lines.append(
            "- {} r{} | {} | {} | {}".format(
                row["subscription_id"],
                row["revision"],
                row["priority"],
                mode,
                row["summary"],
            )
        )
    if stats["count"] > len(visible):
        lines.append("- 另有 {} 条；需要时使用 subscription list 读取。".format(stats["count"] - len(visible)))
    context = "\n".join(lines)
    if len(context.encode("utf-8")) > 4096:
        compact_lines = [lines[0]]
        for line in lines[1:]:
            candidate = "\n".join(compact_lines + [line])
            if len(candidate.encode("utf-8")) > 3900:
                compact_lines.append("- 订阅摘要过多，已安全截断；需要时使用 subscription list 读取。")
                break
            compact_lines.append(line)
        context = "\n".join(compact_lines)
    return context


def run_hook(kind, payload, env=None, transport=None, clock=None):
    """Return a hook JSON object, or None for a strict empty/fail-open result."""

    values = dict(os.environ if env is None else env)
    monotonic = clock or time.monotonic
    started = monotonic()
    try:
        if not isinstance(payload, dict):
            return None
        if kind not in ("user-prompt", "session-start"):
            return None
        if kind == "session-start" and payload.get("source") != "compact":
            return None
        store = StateStore(resolve_storage_paths(values))
        # Hook recovery has a hard 750 ms host budget.  Skip the full
        # integrity scan here and use bounded SQLite reads; the regular CLI
        # doctor/status paths still perform the complete check.
        store.require_initialized(db_timeout=0.1, integrity_check=False)
        hashed_scope = _hook_scope(store, payload, values)
        # A hook may perform one short, best-effort outbox claim. It never
        # blocks context recovery when the endpoint is missing or the queue is
        # paused, and it never waits for a second network attempt.
        if monotonic() - started < 0.25:
            try:
                endpoint = load_endpoint(store.paths)
                remaining = max(0.05, min(0.5, 0.75 - (monotonic() - started)))
                adapter = transport or BarkTransport(timeout=remaining)
                from .subscriptions import drain_outbox

                drain_outbox(
                    store,
                    endpoint,
                    adapter,
                    values,
                    max_attempts=1,
                    db_timeout=min(0.1, remaining),
                    sleep=lambda _seconds: None,
                )
            except (NotifyMeError, OSError, ValueError, TypeError):
                pass
        context = _context(store, hashed_scope)
        if monotonic() - started >= 0.75:
            return None
        if not context:
            return None
        event_name = "UserPromptSubmit" if kind == "user-prompt" else "SessionStart"
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": context,
            },
        }
    except (NotifyMeError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
