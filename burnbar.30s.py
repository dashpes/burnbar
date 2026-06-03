#!/usr/bin/env python3
# <bitbar.title>burnbar</bitbar.title>
# <bitbar.version>0.1.0</bitbar.version>
# <bitbar.author>burnbar</bitbar.author>
# <bitbar.desc>Claude Code 5-hour-block token burn, as a filling bar in the menu bar.</bitbar.desc>
# <bitbar.dependencies>python3</bitbar.dependencies>
# <swiftbar.hideAbout>false</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>false</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>false</swiftbar.hideDisablePlugin>
"""
burnbar — a SwiftBar/xbar plugin that renders your *current* Claude Code
5-hour usage block as a literal progress bar that fills up, right in the
macOS menu bar (visual + text), e.g.:

    ███████▍░░ 74% · 1.8M

It reads Claude Code's own local transcripts (~/.claude/projects/**/*.jsonl),
groups assistant turns into rolling 5-hour blocks (the way usage limits reset),
and shows how hard you're burning the *active* block.

"100%" is auto-calibrated to your busiest-ever block (a self-tuning high-water
mark persisted to ~/.config/burnbar/state.json) — no plan limits or pricing
needed, and it gets smarter the more you use it.
"""

import glob
import json
import os
from datetime import datetime, timedelta, timezone

# ─────────────────────────── config ───────────────────────────
BLOCK_HOURS = 5                     # length of a usage block
BAR_CELLS = 10                      # width of the menu-bar bar
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
STATE_PATH = os.path.expanduser("~/.config/burnbar/state.json")

# Cache reads are ~10x cheaper / lighter than fresh tokens, so we down-weight
# them when measuring "burn". Set to 1.0 to count every token equally.
CACHE_READ_WEIGHT = 0.1

# Floor for the auto-calibrated peak, so a quiet first-ever block doesn't
# instantly read as 100%. Once you have real history this is irrelevant.
PEAK_FLOOR = 300_000

MONO = "Menlo"                      # monospace font so the bar aligns
FONT = f"font={MONO} size=13"

# ─────────────────────────── helpers ───────────────────────────
def parse_ts(s):
    # "2026-06-03T19:11:27.979Z" -> aware UTC datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def compact(n):
    n = float(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return f"{int(n)}"


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"peak": 0}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def weighted(tok):
    """Effective burn from a {input,output,cache_creation,cache_read} dict."""
    return (
        tok["input"]
        + tok["output"]
        + tok["cache_creation"]
        + tok["cache_read"] * CACHE_READ_WEIGHT
    )


def new_tokens():
    return {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}


def add_tokens(dst, u):
    dst["input"] += u.get("input_tokens", 0) or 0
    dst["output"] += u.get("output_tokens", 0) or 0
    dst["cache_creation"] += u.get("cache_creation_input_tokens", 0) or 0
    dst["cache_read"] += u.get("cache_read_input_tokens", 0) or 0


# ─────────────────────────── data load ───────────────────────────
def load_records():
    """Return deduped list of (ts, model, usage_dict) for assistant turns."""
    seen = set()
    out = []
    for fp in glob.glob(PROJECTS_GLOB, recursive=True):
        try:
            f = open(fp)
        except Exception:
            continue
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "assistant":
                    continue
                msg = o.get("message") or {}
                u = msg.get("usage")
                ts = o.get("timestamp")
                if not u or not ts:
                    continue
                key = (msg.get("id"), o.get("requestId"))
                if key != (None, None) and key in seen:
                    continue
                seen.add(key)
                try:
                    out.append((parse_ts(ts), msg.get("model", "?"), u))
                except Exception:
                    continue
    out.sort(key=lambda r: r[0])
    return out


def build_blocks(records):
    """Group records into rolling BLOCK_HOURS blocks (ccusage-style)."""
    window = timedelta(hours=BLOCK_HOURS)
    blocks = []
    for ts, model, u in records:
        if blocks and (
            ts - blocks[-1]["start"] < window
            and ts - blocks[-1]["last"] < window
        ):
            b = blocks[-1]
        else:
            b = {
                "start": floor_hour(ts),
                "last": ts,
                "tokens": new_tokens(),
                "by_model": {},
                "msgs": 0,
            }
            blocks.append(b)
        b["last"] = ts
        b["msgs"] += 1
        add_tokens(b["tokens"], u)
        bm = b["by_model"].setdefault(model, new_tokens())
        add_tokens(bm, u)
    return blocks


# ─────────────────────────── rendering ───────────────────────────
def render_bar(frac, cells=BAR_CELLS):
    frac = max(0.0, min(1.0, frac))
    filled = frac * cells
    full = int(filled)
    bar = "█" * full
    if full < cells:
        eighths = " ▏▎▍▌▋▊▉█"
        idx = round((filled - full) * 8)
        if idx > 0:
            bar += eighths[idx]
            full += 1
        bar += "░" * (cells - full)
    return bar[:cells] if len(bar) >= cells else bar + "░" * (cells - len(bar))


def color_for(pct):
    if pct >= 90:
        return "#ff453a"   # red
    if pct >= 70:
        return "#ff9f0a"   # orange
    if pct >= 40:
        return "#ffd60a"   # yellow
    return "#30d158"       # green


def fmt_dur(delta):
    secs = max(0, int(delta.total_seconds()))
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h{m:02d}m"


def main():
    now = datetime.now(timezone.utc)
    records = load_records()
    state = load_state()

    if not records:
        print("⚡ burnbar")
        print("---")
        print("No Claude Code usage found yet | " + FONT)
        print(f"Looked in: {PROJECTS_GLOB} | {FONT}")
        print("Refresh | refresh=true")
        return

    blocks = build_blocks(records)
    window = timedelta(hours=BLOCK_HOURS)

    # Active block = last block whose window still covers 'now'.
    active = None
    last = blocks[-1]
    if now - last["start"] < window:
        active = last

    # Calibrate peak from *completed* blocks + persisted high-water mark.
    completed = [b for b in blocks if b is not active]
    peak = max(
        [weighted(b["tokens"]) for b in completed] + [state.get("peak", 0), PEAK_FLOOR]
    )
    # Fold completed blocks into the persisted peak so it survives pruning.
    new_peak = max(state.get("peak", 0), *[weighted(b["tokens"]) for b in completed]) \
        if completed else state.get("peak", 0)
    if new_peak != state.get("peak", 0):
        state["peak"] = new_peak
        save_state(state)

    if active is None:
        # Idle: no active block. Show a flat bar.
        print(f"{render_bar(0)} idle | {FONT} color=#8e8e93")
        print("---")
        print("No active 5-hour block | " + FONT)
        since = now - last["last"]
        print(f"Last activity {fmt_dur(since)} ago | {FONT}")
        emit_history(blocks, now, peak)
        return

    burn = weighted(active["tokens"])
    frac = burn / peak if peak else 0
    pct = round(frac * 100)
    bar = render_bar(frac)
    title = f"{bar} {pct}% · {compact(burn)}"
    print(f"{title} | {FONT} color={color_for(pct)}")
    print("---")

    end = active["start"] + window
    remaining = end - now
    tz = datetime.now().astimezone().tzinfo
    s_local = active["start"].astimezone(tz).strftime("%H:%M")
    e_local = end.astimezone(tz).strftime("%H:%M")

    print(f"Claude Code — current 5-hour block | {FONT}")
    print(f"Burn: {compact(burn)} tok · {pct}% of peak | {FONT}")
    print(f"Window: {s_local}–{e_local} · resets in {fmt_dur(remaining)} | {FONT}")
    print(f"Peak baseline: {compact(peak)} tok | {FONT}")
    print("---")

    t = active["tokens"]
    print(f"Breakdown (this block) | {FONT}")
    print(f"--Input        {compact(t['input']):>7} | {FONT}")
    print(f"--Output       {compact(t['output']):>7} | {FONT}")
    print(f"--Cache write  {compact(t['cache_creation']):>7} | {FONT}")
    print(f"--Cache read   {compact(t['cache_read']):>7} | {FONT}")
    print(f"By model (this block) | {FONT}")
    for model, mt in sorted(active["by_model"].items(),
                            key=lambda kv: -weighted(kv[1])):
        short = model.replace("claude-", "")
        print(f"--{short:<16}{compact(weighted(mt)):>7} | {FONT}")

    emit_history(blocks, now, peak)


def emit_history(blocks, now, peak):
    print("---")
    tz = datetime.now().astimezone().tzinfo

    # Today (local) totals.
    today = datetime.now().astimezone(tz).date()
    today_tok, today_msgs = new_tokens(), 0
    by_day = {}
    for b in blocks:
        d = b["start"].astimezone(tz).date()
        agg = by_day.setdefault(d, [0, 0])
        agg[0] += weighted(b["tokens"])
        agg[1] += b["msgs"]
        if d == today:
            for k in today_tok:
                today_tok[k] += b["tokens"][k]
            today_msgs += b["msgs"]
    print(f"Today: {compact(weighted(today_tok))} tok · {today_msgs} msgs | {FONT}")

    print(f"Last 7 days | {FONT}")
    days = sorted(by_day.items(), reverse=True)[:7]
    daymax = max((v[0] for _, v in days), default=1) or 1
    for d, (tok, msgs) in days:
        mini = render_bar(tok / daymax, cells=6)
        print(f"--{d.strftime('%m-%d')}  {mini}  {compact(tok):>6} | {FONT}")

    print(f"Recent blocks | {FONT}")
    for b in list(reversed(blocks))[:8]:
        s = b["start"].astimezone(tz).strftime("%m-%d %H:%M")
        burn = weighted(b["tokens"])
        live = " ● live" if (now - b["start"] < timedelta(hours=BLOCK_HOURS)
                             and b is blocks[-1]) else ""
        print(f"--{s}  {compact(burn):>6}{live} | {FONT}")

    print("---")
    print("Refresh | refresh=true")
    print("Open transcripts | bash=/usr/bin/open "
          'param1="' + os.path.expanduser("~/.claude/projects") + '" terminal=false')


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never leave the menu bar blank
        print("⚡ burnbar !")
        print("---")
        print(f"Error: {e} | {FONT} color=#ff453a")
        print("Refresh | refresh=true")
