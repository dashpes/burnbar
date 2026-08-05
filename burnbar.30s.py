#!/usr/bin/env python3
# <bitbar.title>burnbar</bitbar.title>
# <bitbar.version>1.8.2</bitbar.version>
# <bitbar.author>burnbar</bitbar.author>
# <bitbar.desc>AI coding agent usage: live burn bar + context-rot tracking + stats.</bitbar.desc>
# <bitbar.dependencies>python3</bitbar.dependencies>
# <swiftbar.hideAbout>false</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>false</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>false</swiftbar.hideDisablePlugin>
"""
burnbar — a SwiftBar/xbar plugin: one menu for every AI coding agent you run.

Not a Claude Code tool that grew a Cursor tab. Providers are entries in the
PROVIDERS registry below, and everything that varies per agent — detection,
settings row, menu label, icon, live rows, today line — reads from that entry.
Adding an agent is a table entry plus its own gather/rows functions; no existing
function grows another branch.

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
import contextlib
import glob
import io
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
# Provider identity is spelled out on every row (AGENT_LABEL, derived from the
# PROVIDERS registry). An icon alone can't carry it: a monochrome glyph tinted to
# the row's rot colour reads as decoration, and when every visible row is the same
# provider there's no contrast to decode it against.
SUBAGENT_ICON = "arrow.turn.down.right"   # subagents nest under their parent row

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
    "providers_mode": "auto",    # auto (show whatever is installed) | manual
    # Per-provider "on"/"off" keys (provider_claude, …) are added to DEFAULTS from
    # the PROVIDERS registry once it's defined, so a new agent needs no edit here.
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
    migrate_providers(cfg)
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


def context_window(model, peak_ctx, mode, reported=None):
    """Pick a session's context-window size.

    `reported` is what Claude Code itself said the window is (via the statusLine
    bridge) and always wins outside an explicit pin, because it is the only source
    that can be right: transcripts never record a size, and the model name can't
    be mapped to one — Claude Code offers "claude-opus-5" (200K) and
    "claude-opus-5[1m]" (1M) as separate models of the same underlying one. It
    also follows a /model switch on its own.

    Without a bridge there is nothing to go on but the name, so 'auto' guesses
    conservatively: the [1m] suffix marks a 1M variant, and a session whose own
    high-water mark has passed 200K has demonstrably got more than that. Anything
    else is assumed 200K. Deliberately not "opus means 1M" — a models.dev-style
    table lists what a model can do, not what Claude Code hands this session, and
    guessing high hides rot, while guessing low only warns early."""
    if mode == "200k":
        return CTX_200K
    if mode == "1m":
        return CTX_1M
    if reported:
        return int(reported)
    name = (model or "").lower()
    return CTX_1M if ("[1m]" in name or peak_ctx > CTX_200K) else CTX_200K


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


def claude_registry(now_epoch, stale_min=CLAUDE_CTX_STALE_MIN):
    """{session_id: entry} for sessions the bridge refreshed within stale_min, or
    None if there's no registry to answer. Each entry carries the heartbeat plus
    whatever Claude Code reported — notably the exact context window size."""
    try:
        with open(CLAUDE_SESSIONS_PATH) as f:
            store = json.load(f)
        sessions = store.get("sessions") if isinstance(store, dict) else None
    except Exception:
        return None
    if not isinstance(sessions, dict):
        return None
    cut = now_epoch - stale_min * 60
    return {sid: v for sid, v in sessions.items()
            if isinstance(v, dict) and (v.get("captured_at") or 0) >= cut}


def claude_registry_sessions(now_epoch, stale_min=CLAUDE_CTX_STALE_MIN):
    """[(session_id, last_seen)] from the registry, newest first — or None.

    None means "unknown, go guess" (the pgrep/lsof path). It is deliberately not
    the same as an empty list, which means "the bridge is running and reports
    nothing open"."""
    reg = claude_registry(now_epoch, stale_min)
    if reg is None:
        return None
    rows = [(sid, v.get("captured_at") or 0) for sid, v in reg.items()]
    rows.sort(key=lambda r: -r[1])
    return rows


def claude_proc_count():
    """How many Claude Code CLI processes are running, or None if unreadable.

    Just the count — the caller only needs to know how many sessions exist, not
    where, so this skips the lsof that live_session_cwds pays for."""
    return proc_count("claude")


def reconcile_live(rows, proc_count, now_epoch, fresh_min=CONTEXT_LIVE_MIN):
    """Which of `rows` [(id, last_seen), newest first] are sessions still open.

    Every agent poses the same problem, and the two available signals fail in
    opposite directions: per-session activity says *which* session did something
    but not when one dies (closing a tab looks exactly like idling), while a
    process count says *how many* are open but not which.

    So cross-check. Anything active within fresh_min is live outright — recent
    activity is stronger evidence than any process scan, and process enumeration
    can be restricted (under a sandbox pgrep can miss even the CLI hosting the
    caller), so it must never veto a session we just heard from. Quieter entries
    need the count to corroborate them, newest first. proc_count None means the
    process table was unreadable — corroborate nothing, trust the activity."""
    fresh_cut = now_epoch - fresh_min * 60
    beating = [sid for sid, ts in rows if ts >= fresh_cut]
    quiet = [sid for sid, ts in rows if ts < fresh_cut]
    if proc_count is None:
        return set(beating) | set(quiet)
    return set(beating) | set(quiet[:max(0, proc_count - len(beating))])


def proc_count(pattern, exact=True):
    """How many processes match, or None if the process table can't be read."""
    try:
        args = ["/usr/bin/pgrep", "-x" if exact else "-f", pattern]
        out = subprocess.run(args, capture_output=True, text=True, timeout=3).stdout
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
    return reconcile_live(reg, claude_proc_count(), now_epoch)


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


def claude_agent_rows(pdata, cfg, now_epoch):
    """Claude's contribution to the agent list: one row per live main session."""
    mode = cfg["context_window"]
    reg = (pdata or {}).get("registry") or {}
    rows = []
    for key, sv in (pdata or {}).get("mains", []):
        entry = reg.get(sv.get("sid")) or reg.get(key) or {}
        win = context_window(sv.get("model"), sv.get("peak_ctx", 0), mode,
                             entry.get("context_window_size"))
        used = sv.get("last_ctx", 0)
        rows.append({"prov": "claude", "key": key, "label": ctx_session_label(sv),
                     "tok": used, "win": win,
                     "pct": min(100.0, 100.0 * used / win) if win else 0.0,
                     "age": now_epoch - sv.get("mtime", now_epoch)})
    return rows


def cursor_agent_rows(pdata, cfg, now_epoch):
    """Cursor's contribution: live context fill per session, from the bridge."""
    del cfg                      # Cursor reports its own window size
    rows = []
    for sid, entry, pct in ((pdata or {}).get("ctx_rows") or [])[:CONTEXT_MAX_ROWS]:
        cw = entry.get("context_window") or {}
        rows.append({"prov": "cursor", "key": sid,
                     "label": cursor_session_label(entry),
                     "tok": cursor_ctx_tokens(entry) or 0,
                     "win": cw.get("context_window_size"), "pct": pct,
                     "age": now_epoch - (entry.get("captured_at") or now_epoch)})
    return rows


def unified_agent_rows(rows):
    """Band and rank every provider's rows into one list — worst context first.

    burnbar used to print this three times over (a top-of-menu risk strip, then
    again inside each provider's own section). One list, ranked the way the risk
    itself ranks — rot tier, then absolute tokens, then window fill — says the
    same thing once, and a new provider joins it by returning rows of the same
    shape rather than earning a section of its own."""
    rows = list(rows)
    for r in rows:
        # pct is None when the agent doesn't report a window size (a local model
        # nobody publishes a limit for). The rot band still works — it reads
        # absolute tokens — but nothing may claim a percentage it doesn't have.
        pct = r.get("pct")
        r["tier"], r["tags"] = ctx_tags(r["tok"], pct or 0, r["win"])
        r["at_risk"] = r["tier"] >= CTX_RISK_TIER or (pct or 0) >= CONTEXT_WARN_PCT
    rows.sort(key=lambda r: (-r["tier"], -r["tok"], -(r.get("pct") or 0)))
    return rows


def emit_agent_row(r):
    """One agent: name, window fill, and the two signals that can disagree —
    bar *length* is how full the window is, bar *colour* is the rot band."""
    # Window labels vary in width ("1M" vs "256K"), so pad to the widest — otherwise
    # the age/tag tail ragged-edges down the column.
    pct = r.get("pct")
    win = f"/{ctx_label(r['win']):<4}" if r.get("win") else " " * 5
    # No window means no fill to draw and no percentage to claim: a dashed bar and
    # an em dash say "unknown", where 0% would say "plenty of room left".
    bar = render_bar(pct / 100, 6) if pct is not None else "┄" * 6
    pct_s = f"{round(pct):>3}%" if pct is not None else "   —"
    when = "live" if r["age"] < CONTEXT_LIVE_MIN * 60 else fmt_age(r["age"])
    tail = (" · " + " · ".join(r["tags"])) if r["tags"] else ""
    if r.get("note"):
        when = f"{r['note']} · {when}"
    emit(f"{AGENT_LABEL.get(r['prov'], '?'):<8} "
         f"{ellipsis(r['label'], CONTEXT_NAME_W):<{CONTEXT_NAME_W}} "
         f"{bar} {pct_s} "
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
        emit(f"{'':<8} {ellipsis(name, CONTEXT_NAME_W):<{CONTEXT_NAME_W}} "
             f"{render_bar(frac, 6)} {pct:>3}% "
             f"{compact(used):>5}/{ctx_label(win):<4} · {when}{tail}",
             color=adaptive(band_color(tier)), size=CONTEXT_TEXT_SIZE,
             sfimage=SUBAGENT_ICON)
    if len(kids) > CONTEXT_MAX_AGENTS:
        emit(f"{'':<8} +{len(kids) - CONTEXT_MAX_AGENTS} more", color=MUTED,
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


# ─────────────────────── the provider registry ───────────────────────
# One entry per AI coding agent. Everything that varies per agent lives here, so
# adding one is an entry plus the functions it names — not a new branch in
# active_providers, the settings menu, the agent list and the today block.
#
#   key      config/registry id, and the key active_providers() returns
#   label    what the menu calls it (spelled out on every agent row)
#   icon     SF Symbol; reinforces the label, and marks the row's provider
#   detect   () -> bool: is this agent installed on this machine?
#   gather   (now, tz, now_epoch) -> provider data, or None. Handed back to the
#            hooks below; burnbar never looks inside it.
#   rows     (pdata, cfg, now_epoch) -> [raw agent row], one per live session.
#            Raw = {prov, key, label, tok, win, pct, age}; banding, ranking and
#            rendering are shared (see unified_agent_rows).
#   today    (pdata) -> str, the provider's line in TODAY, or None
#   stats    (pdata, now_epoch) -> None, optional; emits into the Stats submenu
#   setup    docs anchor for the "not set up" nudge in Settings
#
# Order here is display order for ties and for the settings list.
PROVIDERS = (
    {"key": "claude", "label": "Claude", "icon": "bolt.fill",
     "detect": lambda: detect_claude(),
     "gather": lambda now, tz, now_epoch: gather_claude(now, tz),
     "rows": lambda pdata, cfg, now_epoch: claude_agent_rows(pdata, cfg, now_epoch),
     "today": lambda pdata: claude_today_line(pdata),
     "stats": None,          # Claude's stats block is emitted inline (it's the big one)
     "setup": "#live-usage-real-limits-not-estimates"},
    {"key": "cursor", "label": "Cursor", "icon": "cursorarrow",
     "detect": lambda: detect_cursor(),
     "gather": lambda now, tz, now_epoch: gather_cursor(now, tz),
     "rows": lambda pdata, cfg, now_epoch: cursor_agent_rows(pdata, cfg, now_epoch),
     "today": lambda pdata: cursor_today_line(pdata),
     "stats": lambda pdata, now_epoch: emit_cursor_stats(pdata, now_epoch),
     "setup": "#cursor-cli"},
    {"key": "opencode", "label": "OpenCode",
     "icon": "chevron.left.forwardslash.chevron.right",
     "detect": lambda: detect_opencode(),
     "gather": lambda now, tz, now_epoch: gather_opencode(now, tz),
     "rows": lambda pdata, cfg, now_epoch: opencode_agent_rows(pdata, cfg, now_epoch),
     "today": lambda pdata: opencode_today_line(pdata),
     "stats": lambda pdata, now_epoch: emit_opencode_stats(pdata, now_epoch),
     "setup": "#adding-a-provider"},
)
# "Is this agent's statusLine bridge wired up?" — separate from the table so the
# table stays a plain description of each agent.
# OpenCode needs no bridge — it records everything in its own SQLite store, so it
# is "connected" whenever that store exists.
BRIDGE_CONNECTED = {"claude": lambda: bool(load_usage()),
                    "cursor": lambda: bool(load_cursor_live()),
                    "opencode": lambda: os.path.exists(OPENCODE_DB)}
PROVIDER_KEYS = tuple(p["key"] for p in PROVIDERS)
PROVIDER_BY_KEY = {p["key"]: p for p in PROVIDERS}
# Provider identity on an agent row is carried by the spelled-out label; the icon
# reinforces it. Both are derived so a new provider needs no edit here either.
AGENT_LABEL = {p["key"]: p["label"] for p in PROVIDERS}
AGENT_ICON = {p["key"]: p["icon"] for p in PROVIDERS}
# Per-provider visibility keys, defaulted on (they only apply in manual mode).
for _p in PROVIDERS:
    DEFAULTS[f"provider_{_p['key']}"] = "on"

# Values the pre-1.7 single "providers" key could hold, and what each meant. Kept
# so an existing config keeps behaving the same after the upgrade.
LEGACY_PROVIDERS = {
    "auto": None,                                   # -> auto mode, nothing pinned
    "claude": {"claude": "on", "cursor": "off"},
    "cursor": {"claude": "off", "cursor": "on"},
    "both": {"claude": "on", "cursor": "on"},
}


def migrate_providers(cfg):
    """Normalise provider settings, converting the pre-1.7 `providers` enum.

    That key was a fixed enum (auto/claude/cursor/both) — it could not express a
    third agent, which is what forced this registry. Translate it once and drop
    it; per-provider keys scale to as many agents as we add."""
    legacy = cfg.pop("providers", None)
    if isinstance(legacy, str) and legacy in LEGACY_PROVIDERS:
        pins = LEGACY_PROVIDERS[legacy]
        cfg["providers_mode"] = "manual" if pins else "auto"
        # The legacy values were exhaustive ("claude" meant *only* Claude), so any
        # agent added since must default off, not on. Otherwise upgrading would
        # silently switch on agents the user had deliberately excluded.
        for key in PROVIDER_KEYS:
            if pins is not None:
                cfg[f"provider_{key}"] = pins.get(key, "off")
    if cfg.get("providers_mode") not in ("auto", "manual"):
        cfg["providers_mode"] = "auto"
    for key in PROVIDER_KEYS:
        if cfg.get(f"provider_{key}") not in ("on", "off"):
            cfg[f"provider_{key}"] = "on"


def active_providers(cfg):
    """Which agents to show: everything installed, or exactly what you've pinned."""
    if cfg.get("providers_mode") == "manual":
        return {k: cfg.get(f"provider_{k}") == "on" for k in PROVIDER_KEYS}
    return {p["key"]: bool(p["detect"]()) for p in PROVIDERS}


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
        # Live context fill per open session — what the agent list is built from.
        "ctx_rows": fresh_cursor_sessions(session_map, datetime.now(tz).timestamp()),
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


# ─────────────────────── OpenCode (SQLite) ───────────────────────
# OpenCode keeps everything in one SQLite database rather than JSON transcripts,
# which is why its context numbers are easy to miss when you go looking for files.
# Everything burnbar needs is in there:
#   session.*                 title, directory, model, time_updated (ms)
#   message.data (JSON)       per-turn {tokens:{total,input,output,cache:{read,write}}}
# The context figure OpenCode shows in its own sidebar is the newest *completed*
# assistant turn's `tokens.total` — input + output + cache — so burnbar reports the
# same number rather than a second, subtly different one.
OPENCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
OPENCODE_CONFIG = os.path.expanduser("~/.config/opencode")
OPENCODE_STATE = os.path.expanduser("~/.local/state/opencode")
# models.dev mirror OpenCode caches: provider -> models -> id -> limit.context.
OPENCODE_MODELS = os.path.expanduser("~/.cache/opencode/models.json")
OPENCODE_STALE_MIN = 30          # sessions idle longer than this aren't "live"
OPENCODE_MAX_SESSIONS = 20       # newest N sessions are enough to find live ones
# Resolved context windows, cached because the models.dev mirror is ~3.5MB (a 20ms
# parse) and a model's limit effectively never moves.
WINDOW_CACHE_PATH = os.path.expanduser("~/.config/burnbar/windows.json")
WINDOW_CACHE_TTL = 6 * 3600
# A *miss* must expire quickly. Ollama's /api/ps lists only models it currently has
# loaded, so switching to a model that hasn't been used yet legitimately misses —
# and caching that for hours would leave the window unknown long after the model
# loads. Successes are stable and keep the long TTL.
WINDOW_MISS_TTL = 120
OLLAMA_PS_URL = "http://127.0.0.1:11434/api/ps"


def detect_opencode():
    return bool(_which("opencode") or os.path.exists(OPENCODE_DB)
                or os.path.isdir(OPENCODE_CONFIG) or os.path.isdir(OPENCODE_STATE))


def _window_cache():
    try:
        with open(WINDOW_CACHE_PATH) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _window_cache_put(cache, key, win, now_epoch):
    cache[key] = {"win": win, "at": now_epoch}
    try:
        os.makedirs(os.path.dirname(WINDOW_CACHE_PATH), exist_ok=True)
        tmp = WINDOW_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, WINDOW_CACHE_PATH)
    except Exception:
        pass


def models_dev_limit(provider_id, model_id):
    """Context limit from OpenCode's models.dev mirror, or None if it isn't listed."""
    try:
        with open(OPENCODE_MODELS) as f:
            db = json.load(f)
        limit = ((db.get(provider_id) or {}).get("models") or {}) \
            .get(model_id, {}).get("limit") or {}
        win = limit.get("context")
        return int(win) if win else None
    except Exception:
        return None


def ollama_context_length(model_id):
    """The context window Ollama actually loaded a model with, or None.

    Local models aren't in models.dev, so neither burnbar nor OpenCode itself can
    look their limit up — OpenCode's sidebar just reports "0% used". Ollama knows,
    because it loaded the model, and answers on the loopback interface. This is a
    request to a daemon already running on this machine; nothing leaves it, and it
    is skipped entirely when Ollama isn't listening."""
    import urllib.request
    try:
        with urllib.request.urlopen(OLLAMA_PS_URL, timeout=1) as r:
            models = (json.load(r) or {}).get("models") or []
    except Exception:
        return None
    for m in models:
        if model_id in (m.get("name"), m.get("model")):
            win = m.get("context_length")
            return int(win) if win else None
    return None


def opencode_window(provider_id, model_id, now_epoch):
    """Context window for an OpenCode model: hosted models from the models.dev
    mirror, local ones from whichever runtime loaded them. None when unknown —
    which is honest, and the rot bands work on absolute tokens regardless."""
    key = f"{provider_id}/{model_id}"
    cache = _window_cache()
    hit = cache.get(key) if isinstance(cache.get(key), dict) else {}
    known = hit.get("win")
    ttl = WINDOW_CACHE_TTL if known else WINDOW_MISS_TTL
    if hit and now_epoch - (hit.get("at") or 0) < ttl:
        return known
    win = models_dev_limit(provider_id, model_id)
    if not win and "ollama" in (provider_id or "").lower():
        win = ollama_context_length(model_id)
    # A local runtime unloads idle models, and then it can no longer say what
    # window they had. That's a gap in the answer, not a change to it — the model
    # reloads with the same context — so a previously resolved size sticks rather
    # than collapsing the row to "unknown" every time the model goes cold.
    if not win and known:
        return known
    _window_cache_put(cache, key, win, now_epoch)
    return win


def opencode_turn_context(data):
    """A turn's context occupancy, matching what OpenCode's own sidebar shows.

    Prefers the `total` OpenCode records; falls back to summing the parts for
    older rows that predate it. Returns 0 for a turn still in flight (all zeros),
    so the caller keeps walking back to the last completed one."""
    tok = data.get("tokens") or {}
    if tok.get("total"):
        return int(tok["total"])
    cache = tok.get("cache") or {}
    return int((tok.get("input") or 0) + (tok.get("output") or 0)
               + (cache.get("read") or 0) + (cache.get("write") or 0))


def gather_opencode(now, tz):
    """Read OpenCode's SQLite store: live sessions + today's activity.

    Opened read-only through a file: URI so a refresh can never lock or write the
    database the running agent owns."""
    import sqlite3
    now_epoch = now.timestamp()
    today = datetime.now(tz).date()
    out = {"sessions": [], "today_turns": 0, "today_sessions": 0, "n_sessions": 0}
    if not os.path.exists(OPENCODE_DB):
        return out
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
    except Exception:
        return out
    try:
        rows = con.execute(
            "SELECT id, title, directory, model, time_updated FROM session "
            "WHERE time_archived IS NULL ORDER BY time_updated DESC LIMIT ?",
            (OPENCODE_MAX_SESSIONS,)).fetchall()
        total = con.execute(
            "SELECT COUNT(*) FROM session WHERE time_archived IS NULL").fetchone()[0]
        sessions, today_sessions, today_turns = [], set(), 0
        for r in rows:
            # session.model is the *currently selected* model, not the one that ran
            # the last turn: OpenCode rewrites it on every session update. That is
            # the right one to size the window by — switching to a smaller model
            # should immediately show the context you're already carrying as over
            # capacity, which is exactly the moment you need telling.
            try:
                model = json.loads(r["model"] or "{}")
            except Exception:
                model = {}
            model_id, provider_id = model.get("id") or "", model.get("providerID") or ""
            # Newest *completed* assistant turn: an in-flight one reports all zeros.
            ctx, turns = 0, 0
            for m in con.execute(
                    "SELECT data, time_created FROM message WHERE session_id = ? "
                    "ORDER BY time_created DESC LIMIT 40", (r["id"],)):
                try:
                    data = json.loads(m["data"])
                except Exception:
                    continue
                if datetime.fromtimestamp((m["time_created"] or 0) / 1000.0,
                                          tz).date() == today:
                    turns += 1
                if not ctx and data.get("role") == "assistant":
                    ctx = opencode_turn_context(data)
            updated = (r["time_updated"] or 0) / 1000.0     # OpenCode stores ms
            if turns:
                today_turns += turns
                today_sessions.add(r["id"])
            sessions.append({
                "id": r["id"], "title": r["title"] or "",
                "directory": r["directory"] or "", "model": model_id,
                "provider": provider_id, "tok": ctx, "updated": updated,
                "win": opencode_window(provider_id, model_id, now_epoch) if ctx else None,
            })
        out.update(sessions=sessions, today_turns=today_turns,
                   today_sessions=len(today_sessions), n_sessions=total)
    except Exception:
        pass
    finally:
        con.close()
    return out


def opencode_model_label(model_id):
    """'qwen3.6:latest' -> 'qwen3.6'; 'claude-sonnet-5' -> 'sonnet'.

    OpenCode is the agent people switch models in most, and between very different
    windows (a 32K local model and a 1M hosted one), so the row names which one is
    actually in play rather than leaving the window size to imply it."""
    m = (model_id or "").split("/")[-1]
    m = m.split(":")[0]
    if m.startswith("claude-"):
        return model_short(m)
    return ellipsis(m, 14)


def opencode_session_label(sess):
    if sess.get("title"):
        return sess["title"]
    d = sess.get("directory") or ""
    return os.path.basename(d.rstrip("/")) or d or sess.get("id") or "session"


def opencode_agent_rows(pdata, cfg, now_epoch):
    """OpenCode's live sessions, cross-checked against running processes the same
    way every other agent's are (see reconcile_live)."""
    del cfg
    sessions = (pdata or {}).get("sessions") or []
    cut = now_epoch - OPENCODE_STALE_MIN * 60
    recent = [s for s in sessions if s.get("updated", 0) >= cut and s.get("tok")]
    recent.sort(key=lambda s: -s["updated"])
    live = reconcile_live([(s["id"], s["updated"]) for s in recent],
                          proc_count("opencode", exact=False), now_epoch)
    rows = []
    for sess in recent[:CONTEXT_MAX_ROWS]:
        if sess["id"] not in live:
            continue
        win = sess.get("win")
        rows.append({"prov": "opencode", "key": sess["id"],
                     "label": opencode_session_label(sess),
                     "tok": sess["tok"], "win": win,
                     "pct": min(100.0, 100.0 * sess["tok"] / win) if win else None,
                     "age": now_epoch - sess["updated"],
                     "note": opencode_model_label(sess.get("model"))})
    return rows


def opencode_today_line(pdata):
    if not pdata:
        return None
    return (f"{pdata['today_turns']:>7} turns · "
            f"{plural(pdata['today_sessions'], 'session')}")


def emit_opencode_stats(pdata, now_epoch):
    if not (pdata and pdata.get("sessions")):
        return
    emit("OPENCODE", color=MUTED, sfimage=AGENT_ICON["opencode"], header=True, sub=1)
    emit(f"Sessions    {pdata['n_sessions']:>8}", sub=1)
    emit("Recent sessions", sub=1)
    for sess in pdata["sessions"][:10]:
        win = f"/{ctx_label(sess['win'])}" if sess.get("win") else ""
        emit(f"{ellipsis(opencode_session_label(sess), 22):<22} "
             f"{compact(sess['tok']):>5}{win} · {fmt_age(now_epoch - sess['updated'])}",
             sub=2)


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


def gather_claude(now, tz):
    """Claude's bundle: transcript rollup, live limits, and which sessions are open."""
    usage = load_usage()
    data = gather(now, tz)
    mains, by_parent = claude_live_agents(data.get("by_session") or {}, now,
                                          load_config())
    return {"data": data, "usage": usage, "mains": mains, "by_parent": by_parent,
            "registry": claude_registry(now.timestamp()) or {}}


def claude_today_line(pdata):
    cstats = (pdata or {}).get("stats")
    if not cstats:
        return "no usage recorded yet"
    return (f"{compact(weighted(cstats['today_tok'])):>7} tok · "
            f"{plural(cstats['today_msgs'], 'msg')} · "
            f"{plural(len(cstats['today_sessions']), 'session')}")


def cursor_today_line(pdata):
    if not pdata:
        return None
    return (f"{pdata['today_turns']:>7} turns · "
            f"{plural(pdata['today_sessions'], 'session')}")


def emit_cursor_stats(pdata, now_epoch):
    """Cursor's block in the Stats submenu. Silent when there's nothing to show."""
    if not (pdata and pdata.get("sessions")):
        return
    emit("CURSOR", color=MUTED, sfimage=AGENT_ICON["cursor"], header=True, sub=1)
    emit(f"Sessions    {pdata['n_sessions']:>8}", sub=1)
    procs = live_cursor_procs()
    if procs:
        emit(f"Live procs  {procs:>8}", sub=1)
    emit("Recent sessions", sub=1)
    for sess in pdata["sessions"][:10]:
        label = (sess.get("title") or sess["project"])[:22]
        emit(f"{label:<22} {sess['turns']:>3}t · "
             f"{fmt_age(now_epoch - sess['mtime'])}", sub=2)


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


def emit_today(bundles, commits, prov):
    """One TODAY block covering every provider — each used to print its own."""
    emit("TODAY", color=MUTED, sfimage="calendar", header=True)
    width = max(len(p["label"]) for p in PROVIDERS)
    for p in PROVIDERS:
        if not prov.get(p["key"]):
            continue
        line = p["today"](bundles.get(p["key"]))
        if line:
            emit(f"{p['label']:<{width}}  {line}",
                 color=MUTED if "no usage" in line else None)
    if commits is not None:
        emit(f"{'Commits':<{width}}  {commits:>7}" + ("  \U0001f525" if commits else ""),
             color=adaptive(TH["grad"][0]) if commits else None)
    cstats = (bundles.get("claude") or {}).get("stats")
    if cstats and any(cstats["today_hours"]):
        emit(f"{'By hour':<{width}}  {spark(cstats['today_hours'])}")
    sep()


def provider_stat_blocks(bundles, now_epoch):
    """Each provider's Stats section, pre-rendered, skipping the ones with nothing.

    Whether a provider has anything to say is only known by emitting it — the hook
    decides what to skip — so render into a buffer and keep the non-empty ones.
    That way the Stats row only appears when opening it would show something."""
    blocks = []
    for p in PROVIDERS:
        if not p["stats"]:
            continue
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            p["stats"](bundles.get(p["key"]), now_epoch)
        if buf.getvalue().strip():
            blocks.append(buf.getvalue())
    return blocks


def stats_submenu(data, cstats, bundles, today, tz, now_epoch):
    """Everything that isn't "what's running right now", one level down.

    This is the old Detailed view. It stopped being a mode you switch the whole
    menu into and became a submenu you open: the deep stats are always there, and
    the top level stays short whether or not you care about them."""
    # A provider being *enabled* isn't the same as it having anything to show — on a
    # fresh install a bundle is a fully-populated dict of zeroes.
    provider_blocks = provider_stat_blocks(bundles, now_epoch)
    if not cstats and not provider_blocks:
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

    printed = bool(cstats)
    for block in provider_blocks:
        if printed:
            sep(1)
        sys.stdout.write(block)
        printed = True


def settings_submenu(cfg, prov):
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

    # Providers are checkboxes, not a fixed enum: the enum could name every
    # combination of two agents, but not of five. Auto-detect is the master;
    # clicking any agent pins the current set and flips that one, so switching to
    # manual can never silently switch on an agent you don't have.
    auto = cfg.get("providers_mode") != "manual"
    emit("Providers", sub=1)
    emit(f"{mark(auto)}Auto-detect installed agents", sub=2, action=SELF,
         args=["--set", "providers_mode=auto"], refresh=True)
    sep(2)
    for p in PROVIDERS:
        on = bool(prov.get(p["key"]))
        pins = ["--set", "providers_mode=manual"]
        for q in PROVIDERS:
            state = bool(prov.get(q["key"]))
            if q["key"] == p["key"]:
                state = not state
            pins.append(f"provider_{q['key']}={'on' if state else 'off'}")
        emit(f"{mark(on)}{p['label']}", sub=2, sfimage=p["icon"],
             action=SELF, args=pins, refresh=True)
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
    # Bridge status, one row per agent: connected once its statusLine hook has
    # written real data. Unset rows are clickable and lead to the setup docs.
    for p in PROVIDERS:
        wired = BRIDGE_CONNECTED.get(p["key"], lambda: False)()
        if wired:
            emit(f"{p['label']} live · connected", sub=1, color=MUTED)
        else:
            emit(f"{p['label']} live · set up…", sub=1,
                 color=adaptive(TH["grad"][1]),
                 open_path=f"https://github.com/dashpes/burnbar{p['setup']}")

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

    # Every active provider gathers into its own opaque bundle, then contributes
    # rows to the shared agent list. Nothing in this loop names a specific agent.
    bundles, raw_rows = {}, []
    for p in PROVIDERS:
        if not prov.get(p["key"]):
            continue
        bundles[p["key"]] = p["gather"](now, tz, now_epoch)
        raw_rows.extend(p["rows"](bundles[p["key"]], cfg, now_epoch) or [])
    agent_rows = unified_agent_rows(raw_rows)

    # Claude keeps two things the generic pipeline doesn't model: real plan limits,
    # and the deep stats panel. Both are opt-in extras, not part of the interface.
    claude = bundles.get("claude") or {}
    data, usage = claude.get("data"), claude.get("usage")
    cstats = claude_stats(data, now, tz, today) if (data and data["all_msgs"]) else None
    claude["stats"] = cstats

    cursor_rows = (bundles.get("cursor") or {}).get("ctx_rows") or []
    emit_menubar_title(cfg, usage, (bundles.get("cursor") or {}).get("live"),
                       cursor_rows[0][2] if cursor_rows else None,
                       cstats["active"] if cstats else None,
                       cfg["menubar_cells"], cfg["title_size"], now_epoch)
    sep()

    if update_avail:
        emit_update(update_avail)

    if not any(prov.values()):
        emit("No AI coding agents detected", color=MUTED)
        emit("Install one, then Refresh", color=MUTED)
        sep()
    else:
        emit_agents(agent_rows, claude.get("by_parent") or {}, now_epoch, cfg)
        if prov.get("claude"):
            emit_limits(usage, now_epoch, tz)
        commits = None
        if cfg["commits"] == "on":
            author = cfg.get("commit_author") or git_identity()
            dirs = [os.path.expanduser(pth) for pth in (cfg.get("commit_dirs") or [])]
            commits = commits_today(author, dirs or COMMIT_DIRS_DEFAULT,
                                    now_epoch, today.isoformat())
        emit_today(bundles, commits, prov)
        stats_submenu(data, cstats, bundles, today, tz, now_epoch)
        settings_submenu(cfg, prov)

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
