#!/usr/bin/env python3
"""
burnbar statusLine bridge.

Claude Code invokes the configured `statusLine` command on every UI update and
feeds it a JSON blob on stdin. For Claude.ai subscribers that blob carries a
`rate_limits` object — the REAL, server-side usage limits (5-hour + 7-day +
opus), already aggregated across claude.ai web, Claude Code, and every machine,
with exact `resets_at` timestamps.

This script:
  1. captures that `rate_limits` object to ~/.config/burnbar/usage.json
     (so the burnbar SwiftBar plugin can show real numbers), and
  2. prints a compact status line back to Claude Code.

It must never crash Claude Code's status bar, so everything is best-effort.
"""
import json
import os
import sys
import time

USAGE_PATH = os.path.expanduser("~/.config/burnbar/usage.json")


def fmt_dur(secs):
    secs = max(0, int(secs))
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def bar(pct, cells=8):
    pct = max(0.0, min(100.0, pct)) / 100.0
    full = int(round(pct * cells))
    return "█" * full + "░" * (cells - full)


def session_title(transcript_path, tail_bytes=262144, cap=48):
    """The session's own title — Claude writes an `ai-title` event (`aiTitle`) to
    the transcript and revises it as the session goes; we want the latest. Only the
    file's tail is read so this stays cheap no matter how long the transcript is."""
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()            # drop the partial first line
            chunk = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    title = None
    for line in chunk.splitlines():
        if '"aiTitle"' not in line:     # cheap pre-filter before parsing
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") == "ai-title" and o.get("aiTitle"):
            title = o["aiTitle"]
    if title and len(title) > cap:
        title = title[:cap - 1] + "…"
    return title


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    rl = data.get("rate_limits") or {}
    model = (data.get("model") or {}).get("display_name") \
        or (data.get("model") or {}).get("id") or ""

    # Persist whatever we got (atomically) for the plugin to read.
    if rl:
        try:
            os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
            payload = {
                "captured_at": int(time.time()),
                "rate_limits": rl,
                "model": model,
                "cost_usd": (data.get("cost") or {}).get("total_cost_usd"),
            }
            tmp = USAGE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, USAGE_PATH)
        except Exception:
            pass

    # Build the status line shown in Claude Code. Lead with the session's own
    # title so you can tell which session a given terminal/tab is at a glance.
    now = time.time()
    parts = []
    title = session_title(data.get("transcript_path"))
    if title:
        parts.append(title)
    five = rl.get("five_hour") or {}
    seven = rl.get("seven_day") or {}
    if five:
        p = five.get("used_percentage")
        reset = five.get("resets_at")
        seg = f"5h {bar(p or 0)} {round(p or 0)}%"
        if reset:
            seg += f"·{fmt_dur(reset - now)}"
        parts.append(seg)
    if seven:
        p = seven.get("used_percentage")
        parts.append(f"7d {round(p or 0)}%")
    if model:
        parts.append(model)
    line = "  ".join(parts) if parts else (model or "burnbar")
    sys.stdout.write(line)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stdout.write("burnbar")
