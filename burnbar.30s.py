#!/usr/bin/env python3
# <bitbar.title>burnbar</bitbar.title>
# <bitbar.version>1.6.1</bitbar.version>
# <bitbar.author>burnbar</bitbar.author>
# <bitbar.desc>CLI agent usage (Claude Code + Cursor): live burn bar + stats dropdown.</bitbar.desc>
# <bitbar.dependencies>python3</bitbar.dependencies>
# <swiftbar.hideAbout>false</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>false</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>false</swiftbar.hideDisablePlugin>
"""
burnbar — a SwiftBar/xbar plugin.

Menu bar:  a live progress bar for your current usage (Claude rate limits when
           available, else Cursor context fill).
Dropdown:  one merged LIVE AGENTS list (every provider, worst context first),
           the real usage limits, a today summary, then Stats and Settings
           submenus. Providers are auto-detected from what's installed.

Offline-first: reads local transcripts + statusLine bridge files. No API keys,
no pricing tables. Optional once-a-day GitHub version check only.

Settings are stored in ~/.config/burnbar/config.json and changed by clicking
items in the Settings submenu (which re-invoke this script with --set).
"""

import base64
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from datetime import date, datetime, timedelta, timezone

# ─────────────────────────── fixed config ───────────────────────────
BLOCK_HOURS = 5
BAR_CELLS = 10                   # bar width inside the dropdown
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
CONFIG_PATH = os.path.expanduser("~/.config/burnbar/config.json")
USAGE_PATH = os.path.expanduser("~/.config/burnbar/claude/usage.json")
USAGE_PATH_LEGACY = os.path.expanduser("~/.config/burnbar/usage.json")
CLAUDE_SESSIONS_PATH = os.path.expanduser("~/.config/burnbar/claude/sessions.json")
CURSOR_LIVE_PATH = os.path.expanduser("~/.config/burnbar/cursor/live.json")
CURSOR_SESSIONS_PATH = os.path.expanduser("~/.config/burnbar/cursor/sessions.json")
CURSOR_PROJECTS = os.path.expanduser("~/.cursor/projects")
CURSOR_CHATS = os.path.expanduser("~/.cursor/chats")
CACHE_PATH = os.path.expanduser("~/.config/burnbar/cache.json")  # Claude per-file rollups
UPDATE_PATH = os.path.expanduser("~/.config/burnbar/update.json")  # daily update check
CACHE_VERSION = 5                # bumped: per-file aggregates now carry context info

UPDATE_INTERVAL = 86400          # check GitHub for a newer version at most once a day
RAW_BASE = os.environ.get(       # where install.sh + the plugin live (overridable for forks)
    "BURNBAR_RAW", "https://raw.githubusercontent.com/dashpes/burnbar/main")
# Owner/repo derived from RAW_BASE so a fork only has to override one var.
_OWNER_REPO = re.search(r"githubusercontent\.com/([^/]+)/([^/]+)/", RAW_BASE)
_OWNER, _REPO = (_OWNER_REPO.group(1), _OWNER_REPO.group(2)) if _OWNER_REPO \
    else ("dashpes", "burnbar")
API_BASE = f"https://api.github.com/repos/{_OWNER}/{_REPO}"
# Stable "latest release" asset URL. The daily version check fetches this tiny
# file to learn the newest version; because it's a real release asset, GitHub
# increments its public download_count on each fetch — an anonymous, server-side
# tally of active installs. No per-user data is sent; it's the same version-only
# GET, and turning the update check off (Settings) opts out of the count too.
VERSION_ASSET_URL = f"https://github.com/{_OWNER}/{_REPO}/releases/latest/download/version.txt"
RECENT_DAYS = 3                  # keep it lean: only files newer than this are
#                                  re-parsed each refresh (for recent blocks); older
#                                  ones are read once and served from cache. Smaller
#                                  = less CPU/RAM per refresh, shorter recent history.
CACHE_READ_WEIGHT = 0.1          # cache reads are ~10x lighter; down-weight burn

# ── context window (how much of the model's window each agent has filled) ──
CTX_200K = 200_000               # standard Claude Code window
CTX_1M = 1_000_000               # the 1M-context Opus
CONTEXT_ACTIVE_MIN = 10          # fallback only (live processes unreadable): treat a session
                                 # touched this recently as still open
CONTEXT_AGENT_MIN = 15           # subagents are ephemeral: only show ones still running
CONTEXT_LIVE_MIN = 5             # freshest sessions get a "live" tag instead of an age
CONTEXT_MAX_ROWS = 6             # cap on main sessions shown *per provider*
CONTEXT_MAX_AGENTS = 5           # cap on subagents shown per parent
CONTEXT_NAME_W = 20              # name column width (titles truncated to fit the row).
#                                  Narrow: each row also carries an SF Symbol (which
#                                  indents it) and a spelled-out provider name.
CONTEXT_TEXT_SIZE = 11           # context rows are a touch smaller, so longer titles fit
CONTEXT_WARN_PCT = 70            # nearing the window: compaction is coming
CONTEXT_CRIT_PCT = 85            # compaction imminent — you're about to lose detail
CURSOR_CTX_STALE_MIN = 30        # drop Cursor statusLine readings older than this
CLAUDE_CTX_STALE_MIN = 30        # ditto for Claude, once the bridge registry exists.
#                                  Both providers now answer "is this open?" the same
#                                  way: their statusLine hook fired recently for that
#                                  exact session id. Idle longer than this and the
#                                  context isn't moving anyway, so there's nothing to
#                                  watch — better than listing a session you closed.
COMMITS_PATH = os.path.expanduser("~/.config/burnbar/commits.json")
COMMITS_TTL = 300                # commit scan is the slowest thing here (a find(1)
#                                  plus a git log per repo); today's count doesn't
#                                  move fast enough to redo it every 30s refresh.

# ── context rot bands (absolute tokens) ──
# Degradation tracks *absolute* context size far more than % of window: a 1M-window
# session at 30% holds 300K tokens and is deeper into rot than a 200K one at 85%.
# So % drives the compaction warning above, and these bands drive the quality one.
# The thresholds come from published long-context evals, not round numbers:
#   32K  NoLiMa (Adobe, ICML'25) — 11 of 13 models claiming >=128K fall below half
#        their short-context baseline here.
#   60K  Chroma "Context Rot" (2025) — all 18 frontier models degrade well before
#        their limit; a 200K-window model shows real degradation by ~50K.
#  128K  RULER (NVIDIA) — effective context is ~50-65% of the advertised window,
#        so past this recall is unreliable whatever the window claims.
# These are retrieval/reasoning benchmark findings, not a measured cliff in coding
# agents: calibrated heuristics, not laws. Coding sessions likely fare *worse* —
# Chroma found distractors compound the effect, and a codebase is full of near-misses.
#
# Bands scale with the window, but *sub-linearly* — which is why this is a table
# and not a percentage. A model built for 1M genuinely holds up past where a 200K
# one gives out (published recall stays high to ~600-700K on simple retrieval), so
# flat thresholds over-warn on big windows. But it doesn't hold up proportionally:
# on multi-needle work, effective context for current frontier models lands in the
# 200-400K band, with one measured flagship at 57.5% over 256-512K and 36.6% over
# 512K-1M. The window is a capacity limit, not a quality guarantee.
#
# Bands stay on the conservative side of those numbers on purpose: the evals above
# are retrieval/comprehension tests, while an agentic coding session is
# multi-needle deep comprehension — the hardest case — with a codebase full of
# near-miss distractors, which Chroma found compounds the decay. Fiction.LiveBench,
# which tests comprehension rather than recall, sees slippage closer to 32K and
# suggests a usable band of 16-64K for most models.
CTX_BAND_TABLE = (
    # window     drifting  degraded      rot
    (200_000, (32_000, 60_000, 128_000)),
    (1_000_000, (100_000, 200_000, 400_000)),
)
CTX_BAND_NAMES = ("drifting", "degraded", "rot")
CTX_RISK_TIER = 2                # call out the advice line from "degraded" up
MONO = "Menlo"

# Provider identity in the merged LIVE AGENTS list is carried by *shape*, not
# colour — colour is already spoken for by the rot band, and one channel can't
# encode two independent signals. The SF Symbol also sits outside the monospace
# run, so the bars stay column-aligned however wide the icon renders.
AGENT_ICON = {"claude": "bolt.fill", "cursor": "cursorarrow"}
SUBAGENT_ICON = "arrow.turn.down.right"   # subagents nest under their parent row
# ...but the icon alone doesn't carry it. A monochrome glyph tinted to the row's rot
# colour reads as decoration, not identity — especially when every visible row
# happens to be the same provider, so there's no contrast to decode it against. The
# name is spelled out; the icon just reinforces it.
AGENT_LABEL = {"claude": "Claude", "cursor": "Cursor"}

# ── commits-today tracking ──
# Default folders scanned for git repos when "commit_dirs" isn't set in config.
# The author defaults to the user's own git identity (see git_identity), so the
# count is theirs out of the box — overridable via "commit_author" in config.json.
COMMIT_DIRS_DEFAULT = [os.path.expanduser(p) for p in
                       ("~/Developer", "~/Projects", "~/Code", "~/dev",
                        "~/src", "~/repos")]
MUTED = "#8e8e93"                # section headers / secondary notes
SELF = os.path.realpath(__file__)


def parse_version_header(text):
    """Pull the version out of a plugin file's BitBar metadata header."""
    m = re.search(r"<bitbar\.version>([^<]+)</bitbar\.version>", text or "")
    return m.group(1).strip() if m else None


def _read_self_version():
    try:
        with open(SELF, encoding="utf-8") as f:
            return parse_version_header(f.read(2048))
    except Exception:
        return None


# Single source of truth for the version: the <bitbar.version> header at the top of
# this file. Bump it there alone — the daily update check and CI release both read it.
VERSION = _read_self_version() or "0.0.0"

# ── user-configurable defaults (overridden by config.json) ──
DEFAULTS = {
    "theme": "default",          # see THEMES
    "menubar_cells": 5,          # bar width in the menu bar
    "title_size": 11,            # menu-bar font size
    "menubar_extra": "countdown",  # trailer after the %: countdown | tokens | none
    "context_window": "auto",    # how to size the context bar: auto | 200k | 1m
    "update_check": "on",        # daily "is there a newer burnbar?" check: on | off
    "commits": "on",             # show today's git commit count in TODAY: on | off
    "commit_author": "",         # whose commits to count; "" = your git identity
    "commit_dirs": [],           # folders to scan for repos; [] = sensible defaults
    "providers": "auto",         # auto | claude | cursor | both
}
MENUBAR_EXTRAS = ("countdown", "tokens", "none")
CONTEXT_WINDOWS = ("auto", "200k", "1m")
PROVIDERS = ("auto", "claude", "cursor", "both")

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


# ── theme swatches: SwiftBar can't render multi-color text (ANSI is 16-color and
#    mangles truecolor), so to preview a theme's gradient in the picker we draw a
#    tiny PNG of its colour stops and hand it to the item as a base64 `image=`.
#    Pure stdlib (zlib + struct) — no Pillow, keeps burnbar dependency-free.
def _png(width, height, pixels):
    """Encode raw RGB bytes (width*height*3) as a minimal 8-bit truecolor PNG."""
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)                       # filter type 0 (none) per scanline
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def theme_swatch(grad, seg=11, height=12):
    """A base64 PNG of a theme's gradient: one solid block per colour stop — which
    mirrors how the burn bar actually picks a single stop by severity, not a blend."""
    stops = [(int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)) for c in grad]
    width = seg * len(stops)
    row = bytearray()
    for x in range(width):
        row += bytes(stops[min(x // seg, len(stops) - 1)])
    return base64.b64encode(_png(width, height, bytes(row) * height)).decode()


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
    cfg.pop("view", None)        # retired in 1.5: one layout, deep stats in a submenu
    if cfg.get("menubar_extra") not in MENUBAR_EXTRAS:
        cfg["menubar_extra"] = "countdown"
    if cfg.get("context_window") not in CONTEXT_WINDOWS:
        cfg["context_window"] = "auto"
    if cfg.get("update_check") not in ("on", "off"):
        cfg["update_check"] = "on"
    if cfg.get("commits") not in ("on", "off"):
        cfg["commits"] = "on"
    if cfg.get("providers") not in PROVIDERS:
        cfg["providers"] = "auto"
    try:
        cfg["menubar_cells"] = max(3, min(12, int(cfg.get("menubar_cells", 5))))
    except Exception:
        cfg["menubar_cells"] = 5
    if not isinstance(cfg.get("commit_author"), str):
        cfg["commit_author"] = ""
    if not isinstance(cfg.get("commit_dirs"), list):
        cfg["commit_dirs"] = []
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
    """`--set key=value [key=value ...]` writes config; SwiftBar refreshes after.
    `--self-update` runs the in-place update (the menu's Update row re-invokes us
    with this flag rather than handing SwiftBar a raw `curl … | bash` command,
    which a terminal=true action word-splits and breaks — see self_update)."""
    if argv and argv[0] == "--self-update":
        self_update()
        return
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
    if "opus" in (model or "").lower() or peak_ctx > CTX_200K:
        return CTX_1M
    return CTX_200K


def ctx_label(win):
    return "1M" if win >= CTX_1M else f"{win // 1000}K"


def ctx_band_floors(win):
    """The (drifting, degraded, rot) token floors for a given window size.

    Picks the largest calibrated window class the model's window reaches, so a
    1M model gets 1M-class bands and everything smaller gets the 200K ones."""
    floors = CTX_BAND_TABLE[0][1]
    for cls, f in CTX_BAND_TABLE:
        if (win or 0) >= cls:
            floors = f
    return floors


def ctx_band(tokens, win=None):
    """Context-rot tier for a context size: (tier, label).

    tier 0 sharp · 1 drifting · 2 degraded · 3 rot. Driven by absolute tokens
    against window-scaled floors — a 1M session at 30% is carrying 300K and is
    further gone than a 200K one at 85%, but it isn't judged by 200K's ruler."""
    tokens = tokens or 0
    floors = ctx_band_floors(win)
    for i in range(len(floors) - 1, -1, -1):
        if tokens >= floors[i]:
            return i + 1, CTX_BAND_NAMES[i]
    return 0, "sharp"


def band_color(tier):
    """Rot tier -> theme gradient stop (same 4 stops the % bars use)."""
    return TH["grad"][max(0, min(tier, 3))]


def ctx_tags(tokens, pct, win=None):
    """The two independent signals for one session, as display tags:
    quality (tokens vs the window's rot bands) and compaction proximity (% full)."""
    tier, label = ctx_band(tokens, win)
    tags = [label] if tier else []
    if pct >= CONTEXT_CRIT_PCT:
        tags.append("compacting")
    elif pct >= CONTEXT_WARN_PCT:
        tags.append("near full")
    return tier, tags


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


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
    """Live Claude rate_limits captured by the statusLine bridge, or None.
    Migrates pre-1.4 ~/.config/burnbar/usage.json into claude/usage.json once."""
    for path in (USAGE_PATH, USAGE_PATH_LEGACY):
        try:
            with open(path) as f:
                u = json.load(f)
            if not (u.get("rate_limits") or {}).get("five_hour"):
                continue
            if path == USAGE_PATH_LEGACY and not os.path.exists(USAGE_PATH):
                try:
                    os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
                    shutil.copy2(path, USAGE_PATH)
                except Exception:
                    pass
            return u
        except Exception:
            continue
    return None


def load_cursor_live():
    """Latest Cursor context_window capture (single-file, for menubar fallback)."""
    try:
        with open(CURSOR_LIVE_PATH) as f:
            u = json.load(f)
        if u.get("context_window") is not None or u.get("session_id"):
            return u
    except Exception:
        pass
    return None


def load_cursor_session_map():
    """Per-session Cursor context registry written by the statusLine bridge.
    Falls back to live.json alone so a single-session install still works."""
    sessions = {}
    try:
        with open(CURSOR_SESSIONS_PATH) as f:
            store = json.load(f)
        raw = store.get("sessions") if isinstance(store, dict) else None
        if isinstance(raw, dict):
            sessions.update(raw)
    except Exception:
        pass
    live = load_cursor_live()
    if live:
        sid = live.get("session_id") or "_live"
        # Prefer the registry entry if newer; else seed from live.json.
        prev = sessions.get(sid)
        if not prev or (live.get("captured_at") or 0) >= (prev.get("captured_at") or 0):
            sessions[sid] = live
    return sessions


def cursor_ctx_pct(entry):
    """used_percentage from a Cursor live/session entry, or None."""
    try:
        p = (entry.get("context_window") or {}).get("used_percentage")
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def cursor_ctx_tokens(entry):
    """Absolute context tokens for a Cursor session, or None.

    Cursor's `total_input_tokens` is itself derived from used_percentage (its docs
    say so, and the arithmetic confirms it), so deriving from pct x window is the
    same number with no extra assumption — and it still works when the field is null.
    """
    cw = entry.get("context_window") or {}
    pct = cursor_ctx_pct(entry)
    try:
        size = float(cw.get("context_window_size") or 0)
    except (TypeError, ValueError):
        return None
    if pct is None or size <= 0:
        return None
    return int(pct / 100.0 * size)


def cursor_session_label(entry):
    name = entry.get("session_name")
    if name:
        return name
    cwd = entry.get("cwd") or ""
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or cwd
    return entry.get("session_id") or "session"


def fresh_cursor_sessions(session_map, now_epoch, stale_min=CURSOR_CTX_STALE_MIN):
    """Sessions with a usable context % that statusLine refreshed recently, worst
    first. Ranked by absolute tokens (the rot risk), not % — a 62%/256K session is
    carrying more context than an 85%/200K one, so it leads."""
    cut = now_epoch - stale_min * 60
    rows = []
    for sid, entry in (session_map or {}).items():
        if (entry.get("captured_at") or 0) < cut:
            continue
        pct = cursor_ctx_pct(entry)
        if pct is None:
            continue
        rows.append((sid, entry, pct))
    rows.sort(key=lambda r: (-(cursor_ctx_tokens(r[1]) or 0), -r[2],
                             -(r[1].get("captured_at") or 0)))
    return rows


def claude_registry_sessions(now_epoch, stale_min=CLAUDE_CTX_STALE_MIN):
    """[(session_id, last_seen)] the Claude statusLine bridge refreshed within
    stale_min, newest first — or None if there's no registry to answer.

    None means "unknown, go guess" (the pgrep/lsof path). It is deliberately not
    the same as an empty list, which means "the bridge is running and reports
    nothing open"."""
    try:
        with open(CLAUDE_SESSIONS_PATH) as f:
            store = json.load(f)
        sessions = store.get("sessions") if isinstance(store, dict) else None
    except Exception:
        return None
    if not isinstance(sessions, dict):
        return None
    cut = now_epoch - stale_min * 60
    rows = [(sid, v.get("captured_at") or 0) for sid, v in sessions.items()
            if isinstance(v, dict) and (v.get("captured_at") or 0) >= cut]
    rows.sort(key=lambda r: -r[1])
    return rows


def claude_proc_count():
    """How many Claude Code CLI processes are running, or None if unreadable.

    Just the count — the caller only needs to know how many sessions exist, not
    where, so this skips the lsof that live_session_cwds pays for."""
    try:
        out = subprocess.run(["/usr/bin/pgrep", "-x", "claude"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    return len(out.split())


def live_claude_session_ids(now_epoch, stale_min=CLAUDE_CTX_STALE_MIN):
    """Which Claude sessions are open right now, as a set of ids (None = unknown).

    Neither available signal is sufficient alone, and they fail in opposite
    directions:

      · the registry knows *which* session each heartbeat came from, but a closed
        tab simply stops beating — indistinguishable from an idle one until the
        entry ages out;
      · the process count knows *how many* sessions exist, but nothing about which.

    So: cross-check. A session seen within CONTEXT_LIVE_MIN is kept
    unconditionally — a heartbeat seconds old is stronger evidence than any
    process scan, and process enumeration can be restricted (under a sandbox
    pgrep can miss even the CLI hosting the caller), so it must never be able to
    veto a session we just heard from. Quieter entries need the count to
    corroborate them, and are taken newest-first."""
    reg = claude_registry_sessions(now_epoch, stale_min)
    if reg is None:
        return None
    fresh_cut = now_epoch - CONTEXT_LIVE_MIN * 60
    beating = [sid for sid, ts in reg if ts >= fresh_cut]
    quiet = [sid for sid, ts in reg if ts < fresh_cut]
    n = claude_proc_count()
    if n is None:
        return set(beating) | set(quiet)     # can't corroborate; trust the registry
    return set(beating) | set(quiet[:max(0, n - len(beating))])


def claude_live_agents(by_session, now, cfg):
    """The open Claude sessions and the subagents they're currently running.

    Returns (mains, by_parent): `mains` is [(key, sv), …] for sessions backed by a
    live `claude` process, capped at CONTEXT_MAX_ROWS; `by_parent` maps a main's key
    to its running subagents, with orphans (parent idle, or past the cap) under None.

    'Agents' = the open main sessions (one per live `claude` process) plus the
    subagents they're currently running."""
    del cfg                      # window sizing happens at row-build time
    if not by_session:
        return [], {}
    now_ts = now.timestamp()

    def norm(sv):
        return os.path.normpath(sv.get("cwd") or "")

    def agent_running(av):
        # A subagent's parent session blocks while the subagent runs; once the
        # parent writes again (the tool result) the subagent has finished — so it's
        # live only until its parent's transcript passes it. Age is a backstop in
        # case the parent was killed mid-run and never resumes.
        if now_ts - av.get("mtime", 0) > CONTEXT_AGENT_MIN * 60:
            return False
        parent = by_session.get(av.get("sid"))
        if parent and not parent.get("agent"):
            return av.get("mtime", 0) >= parent.get("mtime", 0)
        return True

    # Cheap in-memory candidates first, so we only shell out to find live
    # processes when there's actually something that could be shown.
    cand = sorted((kv for kv in by_session.items()
                   if not kv[1].get("agent") and kv[1].get("last_ctx", 0) > 0),
                  key=lambda kv: -kv[1].get("mtime", 0))
    agent_cand = sorted(((k, v) for k, v in by_session.items()
                         if v.get("agent") and v.get("last_ctx", 0) > 0
                         and agent_running(v)),
                        key=lambda kv: -kv[1].get("mtime", 0))
    if not cand and not agent_cand:
        return [], {}

    live_ids = live_claude_session_ids(now_ts)
    if live_ids is None:
        # No bridge registry: fall back to inferring liveness from running processes.
        live_n, by_dir = live_session_cwds()
        mains = select_live_mains(cand, live_n, by_dir, now_ts)[:CONTEXT_MAX_ROWS]
        # Subagents: still-running ones (parent hasn't resumed) in a live working dir
        # (or, when we can't see dirs, just the still-running ones).
        agents = [kv for kv in agent_cand if by_dir is None or norm(kv[1]) in by_dir]
    else:
        # Exact: these session ids are open, whatever their working directory.
        mains = [kv for kv in cand if kv[0] in live_ids
                 or kv[1].get("sid") in live_ids][:CONTEXT_MAX_ROWS]
        agents = [kv for kv in agent_cand if kv[1].get("sid") in live_ids]

    shown = {k for k, _ in mains}
    by_parent = {}
    for k, v in agents:
        sid = v.get("sid")
        by_parent.setdefault(sid if sid in shown else None, []).append((k, v))
    return mains, by_parent


def claude_context_rows(mains, cfg):
    """Live Claude sessions as (label, pct, tokens, window, sv), fullest first."""
    mode = cfg["context_window"]
    rows = []
    for _k, sv in mains:
        win = context_window(sv.get("model"), sv.get("peak_ctx", 0), mode)
        used = sv.get("last_ctx", 0)
        rows.append((ctx_session_label(sv),
                     min(100.0, 100.0 * used / win) if win else 0.0,
                     used, win, sv))
    rows.sort(key=lambda r: -r[2])
    return rows


def collect_context_risks(claude_rows, cursor_rows, warn=CONTEXT_WARN_PCT):
    """Unified at-risk list, worst first: (provider, label, pct, tokens, tier, tags).

    A session is at risk for either reason, and they're genuinely different:
    quality decay (tokens past the window's rot bands) or imminent compaction
    (% of window). Ranking is by rot tier first — how degraded the session
    actually is — then by raw tokens to break ties within a tier."""
    risks = []
    rows = ([("Claude", label, pct, tok, win) for label, pct, tok, win, _sv in claude_rows]
            + [("Cursor", cursor_session_label(entry), pct, cursor_ctx_tokens(entry),
                (entry.get("context_window") or {}).get("context_window_size"))
               for _sid, entry, pct in cursor_rows])
    for prov, label, pct, tok, win in rows:
        tier, tags = ctx_tags(tok, pct, win)
        if tier >= CTX_RISK_TIER or pct >= warn:
            risks.append((prov, label, pct, tok or 0, tier, tags))
    risks.sort(key=lambda r: (-r[4], -r[3], -r[2]))
    return risks


def unified_agent_rows(mains, cfg, cursor_rows, now_epoch):
    """Every live agent, from every provider, in one list — worst context first.

    burnbar used to print this three times over (a top-of-menu risk strip, then
    again inside each provider's own section). One list, ranked the way the risk
    itself ranks — rot tier, then absolute tokens, then window fill — says the same
    thing once. Provider comes back as a per-row icon (see AGENT_ICON)."""
    mode = cfg["context_window"]
    rows = []
    for key, sv in mains:
        win = context_window(sv.get("model"), sv.get("peak_ctx", 0), mode)
        used = sv.get("last_ctx", 0)
        rows.append({"prov": "claude", "key": key, "label": ctx_session_label(sv),
                     "tok": used, "win": win,
                     "pct": min(100.0, 100.0 * used / win) if win else 0.0,
                     "age": now_epoch - sv.get("mtime", now_epoch)})
    for sid, entry, pct in (cursor_rows or [])[:CONTEXT_MAX_ROWS]:
        cw = entry.get("context_window") or {}
        rows.append({"prov": "cursor", "key": sid,
                     "label": cursor_session_label(entry),
                     "tok": cursor_ctx_tokens(entry) or 0,
                     "win": cw.get("context_window_size"), "pct": pct,
                     "age": now_epoch - (entry.get("captured_at") or now_epoch)})
    for r in rows:
        r["tier"], r["tags"] = ctx_tags(r["tok"], r["pct"], r["win"])
        r["at_risk"] = r["tier"] >= CTX_RISK_TIER or r["pct"] >= CONTEXT_WARN_PCT
    rows.sort(key=lambda r: (-r["tier"], -r["tok"], -r["pct"]))
    return rows


def emit_agent_row(r):
    """One agent: name, window fill, and the two signals that can disagree —
    bar *length* is how full the window is, bar *colour* is the rot band."""
    # Window labels vary in width ("1M" vs "256K"), so pad to the widest — otherwise
    # the age/tag tail ragged-edges down the column.
    win = f"/{ctx_label(r['win']):<4}" if r.get("win") else " " * 5
    when = "live" if r["age"] < CONTEXT_LIVE_MIN * 60 else fmt_age(r["age"])
    tail = (" · " + " · ".join(r["tags"])) if r["tags"] else ""
    emit(f"{AGENT_LABEL.get(r['prov'], '?'):<6} "
         f"{ellipsis(r['label'], CONTEXT_NAME_W):<{CONTEXT_NAME_W}} "
         f"{render_bar(r['pct'] / 100, 6)} {round(r['pct']):>3}% "
         f"{compact(r['tok']):>5}{win} · {when}{tail}",
         color=adaptive(band_color(r["tier"])), size=CONTEXT_TEXT_SIZE,
         sfimage=AGENT_ICON.get(r["prov"]))


def emit_subagent_rows(kids, now_ts, cfg):
    """A main session's running subagents, as sibling rows nested under it.

    They stay in the main menu rather than becoming a submenu of the parent row: a
    subagent burning context is exactly the thing you'd never think to hover for.
    The turn-down arrow is an SF Symbol so these line up with the icon-bearing
    parent rows — a text ↳ would sit in the monospace run and knock the bars out of
    column."""
    mode = cfg["context_window"]
    for _ak, av in kids[:CONTEXT_MAX_AGENTS]:
        win = context_window(av.get("model"), av.get("peak_ctx", 0), mode)
        used = av.get("last_ctx", 0)
        frac = used / win if win else 0.0
        pct = min(100, max(0, round(frac * 100)))
        age = now_ts - av.get("mtime", now_ts)
        when = "live" if age < CONTEXT_LIVE_MIN * 60 else fmt_age(age)
        aid = (av.get("agent_id") or "")[:4]
        name = f"{model_short(av.get('model'))}" + (f" {aid}" if aid else "")
        tier, tags = ctx_tags(used, pct, win)
        tail = (" · " + " · ".join(tags)) if tags else ""
        emit(f"{'':<6} {ellipsis(name, CONTEXT_NAME_W):<{CONTEXT_NAME_W}} "
             f"{render_bar(frac, 6)} {pct:>3}% "
             f"{compact(used):>5}/{ctx_label(win):<4} · {when}{tail}",
             color=adaptive(band_color(tier)), size=CONTEXT_TEXT_SIZE,
             sfimage=SUBAGENT_ICON)
    if len(kids) > CONTEXT_MAX_AGENTS:
        emit(f"{'':<6} +{len(kids) - CONTEXT_MAX_AGENTS} more", color=MUTED,
             size=CONTEXT_TEXT_SIZE, sfimage=SUBAGENT_ICON)


def emit_agents(rows, by_parent, now_ts, cfg):
    """The LIVE AGENTS section: what's running right now, worst context first,
    with one advice line when anything is actually degrading."""
    orphans = (by_parent or {}).get(None, [])
    emit(f"LIVE AGENTS{' · ' + str(len(rows)) if rows else ''}",
         color=MUTED, sfimage="gauge", header=True)
    if not rows and not orphans:
        emit("Nothing running", color=MUTED, size=CONTEXT_TEXT_SIZE)
        sep()
        return
    for r in rows:
        emit_agent_row(r)
        if r["prov"] == "claude":
            emit_subagent_rows((by_parent or {}).get(r["key"], []), now_ts, cfg)
    if orphans:      # still running while their parent went idle or fell past the cap
        emit_subagent_rows(orphans, now_ts, cfg)
    at_risk = [r for r in rows if r["at_risk"]]
    if at_risk:
        worst = max(r["tier"] for r in at_risk)
        emit(ctx_risk_advice(worst), color=adaptive(band_color(worst)),
             size=CONTEXT_TEXT_SIZE, sfimage="exclamationmark.triangle.fill")
    sep()


def ctx_risk_advice(tier):
    """One actionable line — a warning that only says 'degraded' is just anxiety.
    No token figure here: the floors move with the window (see CTX_BAND_TABLE)."""
    if tier >= 3:
        return "Recall is unreliable this deep · /compact or start fresh"
    if tier >= 2:
        return "Quality drops well before the window fills · consider /compact"
    return "Nearing the window · compaction will drop detail"


def _which(cmd):
    return shutil.which(cmd)


def detect_claude():
    return bool(_which("claude")
                or glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"),
                             recursive=True)[:1]
                or os.path.isdir(os.path.expanduser("~/.claude")))


def detect_cursor():
    return bool(_which("agent") or _which("cursor-agent")
                or os.path.exists(os.path.expanduser("~/.cursor/cli-config.json"))
                or os.path.isdir(CURSOR_PROJECTS) or os.path.isdir(CURSOR_CHATS))


def active_providers(cfg):
    """Which provider sections to show, from config + auto-detection."""
    mode = cfg.get("providers") or "auto"
    if mode == "claude":
        return {"claude": True, "cursor": False}
    if mode == "cursor":
        return {"claude": False, "cursor": True}
    if mode == "both":
        return {"claude": True, "cursor": True}
    return {"claude": detect_claude(), "cursor": detect_cursor()}


# ─────────────────────────── update check ───────────────────────────
def version_tuple(s):
    """'0.6.0' -> (0, 6, 0); any non-numeric part becomes 0 so compares stay total."""
    out = []
    for p in (s or "").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def fetch_latest_version():
    """Latest *published* version, or None on any failure. Prefer the newest GitHub
    Release, so users are only nudged toward released builds — not a transient
    version bump that's landed on the default branch but isn't out yet. Falls back
    through the releases API and finally the plugin's <bitbar.version> header on the
    default branch. This is the only network call burnbar makes — a plain version GET
    to GitHub; no usage data ever leaves the machine. The first hit fetches a tiny
    version.txt release asset, so GitHub's own download_count doubles as an anonymous
    active-install tally (see README -> Updating)."""
    import urllib.request
    # 1. The latest release's version.txt asset: returns the bare version string,
    #    and the fetch ticks GitHub's download_count (the anonymous install counter).
    #    The regex guard rejects a 404 HTML body so a missing asset falls through.
    try:
        req = urllib.request.Request(
            VERSION_ASSET_URL, headers={"User-Agent": "burnbar"})
        with urllib.request.urlopen(req, timeout=3) as r:
            v = r.read(64).decode("utf-8", "replace").strip().lstrip("v")
        if re.match(r"^\d+(\.\d+)*$", v):
            return v
    except Exception:
        pass
    # 2. Releases API (covers a latest release published before version.txt existed).
    try:
        req = urllib.request.Request(
            f"{API_BASE}/releases/latest",
            headers={"User-Agent": "burnbar",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            tag = (json.load(r).get("tag_name") or "").lstrip("v")
        if tag:
            return tag
    except Exception:
        pass
    # 3. Fall back to the header on the default branch.
    try:
        req = urllib.request.Request(
            f"{RAW_BASE}/burnbar.30s.py",
            headers={"Range": "bytes=0-2047", "User-Agent": "burnbar"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return parse_version_header(r.read(2048).decode("utf-8", "replace"))
    except Exception:
        return None


def check_update(cfg, now_epoch):
    """Return the newer version string if one is available, else None. Refreshes
    from GitHub at most once a day (the result is cached in update.json); stamps the
    check time on every attempt so a failure still waits a full day before retrying.
    Off entirely when the user disables the check."""
    if cfg.get("update_check") != "on":
        return None
    state = {}
    try:
        with open(UPDATE_PATH) as f:
            state = json.load(f)
    except Exception:
        pass
    if now_epoch - state.get("checked", 0) >= UPDATE_INTERVAL:
        latest = fetch_latest_version()
        state = {"checked": now_epoch, "latest": latest or state.get("latest")}
        try:
            os.makedirs(os.path.dirname(UPDATE_PATH), exist_ok=True)
            with open(UPDATE_PATH, "w") as f:
                json.dump(state, f)
        except Exception:
            pass
    latest = state.get("latest")
    if latest and version_tuple(latest) > version_tuple(VERSION):
        return latest
    return None


def update_command():
    """How to pull the newest version, matched to how burnbar was installed: a git
    checkout updates in place; a curl install re-runs the installer (which re-fetches
    the scripts and refreshes SwiftBar)."""
    here = os.path.dirname(SELF)
    if os.path.isdir(os.path.join(here, ".git")):
        return f"git -C '{here}' pull --ff-only && open 'swiftbar://refreshallplugins'"
    return f"curl -fsSL '{RAW_BASE}/install.sh' | bash -s -- -y"


def self_update():
    """Run the in-place update, invoked as `burnbar.30s.py --self-update` in a Terminal
    window from the menu's Update row. The row can't hand SwiftBar the raw
    `curl … | bash` / `git … && open` string directly: for a terminal=true action
    SwiftBar word-splits the parameters, so `/bin/bash -lc` ends up with just the first
    token (`curl`/`git`) and the rest scatter onto the command line — curl then runs
    with no URL ("curl: try 'curl --help' …") and the update fails. Here Python builds
    the argv itself, so the shell receives the whole pipeline as one intact argument."""
    import subprocess
    cmd = update_command()
    print(f"burnbar {VERSION}: updating…\n\n    {cmd}\n", flush=True)
    rc = subprocess.call(["/bin/bash", "-lc", cmd])
    if rc == 0:
        print("\nburnbar: update complete.", flush=True)
        subprocess.call(["/usr/bin/open", "swiftbar://refreshallplugins"])
    else:
        print(f"\nburnbar: update failed (exit {rc}). "
              "See README → Updating to update by hand.", flush=True)
    try:                     # keep the Terminal window open so the result is readable
        input("\nPress Return to close this window. ")
    except (EOFError, KeyboardInterrupt):
        pass


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


def git_identity():
    """The user's own git author (email, then name) — used to count *their* commits."""
    for field in ("user.email", "user.name"):
        try:
            r = subprocess.run(["git", "config", "--get", field],
                               capture_output=True, text=True, timeout=3)
            val = r.stdout.strip()
            if val:
                return val
        except Exception:
            pass
    return ""


def commits_today(author, dirs, now_epoch, today_iso):
    """Today's commit count, recomputed at most every COMMITS_TTL seconds.

    The underlying scan is by far the most expensive thing in a refresh — a find(1)
    over every configured folder plus a `git log` per repo it turns up — and it runs
    on a 30s timer. Cached on (day, author) so a date rollover or an identity change
    invalidates it rather than serving yesterday's number."""
    key = f"{today_iso}|{author}"
    try:
        with open(COMMITS_PATH) as f:
            c = json.load(f)
        if c.get("key") == key and now_epoch - (c.get("at") or 0) < COMMITS_TTL:
            return int(c.get("n") or 0)
    except Exception:
        pass
    n = count_commits_today(author, dirs)
    try:
        os.makedirs(os.path.dirname(COMMITS_PATH), exist_ok=True)
        tmp = COMMITS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"key": key, "at": now_epoch, "n": n}, f)
        os.replace(tmp, COMMITS_PATH)
    except Exception:
        pass
    return n


def count_commits_today(author, dirs):
    """Count commits by `author` since midnight across `dirs`. No author -> 0."""
    if not author:
        return 0
    total = 0
    for base in dirs:
        if not os.path.isdir(base):
            continue
        try:
            find = subprocess.run(
                ["find", base, "-maxdepth", "5", "-type", "d", "-name", ".git"],
                capture_output=True, text=True, timeout=10)
            for git_dir in find.stdout.splitlines():
                repo = os.path.dirname(git_dir.strip())
                if not repo:
                    continue
                try:
                    r = subprocess.run(
                        ["git", "-C", repo, "log", "--oneline", "--since=midnight",
                         f"--author={author}"],
                        capture_output=True, text=True, timeout=5)
                    total += sum(1 for ln in r.stdout.splitlines() if ln.strip())
                except Exception:
                    pass
        except Exception:
            pass
    return total


# ─────────────────────── SwiftBar emit helpers ───────────────────────
def emit(text, sub=0, color=None, sfimage=None, size=13, refresh=False,
         action=None, args=None, open_path=None, header=False, terminal=False,
         image=None):
    prefix = "--" * sub
    params = [f"font={MONO} size={12 if header else size}"]
    params.append(f"color={color if color is not None else TH['text']}")
    if image:
        params.append(f"image={image}")
    if sfimage:
        params.append(f"sfimage={sfimage}")
    if refresh:
        params.append("refresh=true")
    if action:
        params.append(f"bash={action}")
        for i, a in enumerate(args or [], 1):
            params.append(f'param{i}="{a}"')
        params.append(f"terminal={'true' if terminal else 'false'}")
    if open_path:
        params.append("bash=/usr/bin/open")
        params.append(f'param1="{open_path}"')
        params.append("terminal=false")
    print(f"{prefix}{text} | {' '.join(params)}")


def sep(sub=0):
    print("--" * sub + "---")


def emit_update(latest):
    """A prominent 'a newer burnbar exists' row that updates in place when clicked
    (opens Terminal so the pull/install is visible)."""
    emit(f"Update to {latest} (on {VERSION})", color=adaptive(TH["grad"][0]),
         sfimage="arrow.down.circle.fill",
         action=SELF, args=["--self-update"], terminal=True)
    sep()


def emit_version_footer(update_avail):
    """A small version line pinned to the bottom of the menu, so users can see at a
    glance which burnbar they're running — and, when one's out, that an update is
    available (the row pulls it when clicked)."""
    sep()
    if update_avail:
        emit(f"burnbar v{VERSION} · v{update_avail} available",
             size=CONTEXT_TEXT_SIZE, color=adaptive(TH["grad"][0]),
             sfimage="arrow.down.circle",
             action=SELF, args=["--self-update"], terminal=True)
    else:
        emit(f"burnbar v{VERSION}", size=CONTEXT_TEXT_SIZE, color=MUTED)


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
            # Session header fields, captured from whichever early line carries
            # each (they're not all on line 1 — e.g. `cwd` first shows up on the
            # opening user event, after the `mode` line that has only sessionId).
            # cwd = true project; sid = own id for a main session / parent's for a
            # subagent; agentId + sidechain mark subagents; gitBranch differentiates
            # sibling sessions.
            if "cwd" not in meta and o.get("cwd"):
                meta["cwd"] = o.get("cwd")
                meta["branch"] = o.get("gitBranch")
            if "sid" not in meta and o.get("sessionId"):
                meta["sid"] = o.get("sessionId")
                meta["agent_id"] = o.get("agentId")
                meta["sidechain"] = bool(o.get("isSidechain"))
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


def limit_view(d, now_epoch, window_secs=None):
    """Resolve a rate-limit dict (five_hour / seven_day / opus) to what to display.

    While the last-known window is still open, show Anthropic's real numbers — the
    source of truth. Once its reset time has passed we haven't heard from Anthropic
    since (these only refresh on a Claude message), and the next window doesn't
    start until you send one — so show a fresh, full window at ~0%, flagged as an
    estimate. The moment a message lands, real numbers replace this automatically.

    Returns {"pc", "remaining", "reset", "estimated"}; remaining/reset are None when
    unknown (no reset time, or a fresh window whose clock hasn't started)."""
    pc = min(100, max(0, round(d.get("used_percentage") or 0)))
    reset = d.get("resets_at")
    if reset and now_epoch >= reset:
        return {"pc": 0, "remaining": window_secs, "reset": None, "estimated": True}
    return {"pc": pc, "remaining": (reset - now_epoch) if reset else None,
            "reset": reset, "estimated": False}


def emit_live_limits(usage, now_epoch, tz):
    """The real, cross-surface limits from Anthropic (via the statusLine bridge)."""
    rl = usage["rate_limits"]
    plan = f" · {PLAN_LABEL[PLAN]}" if PLAN else ""
    emit(f"LIMITS · live{plan}", color=MUTED, sfimage="speedometer", header=True)

    estimated_any = []

    def line(label, d, window=None):
        if not d or d.get("used_percentage") is None:
            return
        v = limit_view(d, now_epoch, window)
        pc = v["pc"]
        if v["estimated"]:
            estimated_any.append(True)
            # Fresh window: full block remaining, clock not started yet, no fixed
            # reset time to show.
            rs = f" · {fmt_dur(timedelta(seconds=v['remaining']))}" if v["remaining"] else ""
            rs += " · new block"
        elif v["reset"]:
            rs = f" · {fmt_dur(timedelta(seconds=v['remaining']))}"
            rs += f" ({datetime.fromtimestamp(v['reset'], tz):%H:%M})"
        else:
            rs = ""
        flag = " (!)" if d.get("status") in ("warning", "rejected", "exceeded") else ""
        pct = f"~{pc}%" if v["estimated"] else f"{pc}%"
        emit(f"{label:<6}{render_bar(pc / 100, BAR_CELLS)} {pct}{rs}{flag}",
             color=adaptive(color_for(pc)))

    line("5-hr", rl.get("five_hour"), BLOCK_HOURS * 3600)
    line("7-day", rl.get("seven_day"), 7 * 24 * 3600)
    if rl.get("opus"):
        line("Opus", rl.get("opus"))
    cap = usage.get("captured_at")
    if cap:
        age = now_epoch - cap
        note = f"as of {datetime.fromtimestamp(cap, tz):%H:%M}"
        if age > 120:
            note += f" · {fmt_dur(timedelta(seconds=age))} ago (idle)"
        emit(note, color=MUTED)
    if estimated_any:
        emit("~ estimated · send a message to confirm", color=MUTED)
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


def live_session_cwds():
    """How many Claude Code CLI sessions are open, and in which working dirs.

    A running `claude` process is the only reliable 'this session is open' signal
    (the transcript isn't held open and carries no pid); its cwd is the only clue
    to *which* session. Returns a (total, by_dir) pair so the caller can degrade
    gracefully rather than fall straight back to a wide time-based guess:
      (0, {})         nothing running
      (n, {cwd: k})   n sessions, located by dir — the precise, common case
      (n, None)       n sessions exist but we couldn't read their dirs (lsof blocked)
      (None, None)    couldn't even count processes
    """
    try:
        pids = subprocess.run(["/usr/bin/pgrep", "-x", "claude"],
                              capture_output=True, text=True, timeout=3).stdout.split()
    except Exception:
        return None, None
    if not pids:
        return 0, {}
    try:
        # One lsof for all pids (it's the slow call): each process emits its cwd
        # as an `n` line, so counting those tallies live sessions per dir.
        out = subprocess.run(["/usr/sbin/lsof", "-a", "-d", "cwd", "-Fn",
                              "-p", ",".join(pids)],
                             capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return len(pids), None
    counts = {}
    for line in out.splitlines():
        if line.startswith("n"):
            cwd = os.path.normpath(line[1:])
            counts[cwd] = counts.get(cwd, 0) + 1
    # lsof ran but surfaced no cwds (sandboxed / raced): still report the count.
    if not counts:
        return len(pids), None
    return len(pids), counts


def select_live_mains(cand, live_n, by_dir, now_ts):
    """Pick which main sessions to show as live, degrading by how much the process
    probe could tell us (see live_session_cwds for the (live_n, by_dir) shape):
      - by_dir known: per working dir, the N most-recently-active sessions where N
        is the live-process count there, so closed sessions can't linger ({} -> none);
      - by_dir None but live_n known: that many most-recent sessions, recency-gated;
      - both unknown: a short recency window only.
    `cand` is [(key, sv), ...] pre-sorted newest-first; returns the kept sublist."""
    def norm(sv):
        return os.path.normpath(sv.get("cwd") or "")
    if by_dir is not None:
        # A live process in this directory only proves *some* session here is open,
        # not which one — so the budget alone would hand a slot to whatever
        # transcript is next-newest, including one closed hours ago. Gate on
        # freshness too: a session nobody has touched in this long isn't the one
        # holding that process open.
        cut = now_ts - CLAUDE_CTX_STALE_MIN * 60
        budget = dict(by_dir)
        mains = []
        for k, v in cand:
            c = norm(v)
            if budget.get(c, 0) > 0 and v.get("mtime", 0) >= cut:
                mains.append((k, v))
                budget[c] -= 1
        return mains
    cut = now_ts - CONTEXT_ACTIVE_MIN * 60
    mains = [kv for kv in cand if kv[1].get("mtime", 0) >= cut]
    return mains[:live_n] if live_n else mains


# ─────────────────────── Cursor (offline) ───────────────────────
def _cursor_project_slug(path):
    """Users-foo-Dev-bar -> last path segment as a short label."""
    base = os.path.basename(path.rstrip("/"))
    if not base:
        return "?"
    # slug is abs path with / -> -
    pretty = base.replace("-", "/")
    return pretty.rstrip("/").split("/")[-1] or base


def gather_cursor(now, tz):
    """Offline Cursor session/activity rollup from local transcripts + chat meta.
    Live context fill comes from the statusLine multi-session registry (not
    transcripts — those have no token/context fields)."""
    today = datetime.now().astimezone(tz).date()
    sessions = []
    today_turns = 0
    today_sessions = set()
    meta_by_id = {}

    for meta_path in glob.glob(os.path.join(CURSOR_CHATS, "*", "*", "meta.json")):
        try:
            with open(meta_path) as f:
                m = json.load(f)
            cid = os.path.basename(os.path.dirname(meta_path))
            meta_by_id[cid] = m
        except Exception:
            continue

    for fp in glob.glob(os.path.join(CURSOR_PROJECTS, "*", "agent-transcripts",
                                     "*", "*.jsonl")):
        try:
            st = os.stat(fp)
        except OSError:
            continue
        sid = os.path.splitext(os.path.basename(fp))[0]
        parts = fp.split(os.sep)
        try:
            i = parts.index("projects")
            slug = parts[i + 1]
        except (ValueError, IndexError):
            slug = "?"
        users = assistants = 0
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"role"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    role = o.get("role")
                    if role == "user":
                        users += 1
                    elif role == "assistant":
                        assistants += 1
        except Exception:
            continue
        turns = users + assistants
        meta = meta_by_id.get(sid) or {}
        title = meta.get("title") or _cursor_project_slug(slug)
        cwd = meta.get("cwd")
        mtime = st.st_mtime
        local_day = datetime.fromtimestamp(mtime, tz).date()
        if local_day == today:
            today_turns += turns
            today_sessions.add(sid)
        sessions.append({
            "id": sid, "title": title, "cwd": cwd, "project": slug,
            "users": users, "assistants": assistants, "turns": turns,
            "mtime": mtime,
        })

    sessions.sort(key=lambda s: -s["mtime"])
    session_map = load_cursor_session_map()
    live = load_cursor_live()
    return {
        "sessions": sessions,
        "today_turns": today_turns,
        "today_sessions": len(today_sessions),
        "live": live,
        "session_map": session_map,
        "n_sessions": len(sessions),
    }


def live_cursor_procs():
    """How many Cursor CLI agent processes look open (best-effort)."""
    n = 0
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "cursor-agent|versions/.*/cursor-agent"],
            text=True, stderr=subprocess.DEVNULL)
        n = len([ln for ln in out.splitlines() if ln.strip()])
    except Exception:
        pass
    return n


# ─────────────────────────── main ───────────────────────────
def emit_menubar_title(cfg, usage, cursor_live, cursor_hottest, active_block,
                       cells, title_size, now_epoch):
    """Headline: Claude 5h % > hottest Cursor context % > set up / idle.
    Context rot wins the Cursor slot — show the fullest live window."""
    five = (usage or {}).get("rate_limits", {}).get("five_hour") if usage else None
    view = limit_view(five, now_epoch, BLOCK_HOURS * 3600) if five else None
    if usage and view:
        extra = ""
        if cfg["menubar_extra"] == "countdown" and view["remaining"] is not None:
            extra = f" · {fmt_dur(timedelta(seconds=view['remaining']))}"
        elif cfg["menubar_extra"] == "tokens" and active_block:
            extra = f" · {compact(weighted(active_block['tokens']))}"
        ap = view["pc"]
        pct = f"~{ap}%" if view["estimated"] else f"{ap}%"
        print(f"{render_bar(ap / 100, cells)} {pct}{extra} | "
              f"font={MONO} size={title_size} color={color_for(ap)}")
        return
    # Prefer the hottest live Cursor session (context-rot signal).
    pct = cursor_hottest
    if pct is None:
        pct = cursor_ctx_pct(cursor_live or {})
    if pct is not None:
        tag = " rot" if pct >= CONTEXT_CRIT_PCT else (
            " full" if pct >= CONTEXT_WARN_PCT else "")
        print(f"{render_bar(pct / 100, cells)} ctx {round(pct)}%{tag} | "
              f"font={MONO} size={title_size} color={color_for(pct)}")
        return
    label = "set up" if not usage and not cursor_live else "idle"
    print(f"{render_bar(0, cells)} {label} | "
          f"font={MONO} size={title_size} color={MUTED}")


def claude_stats(data, now, tz, today):
    """Everything the menu derives from the Claude transcript rollup, in one pass, so
    TODAY and the Stats submenu can never disagree about what "today" means."""
    window = timedelta(hours=BLOCK_HOURS)
    by_day = {date.fromisoformat(k): v for k, v in data["by_day"].items()}
    blocks = build_blocks(sorted(data["recent_records"], key=lambda r: r["ts"]))

    month, week_start = today.replace(day=1), today - timedelta(days=6)
    today_tok, today_msgs, today_models = new_tokens(), 0, {}
    today_hours, today_sessions = [0.0] * 24, set()
    for r in data["recent_records"]:
        lts = r["ts"].astimezone(tz)
        if lts.date() == today:
            add_tokens(today_tok, r["u"]); today_msgs += 1
            add_tokens(today_models.setdefault(r["model"], new_tokens()), r["u"])
            today_hours[lts.hour] += weighted_one(r["u"])
            today_sessions.add(r["session"])

    last = blocks[-1] if blocks else None
    return {
        "blocks": blocks, "by_day": by_day,
        "active": last if (last and now - last["start"] < window) else None,
        "week_w": sum(v[0] for d, v in by_day.items() if d >= week_start),
        "month_w": sum(v[0] for d, v in by_day.items() if d >= month),
        "today_tok": today_tok, "today_msgs": today_msgs,
        "today_models": today_models, "today_hours": today_hours,
        "today_sessions": today_sessions,
        "busiest_day": (max(by_day.items(), key=lambda kv: kv[1][0])
                        if by_day else (today, [0.0, 0])),
    }


def emit_limits(usage, now_epoch, tz):
    """Anthropic's real cross-surface limits, or the one-time nudge to wire them up."""
    if usage:
        emit_live_limits(usage, now_epoch, tz)
        return
    emit("LIMITS", color=MUTED, sfimage="speedometer", header=True)
    emit("Live usage not set up", color=MUTED)
    emit("Show real 5h / 7d limits", sub=1,
         open_path="https://github.com/dashpes/burnbar#live-usage-real-limits-not-estimates")
    sep()


def emit_today(cstats, cdata, commits, prov):
    """One TODAY block covering every provider — each used to print its own."""
    emit("TODAY", color=MUTED, sfimage="calendar", header=True)
    if cstats:
        emit(f"Claude  {compact(weighted(cstats['today_tok'])):>7} tok · "
             f"{plural(cstats['today_msgs'], 'msg')} · "
             f"{plural(len(cstats['today_sessions']), 'session')}")
    elif prov["claude"]:
        emit("Claude  no usage recorded yet", color=MUTED)
    if cdata:
        emit(f"Cursor  {cdata['today_turns']:>7} turns · "
             f"{plural(cdata['today_sessions'], 'session')}")
    if commits is not None:
        emit(f"Commits {commits:>7}" + ("  \U0001f525" if commits else ""),
             color=adaptive(TH["grad"][0]) if commits else None)
    if cstats and any(cstats["today_hours"]):
        emit(f"By hour {spark(cstats['today_hours'])}")
    sep()


def stats_submenu(data, cstats, cdata, today, tz, now_epoch):
    """Everything that isn't "what's running right now", one level down.

    This is the old Detailed view. It stopped being a mode you switch the whole
    menu into and became a submenu you open: the deep stats are always there, and
    the top level stays short whether or not you care about them."""
    # A provider being *enabled* isn't the same as it having anything to show — on a
    # fresh install cdata is a fully-populated dict of zeroes. Gate on real content,
    # or the menu grows a Stats row leading to empty headers.
    cursor_stats = cdata if (cdata and cdata.get("sessions")) else None
    if not cstats and not cursor_stats:
        return
    emit("Stats", sfimage="chart.bar.fill")

    if cstats:
        by_day, blocks = cstats["by_day"], cstats["blocks"]
        emit("LAST 7 DAYS", color=MUTED, sfimage="chart.bar.fill", header=True, sub=1)
        days = sorted(by_day.items(), reverse=True)[:7]
        daymax = max((v[0] for _, v in days), default=1) or 1
        for d, (tok, _msgs) in days:
            tag = "  ·today" if d == today else ""
            emit(f"{d.strftime('%a %m-%d')} {render_bar(tok/daymax, 8)} "
                 f"{compact(tok):>6}{tag}", sub=1)
        emit(f"Week total  {compact(cstats['week_w']):>8} tok", sub=1)
        emit(f"Month total {compact(cstats['month_w']):>8} tok", sub=1)
        sep(1)

        first_day = min(by_day) if by_day else today
        span_days = (today - first_day).days + 1
        all_tok, all_msgs = data["all_tok"], data["all_msgs"]
        emit("ALL TIME", color=MUTED, sfimage="clock.arrow.circlepath",
             header=True, sub=1)
        if PLAN:
            emit(f"Plan        {PLAN_LABEL[PLAN]:>8}", sub=1)
        emit(f"Total       {compact(weighted(all_tok)):>8} tok", sub=1)
        emit(f"Raw tokens  {compact(raw_total(all_tok)):>8}", sub=1)
        emit(f"Messages    {all_msgs:>8}", sub=1)
        emit(f"Sessions    {len(data['by_session']):>8}", sub=1)
        emit(f"Projects    {len(data['by_project']):>8}", sub=1)
        emit(f"Since       {first_day.strftime('%Y-%m-%d')} ({span_days}d)", sub=1)
        emit(f"Daily avg   {compact(weighted(all_tok)/max(1, span_days)):>8} tok", sub=1)
        emit(f"By hour  {spark(data['hour_profile'])}", sub=1)
        sep(1)

        emit("By model", sub=1)
        for m, mt in sorted(data["by_model"].items(), key=lambda kv: -weighted(kv[1])):
            emit(f"{m.replace('claude-', ''):<16}{compact(weighted(mt)):>8}", sub=2)
        emit("By project", sub=1)
        for p, pv in sorted(data["by_project"].items(),
                            key=lambda kv: -weighted(kv[1]["t"]))[:12]:
            emit(f"{p[:18]:<18}{compact(weighted(pv['t'])):>8}", sub=2)
        emit("Top sessions", sub=1)
        for _sid, sv in sorted(data["by_session"].items(),
                               key=lambda kv: -weighted(kv[1]["t"]))[:8]:
            when = (parse_ts(sv["last"]).astimezone(tz).strftime("%m-%d")
                    if sv.get("last") else "  -  ")
            emit(f"{sv['p'][:12]:<12} {when} {compact(weighted(sv['t'])):>7} "
                 f"{sv['m']:>4}m", sub=2)
        emit("Recent blocks", sub=1)
        for b in list(reversed(blocks))[:10]:
            s = b["start"].astimezone(tz).strftime("%m-%d %H:%M")
            live = " (live)" if (b is blocks[-1] and cstats["active"]) else ""
            emit(f"{s}  {compact(weighted(b['tokens'])):>7} · {b['msgs']:>3}m{live}",
                 sub=2, color=adaptive(TH["grad"][0]) if live else None)
        sep(1)

        emit("RECORDS", color=MUTED, sfimage="trophy.fill", header=True, sub=1)
        peak = data["peak"]
        if peak:
            pb_when = parse_ts(peak["start"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")
            emit(f"Peak block  {compact(peak['w']):>8} tok", sub=1)
            emit(f"            {pb_when}", color=MUTED, sub=1)
        bd, (bw, bm) = cstats["busiest_day"]
        emit(f"Busiest day {compact(bw):>8} tok", sub=1)
        emit(f"            {bd.strftime('%Y-%m-%d')} · {bm} msgs", color=MUTED, sub=1)

    if cursor_stats:
        if cstats:
            sep(1)
        emit("CURSOR", color=MUTED, sfimage=AGENT_ICON["cursor"], header=True, sub=1)
        emit(f"Sessions    {cursor_stats['n_sessions']:>8}", sub=1)
        procs = live_cursor_procs()
        if procs:
            emit(f"Live procs  {procs:>8}", sub=1)
        emit("Recent sessions", sub=1)
        for s in cursor_stats["sessions"][:10]:
            label = (s.get("title") or s["project"])[:22]
            emit(f"{label:<22} {s['turns']:>3}t · {fmt_age(now_epoch - s['mtime'])}",
                 sub=2)


def settings_submenu(cfg):
    """Every knob under one top-level row. These used to sit at the top level, one
    row per group — over a third of the menu was settings you had already set."""
    emit("Settings", sfimage="gearshape")

    def mark(active):
        # A checkmark on the selected row, blank (aligned) on the rest — cleaner
        # than [x]/[ ] boxes. A native menu can't persistently highlight a row's
        # background, so the checkmark is the selection cue.
        return "✓ " if active else "  "

    def group(title, key, options, image=None):
        emit(title, sub=1)
        for opt, lbl in options:
            emit(f"{mark(str(cfg[key]) == str(opt))}{lbl}", sub=2,
                 image=image(opt) if image else None, action=SELF,
                 args=["--set", f"{key}={opt}"], refresh=True)

    group("Providers", "providers",
          (("auto", "Auto-detect"), ("claude", "Claude only"),
           ("cursor", "Cursor only"), ("both", "Claude + Cursor")))
    # Preview each theme by a PNG swatch of its gradient stops — the thing that
    # actually differs between themes (their font colors are all near-white/black
    # for readability, so they can't tell the themes apart on their own).
    group("Theme", "theme", [(n, n.capitalize()) for n in THEMES],
          image=lambda n: theme_swatch(THEMES[n]["grad"]))
    group("Context window", "context_window",
          (("auto", "Auto-detect"), ("200k", "200K"), ("1m", "1M")))
    group("Menu-bar trailer", "menubar_extra",
          (("countdown", "Reset countdown"), ("tokens", "Token count"),
           ("none", "None")))
    group("Menu-bar width", "menubar_cells", [(w, str(w)) for w in (3, 5, 8, 10)])
    group("Commits today", "commits",
          (("on", "On (your git commits)"), ("off", "Off")))
    group("Check for updates", "update_check",
          (("on", "Daily (a version-only GET to GitHub)"), ("off", "Off")))

    sep(1)
    # Live-usage status: connected once the statusLine bridge has written real data.
    # Unset rows are clickable and lead straight to the setup docs.
    if load_usage():
        emit("Claude live · connected", sub=1, color=MUTED)
    else:
        emit("Claude live · set up…", sub=1, color=adaptive(TH["grad"][1]),
             open_path="https://github.com/dashpes/burnbar#live-usage-real-limits-not-estimates")
    if load_cursor_live():
        emit("Cursor live · connected", sub=1, color=MUTED)
    else:
        emit("Cursor live · set up…", sub=1, color=adaptive(TH["grad"][1]),
             open_path="https://github.com/dashpes/burnbar#cursor-cli")

    sep(1)
    emit("Open Claude transcripts", sub=1, sfimage="folder",
         open_path=os.path.expanduser("~/.claude/projects"))
    emit("Open Cursor projects", sub=1, sfimage="folder",
         open_path=CURSOR_PROJECTS)
    emit("Edit config file", sub=1, sfimage="doc.text", open_path=CONFIG_PATH)


def main():
    global TH, MUTED, PLAN
    cfg = load_config()
    TH = THEMES[cfg["theme"]]
    MUTED = TH["muted"]
    PLAN = read_plan()
    prov = active_providers(cfg)

    now = datetime.now(timezone.utc)
    now_epoch = now.timestamp()
    update_avail = check_update(cfg, now_epoch)
    tz = datetime.now().astimezone().tzinfo
    today = datetime.now().astimezone(tz).date()

    usage = load_usage() if prov["claude"] else None
    data = gather(now, tz) if prov["claude"] else None
    cdata = gather_cursor(now, tz) if prov["cursor"] else None
    cursor_rows = fresh_cursor_sessions((cdata or {}).get("session_map") or {},
                                        now_epoch) if prov["cursor"] else []

    cstats = claude_stats(data, now, tz, today) if (data and data["all_msgs"]) else None
    mains, by_parent = (claude_live_agents(data.get("by_session") or {}, now, cfg)
                        if data else ([], {}))
    agent_rows = unified_agent_rows(mains, cfg, cursor_rows, now_epoch)

    emit_menubar_title(cfg, usage, (cdata or {}).get("live"),
                       cursor_rows[0][2] if cursor_rows else None,
                       cstats["active"] if cstats else None,
                       cfg["menubar_cells"], cfg["title_size"], now_epoch)
    sep()

    if update_avail:
        emit_update(update_avail)

    if not prov["claude"] and not prov["cursor"]:
        emit("No CLI agents detected", color=MUTED)
        emit("Install Claude Code or Cursor CLI, then Refresh", color=MUTED)
        sep()
    else:
        emit_agents(agent_rows, by_parent, now_epoch, cfg)
        if prov["claude"]:
            emit_limits(usage, now_epoch, tz)
        commits = None
        if cfg["commits"] == "on":
            author = cfg.get("commit_author") or git_identity()
            dirs = [os.path.expanduser(p) for p in (cfg.get("commit_dirs") or [])]
            commits = commits_today(author, dirs or COMMIT_DIRS_DEFAULT,
                                    now_epoch, today.isoformat())
        emit_today(cstats, cdata, commits, prov)
        stats_submenu(data, cstats, cdata, today, tz, now_epoch)
        settings_submenu(cfg)

    sep()
    emit("Refresh", refresh=True, sfimage="arrow.clockwise")
    emit_version_footer(update_avail)


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
