#!/usr/bin/env python3
# <bitbar.title>burnbar</bitbar.title>
# <bitbar.version>0.3.0</bitbar.version>
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
Dropdown:  a Stats-style panel (compact or full), with an in-menu Settings
           submenu for theme / view / bar width — no JSON editing required.

All from Claude Code's own local transcripts (~/.claude/projects/**/*.jsonl).
No ccusage, no API keys, no network, no pricing.

Settings are stored in ~/.config/burnbar/config.json and changed by clicking
items in the ⚙ Settings submenu (which re-invoke this script with --set).
"""

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# ─────────────────────────── fixed config ───────────────────────────
BLOCK_HOURS = 5
BAR_CELLS = 10                   # bar width inside the dropdown
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
STATE_PATH = os.path.expanduser("~/.config/burnbar/state.json")
CONFIG_PATH = os.path.expanduser("~/.config/burnbar/config.json")
USAGE_PATH = os.path.expanduser("~/.config/burnbar/usage.json")  # live rate_limits
CACHE_READ_WEIGHT = 0.1          # cache reads are ~10x lighter; down-weight burn
PEAK_FLOOR = 300_000             # floor for the auto-calibrated 100% baseline
MONO = "Menlo"
PRIMARY = "#1d1d1f,#f5f5f7"      # adaptive (light,dark) high-contrast body text
MUTED = "#8e8e93"                # section headers / secondary notes
SELF = os.path.realpath(__file__)

# ── user-configurable defaults (overridden by config.json) ──
DEFAULTS = {
    "view": "default",           # "default" | "compact"
    "theme": "default",          # see THEMES
    "menubar_cells": 5,          # bar width in the menu bar
    "title_size": 11,            # menu-bar font size
    "show_menubar_tokens": False,
}

# ── themes: a full palette, so the whole dropdown gets tinted ──
#   grad  = (low, mid, high, max) bar gradient + alert accents (by % burn)
#   text  = body rows, adaptive "light,dark" so it stays readable in both menus
#   muted = section headers + secondary notes
THEMES = {
    "default":   {"grad": ("#30d158", "#ffd60a", "#ff9f0a", "#ff453a"),
                  "text": "#1d1d1f,#f5f5f7", "muted": "#8e8e93"},
    "mono":      {"grad": ("#8e8e93", "#aeaeb2", "#d1d1d6", "#f5f5f7"),
                  "text": "#1d1d1f,#f5f5f7", "muted": "#8e8e93"},
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
    if cfg.get("view") not in ("default", "compact"):
        cfg["view"] = "default"
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
    if key == "show_menubar_tokens":
        return str(value).lower() in ("1", "true", "yes", "on")
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


def new_tokens():
    return {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}


def add_tokens(dst, u):
    dst["input"] += u.get("input_tokens", 0) or 0
    dst["output"] += u.get("output_tokens", 0) or 0
    dst["cache_creation"] += u.get("cache_creation_input_tokens", 0) or 0
    dst["cache_read"] += u.get("cache_read_input_tokens", 0) or 0


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
def load_records():
    seen = set()
    out = []
    for fp in glob.glob(PROJECTS_GLOB, recursive=True):
        project = pretty_project(os.path.basename(os.path.dirname(fp)))
        session = os.path.splitext(os.path.basename(fp))[0]
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
                    out.append({"ts": parse_ts(ts), "model": msg.get("model", "?"),
                                "u": u, "project": project, "session": session})
                except Exception:
                    continue
    out.sort(key=lambda r: r["ts"])
    return out


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
        flag = " ⚠" if d.get("status") in ("warning", "rejected", "exceeded") else ""
        emit(f"{label:<6}{render_bar(pc / 100, BAR_CELLS)} {pc}%{rs}{flag}",
             color=color_for(pc))

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


def color_for(pct):
    g = TH["grad"]
    if pct >= 90:
        return g[3]
    if pct >= 70:
        return g[2]
    if pct >= 40:
        return g[1]
    return g[0]


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

    records = load_records()
    state = load_state()
    usage = load_usage()

    if not records:
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

    blocks = build_blocks(records)

    # ── aggregations ──
    all_tok, all_msgs = new_tokens(), len(records)
    by_model_all, by_project, by_session = {}, {}, {}
    by_day = {}
    hour_profile = [0.0] * 24
    today_tok, today_msgs, today_models = new_tokens(), 0, {}
    today_hours = [0.0] * 24
    today_sessions = set()
    month = today.replace(day=1)
    month_w = week_w = 0.0
    week_start = today - timedelta(days=6)

    for r in records:
        u, ts = r["u"], r["ts"]
        lts = ts.astimezone(tz)
        d, hr = lts.date(), lts.hour
        add_tokens(all_tok, u)
        add_tokens(by_model_all.setdefault(r["model"], new_tokens()), u)
        proj = by_project.setdefault(r["project"],
                                     {"t": new_tokens(), "m": 0, "s": set()})
        add_tokens(proj["t"], u); proj["m"] += 1; proj["s"].add(r["session"])
        ssn = by_session.setdefault(r["session"],
                                    {"t": new_tokens(), "m": 0, "p": r["project"],
                                     "last": ts})
        add_tokens(ssn["t"], u); ssn["m"] += 1; ssn["last"] = max(ssn["last"], ts)
        wt = weighted_one(u)
        agg = by_day.setdefault(d, [0.0, 0]); agg[0] += wt; agg[1] += 1
        hour_profile[hr] += wt
        if d == today:
            add_tokens(today_tok, u); today_msgs += 1
            add_tokens(today_models.setdefault(r["model"], new_tokens()), u)
            today_hours[hr] += wt; today_sessions.add(r["session"])
        if d >= month:
            month_w += wt
        if d >= week_start:
            week_w += wt

    # ── active block + peak calibration ──
    last = blocks[-1]
    active = last if now - last["start"] < window else None
    all_w = [weighted(b["tokens"]) for b in blocks]
    completed_w = [weighted(b["tokens"]) for b in blocks if b is not active]
    if completed_w:
        np = max(state.get("peak", 0), max(completed_w))
        if np != state.get("peak", 0):
            state["peak"] = np
            save_state(state)
    peak = max(all_w + [state.get("peak", 0), PEAK_FLOOR])

    peak_block = max(blocks, key=lambda b: weighted(b["tokens"]))
    busiest_day = max(by_day.items(), key=lambda kv: kv[1][0])

    # ════════════════ MENU BAR TITLE ════════════════
    # Prefer the REAL 5-hour % from Anthropic (live, cross-surface) when present;
    # otherwise fall back to the token-based estimate vs auto-calibrated peak.
    burn_now = weighted(active["tokens"]) if active else 0
    extra = f" · {compact(burn_now)}" if cfg["show_menubar_tokens"] else ""
    if usage:
        ap = min(100, max(0, round(usage["rate_limits"]["five_hour"].get("used_percentage") or 0)))
        print(f"{render_bar(ap / 100, cells)} {ap}%{extra} | "
              f"font={MONO} size={title_size} color={color_for(ap)}")
    elif active is None:
        print(f"{render_bar(0, cells)} idle | "
              f"font={MONO} size={title_size} color={MUTED}")
    else:
        frac = min(1.0, burn_now / peak) if peak else 0
        pct = min(100, round(frac * 100))
        print(f"{render_bar(frac, cells)} {pct}%{extra} | "
              f"font={MONO} size={title_size} color={color_for(pct)}")
    sep()

    # ════════════════ LIVE LIMITS (real, from Anthropic) ════════════════
    if usage:
        emit_live_limits(usage, now_epoch, tz)

    # ════════════════ CURRENT BLOCK (token detail, this Mac) ════════════════
    emit("5-HOUR TOKENS · this Mac" if usage else "CURRENT 5-HOUR BLOCK", color=MUTED,
         sfimage="gauge.with.dots.needle.bottom.50percent", header=True)
    if active is None:
        emit(f"Idle · last activity {fmt_dur(now - last['last'])} ago", color=MUTED)
    else:
        burn = weighted(active["tokens"])
        pct = min(100, round(burn / peak * 100)) if peak else 0
        end = active["start"] + window
        elapsed_min = max(1.0, (now - active["start"]).total_seconds() / 60)
        rate = burn / elapsed_min
        projected = rate * BLOCK_HOURS * 60
        s_l = active["start"].astimezone(tz).strftime("%H:%M")
        e_l = end.astimezone(tz).strftime("%H:%M")
        if compact_view:
            if not usage:
                emit(f"{render_bar(burn/peak if peak else 0, BAR_CELLS)}  "
                     f"{pct}% · {compact(burn)} tok", color=color_for(pct))
                emit(f"Resets {fmt_dur(end - now)} · {compact(rate)}/min",
                     color=color_for(pct))
            else:
                emit(f"{compact(burn)} tok · {compact(rate)}/min")
        else:
            if not usage:
                emit(f"{render_bar(burn/peak if peak else 0, BAR_CELLS)}  {pct}% of peak",
                     color=color_for(pct))
            emit(f"Burn        {compact(burn):>8} tok")
            emit(f"Messages    {active['msgs']:>8}")
            emit(f"Window      {s_l}–{e_l}")
            if not usage:
                emit(f"Resets in   {fmt_dur(end - now):>8}", color=color_for(pct))
            emit(f"Rate        {compact(rate):>8} tok/min")
            emit(f"Projected   {compact(projected):>8} tok @ block end",
                 color=color_for(round(projected / peak * 100) if peak else 0))
        emit("Breakdown")
        for lbl, k in [("Input", "input"), ("Output", "output"),
                       ("Cache write", "cache_creation"),
                       ("Cache read", "cache_read")]:
            emit(f"{lbl:<12}{compact(active['tokens'][k]):>8}", sub=1)
        emit("By model")
        for m, mt in sorted(active["by_model"].items(),
                            key=lambda kv: -weighted(kv[1])):
            emit(f"{m.replace('claude-',''):<16}{compact(weighted(mt)):>8}", sub=1)
    sep()

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
        emit(f"Peak block   {compact(weighted(peak_block['tokens'])):>8} tok", sub=1)
        bd, (bw, _bm) = busiest_day
        emit(f"Busiest day  {compact(bw):>8} tok", sub=1)
        sep()
    else:
        emit_full_sections(blocks, by_day, by_model_all, by_project, by_session,
                           hour_profile, all_tok, all_msgs, records, today, tz,
                           week_w, month_w, peak, peak_block, busiest_day, active, now)

    settings_menu(cfg)
    sep()
    emit("Refresh", refresh=True, sfimage="arrow.clockwise")
    emit("Open transcripts folder", sfimage="folder",
         open_path=os.path.expanduser("~/.claude/projects"))


def emit_full_sections(blocks, by_day, by_model_all, by_project, by_session,
                       hour_profile, all_tok, all_msgs, records, today, tz,
                       week_w, month_w, peak, peak_block, busiest_day, active, now):
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
    first = records[0]["ts"].astimezone(tz)
    span_days = (today - first.date()).days + 1
    emit("ALL TIME", color=MUTED, sfimage="clock.arrow.circlepath", header=True)
    if PLAN:
        emit(f"Plan        {PLAN_LABEL[PLAN]:>8}")
    emit(f"Total       {compact(weighted(all_tok)):>8} tok")
    emit(f"Raw tokens  {compact(raw_total(all_tok)):>8}")
    emit(f"Messages    {all_msgs:>8}")
    emit(f"Sessions    {len(by_session):>8}")
    emit(f"Projects    {len(by_project):>8}")
    emit(f"Since       {first.strftime('%Y-%m-%d')} ({span_days}d)")
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
        when = sv["last"].astimezone(tz).strftime("%m-%d")
        emit(f"{sv['p'][:12]:<12} {when} {compact(weighted(sv['t'])):>7} "
             f"{sv['m']:>4}m", sub=1)
    sep()

    # RECORDS
    emit("RECORDS", color=MUTED, sfimage="trophy.fill", header=True)
    pb_when = peak_block["start"].astimezone(tz).strftime("%Y-%m-%d %H:%M")
    emit(f"Peak block  {compact(weighted(peak_block['tokens'])):>8} tok")
    emit(f"            {pb_when}", color=MUTED)
    bd, (bw, bm) = busiest_day
    emit(f"Busiest day {compact(bw):>8} tok")
    emit(f"            {bd.strftime('%Y-%m-%d')} · {bm} msgs", color=MUTED)
    emit(f"Calibrated  {compact(peak):>8} tok = 100%")
    sep()

    # RECENT BLOCKS
    emit("Recent blocks")
    for b in list(reversed(blocks))[:10]:
        s = b["start"].astimezone(tz).strftime("%m-%d %H:%M")
        live = " ● live" if (b is blocks[-1] and active is not None) else ""
        emit(f"{s}  {compact(weighted(b['tokens'])):>7} · {b['msgs']:>3}m{live}",
             sub=1, color=TH["grad"][0] if live else None)
    sep()


def settings_menu(cfg):
    sep()
    emit("⚙ Settings", color=MUTED, sfimage="gearshape", header=True)

    def mark(active):
        return "● " if active else "○ "

    emit("View")
    emit(f"{mark(cfg['view']=='default')}Default", sub=1, action=SELF,
         args=["--set", "view=default"], refresh=True)
    emit(f"{mark(cfg['view']=='compact')}Compact", sub=1, action=SELF,
         args=["--set", "view=compact"], refresh=True)

    emit("Theme")
    for name in THEMES:
        emit(f"{mark(cfg['theme']==name)}{name.capitalize()}", sub=1, action=SELF,
             args=["--set", f"theme={name}"], refresh=True)

    emit("Menu-bar tokens")
    emit(f"{mark(cfg['show_menubar_tokens'])}Show", sub=1, action=SELF,
         args=["--set", "show_menubar_tokens=true"], refresh=True)
    emit(f"{mark(not cfg['show_menubar_tokens'])}Hide", sub=1, action=SELF,
         args=["--set", "show_menubar_tokens=false"], refresh=True)

    emit("Menu-bar width")
    for w in (3, 5, 8, 10):
        emit(f"{mark(cfg['menubar_cells']==w)}{w}", sub=1, action=SELF,
             args=["--set", f"menubar_cells={w}"], refresh=True)

    # Live-usage status: on when the statusLine bridge has written real data.
    live = load_usage()
    if live:
        emit("Live usage  ● connected", color=MUTED)
    else:
        emit("Live usage  ○ not set up", color=MUTED)
        emit("Set up live limits (real %, reset times)…", sub=1,
             open_path="https://github.com/dashpes/burnbar#live-usage")

    emit("Edit config file…", sub=0, open_path=CONFIG_PATH)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        handle_cli(sys.argv[1:])
        sys.exit(0)
    try:
        main()
    except Exception as e:
        print("⚡ burnbar !")
        print("---")
        print(f"Error: {e} | font={MONO} size=13 color=#ff453a")
        import traceback
        for ln in traceback.format_exc().splitlines():
            print(f"{ln} | font={MONO} size=10")
        print("Refresh | refresh=true")
