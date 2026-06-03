#!/usr/bin/env python3
# <bitbar.title>burnbar</bitbar.title>
# <bitbar.version>0.5.0</bitbar.version>
# <bitbar.author>burnbar</bitbar.author>
# <bitbar.desc>Claude Code usage: 5-hour-block burn bar + stats dropdown, themeable.</bitbar.desc>
# <bitbar.dependencies>python3</bitbar.dependencies>
# <swiftbar.hideAbout>false</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>false</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>false</swiftbar.hideDisablePlugin>
"""
burnbar — a SwiftBar/xbar plugin.

Menu bar:  a live progress bar for your current Claude Code 5-hour usage block.
Dropdown:  a Stats-style panel (compact by default, or detailed), with Settings
           submenu for theme / view / bar width — no JSON editing required.

All from Claude Code's own local transcripts (~/.claude/projects/**/*.jsonl).
No ccusage, no API keys, no network, no pricing.

Settings are stored in ~/.config/burnbar/config.json and changed by clicking
items in the Settings submenu (which re-invoke this script with --set).
"""

import glob
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

# ─────────────────────────── fixed config ───────────────────────────
BLOCK_HOURS = 5
BAR_CELLS = 10                   # bar width inside the dropdown
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
CONFIG_PATH = os.path.expanduser("~/.config/burnbar/config.json")
USAGE_PATH = os.path.expanduser("~/.config/burnbar/usage.json")  # live rate_limits
CACHE_PATH = os.path.expanduser("~/.config/burnbar/cache.json")  # per-file rollups
CACHE_VERSION = 4                # bumped: per-file aggregates now carry context info
RECENT_DAYS = 3                  # keep it lean: only files newer than this are
#                                  re-parsed each refresh (for recent blocks); older
#                                  ones are read once and served from cache. Smaller
#                                  = less CPU/RAM per refresh, shorter recent history.
CACHE_READ_WEIGHT = 0.1          # cache reads are ~10x lighter; down-weight burn

# ── context window (how much of the model's window each agent has filled) ──
CTX_200K = 200_000               # standard Claude Code window
CTX_1M = 1_000_000               # the 1M-context Opus
CONTEXT_ACTIVE_MIN = 120         # a main session counts as "active" if touched this recently
CONTEXT_AGENT_MIN = 15           # subagents are ephemeral: only show ones still running
CONTEXT_LIVE_MIN = 5             # freshest sessions get a "live" tag instead of an age
CONTEXT_MAX_ROWS = 6             # cap on main sessions shown
CONTEXT_MAX_AGENTS = 5           # cap on subagents shown per parent
MONO = "Menlo"
MUTED = "#8e8e93"                # section headers / secondary notes
SELF = os.path.realpath(__file__)

# ── user-configurable defaults (overridden by config.json) ──
DEFAULTS = {
    "view": "compact",           # "compact" | "detailed"
    "theme": "default",          # see THEMES
    "menubar_cells": 5,          # bar width in the menu bar
    "title_size": 11,            # menu-bar font size
    "menubar_extra": "countdown",  # trailer after the %: countdown | tokens | none
    "context_window": "auto",    # how to size the context bar: auto | 200k | 1m
}
MENUBAR_EXTRAS = ("countdown", "tokens", "none")
CONTEXT_WINDOWS = ("auto", "200k", "1m")

# ── themes: a full palette, so the whole dropdown gets tinted ──
#   grad  = (low, mid, high, max) bar gradient + alert accents (by % burn)
#   text  = body rows, adaptive "light,dark" so it stays readable in both menus
#   muted = section headers + secondary notes
THEMES = {
    "default":   {"grad": ("#30d158", "#ffd60a", "#ff9f0a", "#ff453a"),
                  "text": "#1d1d1f,#f5f5f7", "muted": "#56565b,#9a9aa0"},
    "mono":      {"grad": ("#8e8e93", "#aeaeb2", "#d1d1d6", "#f5f5f7"),
                  "text": "#1d1d1f,#f5f5f7", "muted": "#56565b,#9a9aa0"},
    "nord":      {"grad": ("#a3be8c", "#ebcb8b", "#d08770", "#bf616a"),
                  "text": "#2e3440,#d8dee9", "muted": "#5e81ac,#81a1c1"},
    "dracula":   {"grad": ("#50fa7b", "#f1fa8c", "#ffb86c", "#ff5555"),
                  "text": "#44475a,#f8f8f2", "muted": "#6272a4,#9aa6d4"},
    "solarized": {"grad": ("#859900", "#b58900", "#cb4b16", "#dc322f"),
                  "text": "#073642,#93a1a1", "muted": "#268bd2,#6c9fc2"},
    "matrix":    {"grad": ("#39ff14", "#32e60f", "#28b80c", "#1f8f08"),
                  "text": "#0a5f0a,#39ff14", "muted": "#1f8f08,#2fd80c"},
}

# Module-level theme/derived colors; set in main() once config is loaded.
TH = THEMES["default"]
MUTED = TH["muted"]


# ─────────────────────────── config i/o ───────────────────────────
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if cfg.get("theme") not in THEMES:
        cfg["theme"] = "default"
    if cfg.get("view") in ("default", "expanded"):   # legacy names for the full view
        cfg["view"] = "detailed"
    if cfg.get("view") not in ("compact", "detailed"):
        cfg["view"] = "compact"
    if cfg.get("menubar_extra") not in MENUBAR_EXTRAS:
        cfg["menubar_extra"] = "countdown"
    if cfg.get("context_window") not in CONTEXT_WINDOWS:
        cfg["context_window"] = "auto"
    return cfg


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def coerce(key, value):
    if key in ("menubar_cells", "title_size"):
        try:
            return max(1, int(value))
        except ValueError:
            return DEFAULTS[key]
    return value


def handle_cli(argv):
    """`--set key=value [key=value ...]` writes config; SwiftBar refreshes after."""
    if argv and argv[0] == "--set":
        cfg = load_config()
        for kv in argv[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                if k in DEFAULTS:
                    cfg[k] = coerce(k, v)
        save_config(cfg)


# ─────────────────────────── helpers ───────────────────────────
def parse_ts(s):
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


def fmt_dur(delta):
    secs = max(0, int(delta.total_seconds()))
    h, m = secs // 3600, (secs % 3600) // 60
    if h >= 24:
        return f"{h//24}d{h%24}h"
    return f"{h}h{m:02d}m"


def fmt_age(secs):
    """Compact 'time since' for the context section: 4m, 1h, 2h30m."""
    m = max(0, int(secs // 60))
    if m < 60:
        return f"{m}m"
    h, rm = m // 60, m % 60
    return f"{h}h{rm:02d}m" if rm else f"{h}h"


def context_window(model, peak_ctx, mode):
    """Pick a session's context-window size. 'auto' goes by the model running the
    latest turn — Opus is the 1M-context model, everything else is the standard
    200K — and falls back to the high-water mark for any non-Opus session that has
    somehow crossed 200K (e.g. a 1M-beta Sonnet)."""
    if mode == "200k":
        return CTX_200K
    if mode == "1m":
        return CTX_1M
    if "opus" in (model or "").lower():
        return CTX_1M
    return CTX_1M if peak_ctx > CTX_200K else CTX_200K


def ctx_label(win):
    return "1M" if win >= CTX_1M else f"{win // 1000}K"


def ellipsis(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def model_short(m):
    """'claude-opus-4-8' -> 'opus', 'claude-haiku-4-5-20251001' -> 'haiku'."""
    m = (m or "").replace("claude-", "")
    return m.split("-")[0] if m else "?"


def new_tokens():
    return {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}


def add_tokens(dst, u):
    """Add a raw usage object (input_tokens/...) into a token dict."""
    dst["input"] += u.get("input_tokens", 0) or 0
    dst["output"] += u.get("output_tokens", 0) or 0
    dst["cache_creation"] += u.get("cache_creation_input_tokens", 0) or 0
    dst["cache_read"] += u.get("cache_read_input_tokens", 0) or 0


def merge(dst, src):
    """Add one token dict (input/output/...) into another."""
    for k in dst:
        dst[k] += src.get(k, 0) or 0


def weighted(t):
    return (t["input"] + t["output"] + t["cache_creation"]
            + t["cache_read"] * CACHE_READ_WEIGHT)


def weighted_one(u):
    return ((u.get("input_tokens", 0) or 0)
            + (u.get("output_tokens", 0) or 0)
            + (u.get("cache_creation_input_tokens", 0) or 0)
            + (u.get("cache_read_input_tokens", 0) or 0) * CACHE_READ_WEIGHT)


def raw_total(t):
    return t["input"] + t["output"] + t["cache_creation"] + t["cache_read"]


def ctx_one(u):
    """A turn's context-window occupancy: the full prompt it was sent (all input
    + cache, no output). The latest turn's value is the session's current fill."""
    return ((u.get("input_tokens", 0) or 0)
            + (u.get("cache_creation_input_tokens", 0) or 0)
            + (u.get("cache_read_input_tokens", 0) or 0))


def load_cache():
    try:
        with open(CACHE_PATH) as f:
            c = json.load(f)
        if c.get("version") == CACHE_VERSION:
            return c
    except Exception:
        pass
    return {"version": CACHE_VERSION, "files": {}, "peak": None}


def save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def load_usage():
    """Live rate_limits captured by the statusLine bridge, or None."""
    try:
        with open(USAGE_PATH) as f:
            u = json.load(f)
        if (u.get("rate_limits") or {}).get("five_hour"):
            return u
    except Exception:
        pass
    return None


PLAN_LABEL = {"pro": "Pro", "max5": "Max 5×", "max20": "Max 20×"}
PLAN = None  # set in main() from ~/.claude.json


def read_plan():
    try:
        d = json.load(open(os.path.expanduser("~/.claude.json")))
        t = ((d.get("oauthAccount") or {}).get("organizationRateLimitTier") or "").lower()
        if "20x" in t:
            return "max20"
        if "5x" in t:
            return "max5"
        if "pro" in t:
            return "pro"
    except Exception:
        pass
    return None


def pretty_project(dirname):
    p = dirname.replace("-", "/")
    home = os.path.expanduser("~")
    if p.startswith(home):
        rest = p[len(home):].strip("/")
        return rest.split("/")[-1] if rest else "home"
    base = p.rstrip("/").split("/")[-1]
    return base or p


# ─────────────────────── SwiftBar emit helpers ───────────────────────
def emit(text, sub=0, color=None, sfimage=None, size=13, refresh=False,
         action=None, args=None, open_path=None, header=False):
    prefix = "--" * sub
    params = [f"font={MONO} size={12 if header else size}"]
    params.append(f"color={color if color is not None else TH['text']}")
    if sfimage:
        params.append(f"sfimage={sfimage}")
    if refresh:
        params.append("refresh=true")
    if action:
        params.append(f"bash={action}")
        for i, a in enumerate(args or [], 1):
            params.append(f'param{i}="{a}"')
        params.append("terminal=false")
    if open_path:
        params.append("bash=/usr/bin/open")
        params.append(f'param1="{open_path}"')
        params.append("terminal=false")
    print(f"{prefix}{text} | {' '.join(params)}")


def sep(sub=0):
    print("--" * sub + "---")


# ─────────────────────────── data load ───────────────────────────
def parse_file(fp, project, session, tz):
    """Parse one transcript -> (json-serializable aggregate, [records])."""
    all_t, msgs = new_tokens(), 0
    by_model, by_day, by_hour = {}, {}, [0.0] * 24
    sess_last = None
    last_ctx, peak_ctx = 0, 0       # current + high-water context-window fill
    last_model = "?"                # model of the latest turn (sizes the window)
    title = None                    # Claude's auto-generated session title (aiTitle)
    meta = {}                       # cwd / parent session id / agent id (stable per file)
    records = []
    seen = set()
    try:
        f = open(fp)
    except Exception:
        return None, []
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not meta and o.get("sessionId"):
                # First line carries the session header: cwd (true project), the
                # parent sessionId (== own id for a main session, parent's for a
                # subagent), agentId, the sidechain flag that marks subagents, and
                # the git branch (a cheap differentiator between sibling sessions).
                meta = {"cwd": o.get("cwd"), "sid": o.get("sessionId"),
                        "agent_id": o.get("agentId"),
                        "sidechain": bool(o.get("isSidechain")),
                        "branch": o.get("gitBranch")}
            if o.get("type") == "ai-title":
                # Claude names each session (the title shown in the resume picker);
                # it's revised over the session, so keep the most recent one.
                title = o.get("aiTitle") or title
                continue
            if o.get("type") != "assistant":
                continue
            msg = o.get("message") or {}
            u, ts = msg.get("usage"), o.get("timestamp")
            if not u or not ts:
                continue
            key = (msg.get("id"), o.get("requestId"))
            if key != (None, None):
                if key in seen:
                    continue
                seen.add(key)
            try:
                tsd = parse_ts(ts)
            except Exception:
                continue
            model = msg.get("model", "?")
            add_tokens(all_t, u); msgs += 1
            add_tokens(by_model.setdefault(model, new_tokens()), u)
            c = ctx_one(u)
            if c > peak_ctx:
                peak_ctx = c
            if sess_last is None or ts > sess_last:
                sess_last = ts
                last_ctx = c        # context fill as of the most recent turn
                last_model = model
            lts = tsd.astimezone(tz)
            d, hr = lts.date().isoformat(), lts.hour
            wt = weighted_one(u)
            dd = by_day.setdefault(d, [0.0, 0]); dd[0] += wt; dd[1] += 1
            by_hour[hr] += wt
            records.append({"ts": tsd, "model": model, "u": u,
                            "project": project, "session": session})
    agg = {"all": all_t, "msgs": msgs, "by_model": by_model, "by_day": by_day,
           "by_hour": by_hour, "project": project, "session": session,
           "sess_last": sess_last, "last_ctx": last_ctx, "peak_ctx": peak_ctx,
           "last_model": last_model, "title": title, "meta": meta}
    return agg, records


def gather(now, tz):
    """Aggregate all usage, parsing only new/changed/recent files (cache the rest).

    Returns merged history aggregates + recent records (for blocks) + all-time
    peak block. Per-file rollups are cached so each refresh stays cheap no matter
    how large the transcript history grows."""
    cutoff = now - timedelta(days=RECENT_DAYS)
    cache = load_cache()
    old_files = cache.get("files", {})
    new_files = {}

    all_t, all_msgs = new_tokens(), 0
    by_model_all, by_project, by_session, by_day = {}, {}, {}, {}
    hour_profile = [0.0] * 24
    recent_records, parsed_records = [], []
    dirty = False

    for fp in glob.glob(PROJECTS_GLOB, recursive=True):
        project = pretty_project(os.path.basename(os.path.dirname(fp)))
        session = os.path.splitext(os.path.basename(fp))[0]
        try:
            st = os.stat(fp)
        except OSError:
            continue
        mtime, size = st.st_mtime, st.st_size
        recent = mtime >= cutoff.timestamp()
        cached = old_files.get(fp)
        changed = (not cached or cached.get("mtime") != mtime
                   or cached.get("size") != size)
        if recent or changed:
            agg, records = parse_file(fp, project, session, tz)
            if agg is None:
                continue
            parsed_records.extend(records)
            if recent:
                recent_records.extend(records)
            new_files[fp] = {"mtime": mtime, "size": size, "agg": agg}
            dirty = dirty or changed
        else:
            agg = cached["agg"]
            new_files[fp] = cached

        merge(all_t, agg["all"]); all_msgs += agg["msgs"]
        for m, mt in agg["by_model"].items():
            merge(by_model_all.setdefault(m, new_tokens()), mt)
        for d, (w, c) in agg["by_day"].items():
            dd = by_day.setdefault(d, [0.0, 0]); dd[0] += w; dd[1] += c
        for i, v in enumerate(agg["by_hour"]):
            hour_profile[i] += v
        proj = by_project.setdefault(agg["project"],
                                     {"t": new_tokens(), "m": 0, "s": set()})
        merge(proj["t"], agg["all"]); proj["m"] += agg["msgs"]
        proj["s"].add(agg["session"])
        meta = agg.get("meta") or {}
        by_session[agg["session"]] = {
            "t": agg["all"], "m": agg["msgs"], "p": agg["project"],
            "last": agg["sess_last"], "mtime": mtime,
            "last_ctx": agg.get("last_ctx", 0), "peak_ctx": agg.get("peak_ctx", 0),
            "model": agg.get("last_model", "?"),
            "title": agg.get("title"), "branch": meta.get("branch"),
            "sid": meta.get("sid") or agg["session"], "cwd": meta.get("cwd"),
            "agent": bool(meta.get("sidechain")) or agg["session"].startswith("agent-"),
            "agent_id": meta.get("agent_id")}

    # All-time peak block: high-water in cache, refreshed from whatever we parsed.
    peak = cache.get("peak")
    if parsed_records:
        for b in build_blocks(sorted(parsed_records, key=lambda r: r["ts"])):
            w = weighted(b["tokens"])
            if not peak or w > peak["w"]:
                peak = {"w": w, "start": b["start"].isoformat(), "msgs": b["msgs"]}

    # Only rewrite the cache when something actually changed (new/changed/pruned
    # file, or a new peak) — avoids a disk write on every idle refresh.
    if dirty or peak != cache.get("peak") or set(new_files) != set(old_files):
        save_cache({"version": CACHE_VERSION, "files": new_files, "peak": peak})
    return {
        "all_tok": all_t, "all_msgs": all_msgs, "by_model": by_model_all,
        "by_project": by_project, "by_session": by_session, "by_day": by_day,
        "hour_profile": hour_profile, "recent_records": recent_records,
        "peak": peak,
    }


def build_blocks(records):
    window = timedelta(hours=BLOCK_HOURS)
    blocks = []
    for r in records:
        ts = r["ts"]
        if blocks and (ts - blocks[-1]["start"] < window
                       and ts - blocks[-1]["last"] < window):
            b = blocks[-1]
        else:
            b = {"start": floor_hour(ts), "last": ts,
                 "tokens": new_tokens(), "by_model": {}, "msgs": 0}
            blocks.append(b)
        b["last"] = ts
        b["msgs"] += 1
        add_tokens(b["tokens"], r["u"])
        add_tokens(b["by_model"].setdefault(r["model"], new_tokens()), r["u"])
    return blocks


# ─────────────────────────── bars / charts ───────────────────────────
def render_bar(frac, cells):
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
    return bar if len(bar) >= cells else bar + "░" * (cells - len(bar))


def spark(values):
    ticks = "▁▂▃▄▅▆▇█"
    mx = max(values) if values else 0
    if mx <= 0:
        return "·" * len(values)
    return "".join("·" if v <= 0 else ticks[min(7, int(v / mx * 7 + 0.999))]
                   for v in values)


def emit_live_limits(usage, now_epoch, tz):
    """The real, cross-surface limits from Anthropic (via the statusLine bridge)."""
    rl = usage["rate_limits"]
    plan = f" · {PLAN_LABEL[PLAN]}" if PLAN else ""
    emit(f"USAGE LIMITS · live{plan}", color=MUTED, sfimage="bolt.fill", header=True)

    def line(label, d):
        if not d or d.get("used_percentage") is None:
            return
        pc = min(100, max(0, round(d["used_percentage"])))
        reset = d.get("resets_at")
        rs = ""
        if reset:
            rs = f" · {fmt_dur(timedelta(seconds=reset - now_epoch))}"
            rs += f" ({datetime.fromtimestamp(reset, tz):%H:%M})"
        flag = " (!)" if d.get("status") in ("warning", "rejected", "exceeded") else ""
        emit(f"{label:<6}{render_bar(pc / 100, BAR_CELLS)} {pc}%{rs}{flag}",
             color=adaptive(color_for(pc)))

    line("5-hr", rl.get("five_hour"))
    line("7-day", rl.get("seven_day"))
    if rl.get("opus"):
        line("Opus", rl.get("opus"))
    cap = usage.get("captured_at")
    if cap:
        age = now_epoch - cap
        note = f"as of {datetime.fromtimestamp(cap, tz):%H:%M}"
        if age > 120:
            note += f" · {fmt_dur(timedelta(seconds=age))} ago (idle)"
        emit(note, color=MUTED)
    sep()


def _darken(c, f=0.55):
    c = c.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return f"#{int(r * f):02x}{int(g * f):02x}{int(b * f):02x}"


def adaptive(c):
    """Make a single hex readable in both menus: darker in light mode (where
    saturated colors wash out), original/vibrant in dark mode. Pairs pass through."""
    return c if "," in c else f"{_darken(c)},{c}"


def color_for(pct):
    # Raw vibrant color — right for the menu bar (dark background, light text).
    # Dropdown rows wrap this in adaptive() since that panel is light in light mode.
    g = TH["grad"]
    return g[3] if pct >= 90 else g[2] if pct >= 70 else g[1] if pct >= 40 else g[0]


# ─────────────────────── context (live agents) ───────────────────────
def ctx_session_label(sv):
    """Which session this is. Prefer Claude's own session title (the one in the
    resume picker); fall back to the project folder, adding the git branch when
    it's a meaningful differentiator between sibling sessions."""
    if sv.get("title"):
        return sv["title"]
    cwd = sv.get("cwd")
    proj = (os.path.basename(cwd.rstrip("/")) if cwd else sv.get("p")) or "?"
    br = sv.get("branch")
    return f"{proj} · {br}" if br and br not in ("main", "master", "HEAD") else proj


def ctx_row(sv, now_ts, mode, agent=False):
    """One agent's context-window fill. The bar/numbers lead in fixed columns so
    rows line up; the (variable-length) session title trails."""
    win = context_window(sv.get("model"), sv.get("peak_ctx", 0), mode)
    used = sv.get("last_ctx", 0)
    frac = used / win if win else 0.0
    pct = min(100, max(0, round(frac * 100)))
    age = now_ts - sv.get("mtime", 0)
    when = "live" if age < CONTEXT_LIVE_MIN * 60 else fmt_age(age)
    if agent:
        aid = (sv.get("agent_id") or "")[:4]
        label = f"↳ {model_short(sv.get('model'))}" + (f" {aid}" if aid else "")
    else:
        label = ctx_session_label(sv)
    uw = f"{compact(used)}/{ctx_label(win)}"
    emit(f"{render_bar(frac, 6)} {pct:>3}% {uw:<9}{when:>6}  {ellipsis(label, 32)}",
         color=adaptive(color_for(pct)))


def emit_context(by_session, now, cfg):
    """Per-agent context-window usage, so you can see at a glance how much room is
    left in each running session. 'Agents' = recently-active main sessions plus the
    subagents they spawned (linked by sessionId), sorted by recency."""
    mode = cfg["context_window"]
    now_ts = now.timestamp()
    main_cut = now_ts - CONTEXT_ACTIVE_MIN * 60
    agent_cut = now_ts - CONTEXT_AGENT_MIN * 60
    mains = sorted(((k, v) for k, v in by_session.items()
                    if not v.get("agent") and v.get("last_ctx", 0) > 0
                    and v.get("mtime", 0) >= main_cut),
                   key=lambda kv: -kv[1]["mtime"])
    agents = sorted(((k, v) for k, v in by_session.items()
                     if v.get("agent") and v.get("last_ctx", 0) > 0
                     and v.get("mtime", 0) >= agent_cut),
                    key=lambda kv: -kv[1]["mtime"])
    if not mains and not agents:
        return
    by_parent = {}
    for k, v in agents:
        by_parent.setdefault(v.get("sid"), []).append((k, v))

    emit("CONTEXT · live agents", color=MUTED, sfimage="gauge", header=True)

    def emit_agents(kids):
        for ak, av in kids[:CONTEXT_MAX_AGENTS]:
            ctx_row(av, now_ts, mode, agent=True)
            shown_agents.add(ak)
        if len(kids) > CONTEXT_MAX_AGENTS:
            emit(f"{'':>29}↳ +{len(kids) - CONTEXT_MAX_AGENTS} more", color=MUTED)

    shown_agents = set()
    for key, sv in mains[:CONTEXT_MAX_ROWS]:
        ctx_row(sv, now_ts, mode)
        emit_agents(by_parent.get(key, []))
    # Subagents still running while their parent has gone idle (or fell past the cap).
    orphans = [(k, v) for k, v in agents if k not in shown_agents]
    if orphans:
        emit_agents(orphans)
    sep()


# ─────────────────────────── main ───────────────────────────
def main():
    global TH, MUTED, PLAN
    cfg = load_config()
    TH = THEMES[cfg["theme"]]
    MUTED = TH["muted"]
    PLAN = read_plan()
    cells = cfg["menubar_cells"]
    title_size = cfg["title_size"]
    compact_view = cfg["view"] == "compact"

    now = datetime.now(timezone.utc)
    now_epoch = now.timestamp()
    tz = datetime.now().astimezone().tzinfo
    today = datetime.now().astimezone(tz).date()
    window = timedelta(hours=BLOCK_HOURS)

    usage = load_usage()
    data = gather(now, tz)          # incremental, cached — see gather()
    all_msgs = data["all_msgs"]

    if all_msgs == 0:
        if usage:
            f5 = usage["rate_limits"]["five_hour"]
            ap = min(100, max(0, round(f5.get("used_percentage") or 0)))
            print(f"{render_bar(ap / 100, cells)} {ap}% | "
                  f"font={MONO} size={title_size} color={color_for(ap)}")
            sep()
            emit_live_limits(usage, now_epoch, tz)
        else:
            print(f"{render_bar(0, cells)} | font={MONO} size={title_size} color={MUTED}")
            sep()
            emit("No Claude Code usage found yet")
        emit("Refresh", refresh=True)
        settings_menu(cfg)
        return

    all_tok = data["all_tok"]
    by_model_all, by_project = data["by_model"], data["by_project"]
    by_session, hour_profile = data["by_session"], data["hour_profile"]
    by_day = {date.fromisoformat(k): v for k, v in data["by_day"].items()}
    peak = data["peak"]

    blocks = build_blocks(sorted(data["recent_records"], key=lambda r: r["ts"]))

    # ── today / week / month from recent records + by-day rollup ──
    month, week_start = today.replace(day=1), today - timedelta(days=6)
    week_w = sum(v[0] for d, v in by_day.items() if d >= week_start)
    month_w = sum(v[0] for d, v in by_day.items() if d >= month)
    today_tok, today_msgs, today_models = new_tokens(), 0, {}
    today_hours, today_sessions = [0.0] * 24, set()
    for r in data["recent_records"]:
        lts = r["ts"].astimezone(tz)
        if lts.date() == today:
            add_tokens(today_tok, r["u"]); today_msgs += 1
            add_tokens(today_models.setdefault(r["model"], new_tokens()), r["u"])
            today_hours[lts.hour] += weighted_one(r["u"])
            today_sessions.add(r["session"])

    # ── derived (history only; live limits come from Anthropic) ──
    last = blocks[-1] if blocks else None
    active = last if (last and now - last["start"] < window) else None
    busiest_day = max(by_day.items(), key=lambda kv: kv[1][0]) if by_day else (today, [0.0, 0])

    # ════════════════ MENU BAR TITLE ════════════════
    # The REAL 5-hour % from Anthropic (live, cross-surface). The optional trailer
    # is a countdown to reset (default), the token count, or nothing.
    five = usage["rate_limits"]["five_hour"] if usage else None
    reset_epoch = five.get("resets_at") if five else None
    extra = ""
    if cfg["menubar_extra"] == "countdown" and reset_epoch:
        extra = f" · {fmt_dur(timedelta(seconds=reset_epoch - now_epoch))}"
    elif cfg["menubar_extra"] == "tokens":
        extra = f" · {compact(weighted(active['tokens']) if active else 0)}"
    if usage:
        ap = min(100, max(0, round(five.get("used_percentage") or 0)))
        print(f"{render_bar(ap / 100, cells)} {ap}%{extra} | "
              f"font={MONO} size={title_size} color={color_for(ap)}")
    else:
        print(f"{render_bar(0, cells)} set up | "
              f"font={MONO} size={title_size} color={MUTED}")
    sep()

    # ════════════════ LIVE LIMITS (real, from Anthropic) ════════════════
    if usage:
        emit_live_limits(usage, now_epoch, tz)
    else:
        emit("USAGE LIMITS", color=MUTED, sfimage="bolt.fill", header=True)
        emit("Live usage not set up", color=MUTED)
        emit("Run install.sh to show real 5h / 7d limits", sub=1,
             open_path="https://github.com/dashpes/burnbar#live-usage-real-limits-not-estimates")
        sep()

    # ════════════════ CONTEXT (live agents) ════════════════
    emit_context(by_session, now, cfg)

    # ════════════════ TODAY ════════════════
    emit("TODAY", color=MUTED, sfimage="calendar", header=True)
    if compact_view:
        emit(f"{compact(weighted(today_tok))} tok · {today_msgs} msgs · "
             f"{len(today_sessions)} sessions")
        emit(f"By hour  {spark(today_hours)}")
    else:
        emit(f"Total       {compact(weighted(today_tok)):>8} tok")
        emit(f"Messages    {today_msgs:>8}")
        emit(f"Sessions    {len(today_sessions):>8}")
        if any(today_hours):
            emit(f"Peak hour   {today_hours.index(max(today_hours)):02d}:00")
        emit(f"By hour  {spark(today_hours)}")
        emit("By model")
        for m, mt in sorted(today_models.items(), key=lambda kv: -weighted(kv[1])):
            emit(f"{m.replace('claude-',''):<16}{compact(weighted(mt)):>8}", sub=1)
    sep()

    if compact_view:
        # ── compact: tuck the heavy stats behind one submenu ──
        emit("More stats", sfimage="chart.bar.fill")
        emit(f"Week total   {compact(week_w):>8} tok", sub=1)
        emit(f"Month total  {compact(month_w):>8} tok", sub=1)
        emit(f"All-time     {compact(weighted(all_tok)):>8} tok", sub=1)
        emit(f"Messages     {all_msgs:>8}", sub=1)
        emit(f"Sessions     {len(by_session):>8}", sub=1)
        emit(f"Peak block   {compact(peak['w']) if peak else '-':>8} tok", sub=1)
        bd, (bw, _bm) = busiest_day
        emit(f"Busiest day  {compact(bw):>8} tok", sub=1)
        sep()
    else:
        emit_full_sections(blocks, by_day, by_model_all, by_project, by_session,
                           hour_profile, all_tok, all_msgs, today, tz,
                           week_w, month_w, peak, busiest_day, active, now)

    settings_menu(cfg)
    sep()
    emit("Refresh", refresh=True, sfimage="arrow.clockwise")
    emit("Open transcripts folder", sfimage="folder",
         open_path=os.path.expanduser("~/.claude/projects"))


def emit_full_sections(blocks, by_day, by_model_all, by_project, by_session,
                       hour_profile, all_tok, all_msgs, today, tz,
                       week_w, month_w, peak, busiest_day, active, now):
    # LAST 7 DAYS
    emit("LAST 7 DAYS", color=MUTED, sfimage="chart.bar.fill", header=True)
    days = sorted(by_day.items(), reverse=True)[:7]
    daymax = max((v[0] for _, v in days), default=1) or 1
    for d, (tok, _msgs) in days:
        tag = "  ·today" if d == today else ""
        emit(f"{d.strftime('%a %m-%d')} {render_bar(tok/daymax, 8)} "
             f"{compact(tok):>6}{tag}")
    emit(f"Week total  {compact(week_w):>8} tok")
    emit(f"Month total {compact(month_w):>8} tok")
    sep()

    # ALL TIME
    first_day = min(by_day) if by_day else today
    span_days = (today - first_day).days + 1
    emit("ALL TIME", color=MUTED, sfimage="clock.arrow.circlepath", header=True)
    if PLAN:
        emit(f"Plan        {PLAN_LABEL[PLAN]:>8}")
    emit(f"Total       {compact(weighted(all_tok)):>8} tok")
    emit(f"Raw tokens  {compact(raw_total(all_tok)):>8}")
    emit(f"Messages    {all_msgs:>8}")
    emit(f"Sessions    {len(by_session):>8}")
    emit(f"Projects    {len(by_project):>8}")
    emit(f"Since       {first_day.strftime('%Y-%m-%d')} ({span_days}d)")
    emit(f"Daily avg   {compact(weighted(all_tok)/max(1,span_days)):>8} tok")
    emit(f"By hour  {spark(hour_profile)}")
    emit("By model")
    for m, mt in sorted(by_model_all.items(), key=lambda kv: -weighted(kv[1])):
        emit(f"{m.replace('claude-',''):<16}{compact(weighted(mt)):>8}", sub=1)
    emit("By project")
    for p, pv in sorted(by_project.items(), key=lambda kv: -weighted(kv[1]["t"]))[:12]:
        emit(f"{p[:18]:<18}{compact(weighted(pv['t'])):>8}", sub=1)
    emit("Top sessions")
    for _sid, sv in sorted(by_session.items(),
                           key=lambda kv: -weighted(kv[1]["t"]))[:8]:
        when = (parse_ts(sv["last"]).astimezone(tz).strftime("%m-%d")
                if sv.get("last") else "  -  ")
        emit(f"{sv['p'][:12]:<12} {when} {compact(weighted(sv['t'])):>7} "
             f"{sv['m']:>4}m", sub=1)
    sep()

    # RECORDS
    emit("RECORDS", color=MUTED, sfimage="trophy.fill", header=True)
    if peak:
        pb_when = parse_ts(peak["start"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")
        emit(f"Peak block  {compact(peak['w']):>8} tok")
        emit(f"            {pb_when}", color=MUTED)
    bd, (bw, bm) = busiest_day
    emit(f"Busiest day {compact(bw):>8} tok")
    emit(f"            {bd.strftime('%Y-%m-%d')} · {bm} msgs", color=MUTED)
    sep()

    # RECENT BLOCKS
    emit("Recent blocks")
    for b in list(reversed(blocks))[:10]:
        s = b["start"].astimezone(tz).strftime("%m-%d %H:%M")
        live = " (live)" if (b is blocks[-1] and active is not None) else ""
        emit(f"{s}  {compact(weighted(b['tokens'])):>7} · {b['msgs']:>3}m{live}",
             sub=1, color=adaptive(TH["grad"][0]) if live else None)
    sep()


def settings_menu(cfg):
    sep()
    emit("Settings", color=MUTED, sfimage="gearshape", header=True)

    def mark(active):
        return "[x] " if active else "[ ] "

    emit("View")
    emit(f"{mark(cfg['view']=='compact')}Compact", sub=1, action=SELF,
         args=["--set", "view=compact"], refresh=True)
    emit(f"{mark(cfg['view']=='detailed')}Detailed", sub=1, action=SELF,
         args=["--set", "view=detailed"], refresh=True)

    emit("Theme")
    for name in THEMES:
        emit(f"{mark(cfg['theme']==name)}{name.capitalize()}", sub=1, action=SELF,
             args=["--set", f"theme={name}"], refresh=True)

    emit("Menu-bar trailer")
    for opt, lbl in (("countdown", "Reset countdown"), ("tokens", "Token count"),
                     ("none", "None")):
        emit(f"{mark(cfg['menubar_extra']==opt)}{lbl}", sub=1, action=SELF,
             args=["--set", f"menubar_extra={opt}"], refresh=True)

    emit("Menu-bar width")
    for w in (3, 5, 8, 10):
        emit(f"{mark(cfg['menubar_cells']==w)}{w}", sub=1, action=SELF,
             args=["--set", f"menubar_cells={w}"], refresh=True)

    emit("Context window")
    for opt, lbl in (("auto", "Auto-detect"), ("200k", "200K"), ("1m", "1M")):
        emit(f"{mark(cfg['context_window']==opt)}{lbl}", sub=1, action=SELF,
             args=["--set", f"context_window={opt}"], refresh=True)

    # Live-usage status: on when the statusLine bridge has written real data.
    if load_usage():
        emit("Live usage  connected", color=MUTED)
    else:
        emit("Live usage  not set up", color=MUTED)
        emit("Set up live limits (real %, reset times)", sub=1,
             open_path="https://github.com/dashpes/burnbar#live-usage-real-limits-not-estimates")

    emit("Edit config file", sub=0, open_path=CONFIG_PATH)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        handle_cli(sys.argv[1:])
        sys.exit(0)
    try:
        main()
    except Exception as e:
        print("burnbar !")
        print("---")
        print(f"Error: {e} | font={MONO} size=13 color=#ff453a")
        import traceback
        for ln in traceback.format_exc().splitlines():
            print(f"{ln} | font={MONO} size=10")
        print("Refresh | refresh=true")
