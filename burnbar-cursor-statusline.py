#!/usr/bin/env python3
"""
burnbar Cursor CLI statusLine bridge.

Cursor Agent invokes the configured `statusLine` command on every UI update and
feeds it a JSON blob on stdin (Claude Code–compatible shape). Unlike Claude,
Cursor's payload carries live *context window* fill — not plan/quota rate_limits.

This script:
  1. captures the latest reading to ~/.config/burnbar/cursor/live.json
  2. merges per-session context into ~/.config/burnbar/cursor/sessions.json
     (so multiple open agents don't overwrite each other — context-rot tracking),
  3. prints a compact status line back to the Cursor CLI.

It must never crash Cursor's status bar, so everything is best-effort.
"""
import json
import os
import sys
import time

LIVE_PATH = os.path.expanduser("~/.config/burnbar/cursor/live.json")
SESSIONS_PATH = os.path.expanduser("~/.config/burnbar/cursor/sessions.json")
# Drop sessions that haven't been refreshed by statusLine in this long.
SESSION_TTL_SEC = 2 * 3600


def bar(pct, cells=8):
    pct = max(0.0, min(100.0, float(pct or 0))) / 100.0
    full = int(round(pct * cells))
    return "█" * full + "░" * (cells - full)


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _merge_session(payload, now):
    """Upsert this session into the multi-session registry; prune stale ones."""
    try:
        with open(SESSIONS_PATH) as f:
            store = json.load(f)
        if not isinstance(store, dict):
            store = {"sessions": {}}
    except Exception:
        store = {"sessions": {}}
    sessions = store.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}

    sid = payload.get("session_id") or "_unknown"
    sessions[sid] = payload

    cut = now - SESSION_TTL_SEC
    sessions = {k: v for k, v in sessions.items()
                if (v.get("captured_at") or 0) >= cut}
    store = {"updated_at": now, "sessions": sessions}
    _atomic_write(SESSIONS_PATH, store)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    model = (data.get("model") or {}).get("display_name") \
        or (data.get("model") or {}).get("id") or ""
    ctx = data.get("context_window") or {}
    cwd = data.get("cwd") or (data.get("workspace") or {}).get("current_dir") or ""
    title = data.get("session_name") or ""
    sid = data.get("session_id") or ""
    now = int(time.time())

    payload = {
        "captured_at": now,
        "session_id": sid,
        "session_name": title or None,
        "cwd": cwd or None,
        "transcript_path": data.get("transcript_path"),
        "model": model,
        "context_window": {
            "used_percentage": ctx.get("used_percentage"),
            "remaining_percentage": ctx.get("remaining_percentage"),
            "context_window_size": ctx.get("context_window_size"),
            "total_input_tokens": ctx.get("total_input_tokens"),
            "total_output_tokens": ctx.get("total_output_tokens"),
            "current_usage": ctx.get("current_usage"),
        },
    }

    try:
        _atomic_write(LIVE_PATH, payload)
        _merge_session(payload, now)
    except Exception:
        pass

    parts = []
    if title:
        parts.append(title)
    elif cwd:
        parts.append(os.path.basename(cwd.rstrip("/")) or cwd)
    pct = ctx.get("used_percentage")
    if pct is not None:
        try:
            p = float(pct)
            parts.append(f"ctx {bar(p)} {round(p)}%")
        except (TypeError, ValueError):
            pass
    if model:
        parts.append(model)
    sys.stdout.write("  ".join(parts) if parts else (model or "burnbar"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stdout.write("burnbar")
