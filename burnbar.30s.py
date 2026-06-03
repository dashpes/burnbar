#!/usr/bin/env python3
# <bitbar.title>burnbar</bitbar.title>
# <bitbar.version>0.2.0</bitbar.version>
# <bitbar.author>burnbar</bitbar.author>
# <bitbar.desc>Claude Code usage: 5-hour-block burn bar + full stats dropdown.</bitbar.desc>
# <bitbar.dependencies>python3</bitbar.dependencies>
# <swiftbar.hideAbout>false</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>false</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>false</swiftbar.hideDisablePlugin>
"""
burnbar — a SwiftBar/xbar plugin.

Menu bar:  a live progress bar for your current Claude Code 5-hour usage block.
Dropdown:  a full Stats-style panel — current block, today, last 7 days, all
           time, by model, by project, by hour, and records.

All from Claude Code's own local transcripts (~/.claude/projects/**/*.jsonl).
No ccusage, no API keys, no network, no pricing.
"""

import glob
import json
import os
from datetime import datetime, timedelta, timezone

# ─────────────────────────── config ───────────────────────────
BLOCK_HOURS = 5
BAR_CELLS = 10
PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
STATE_PATH = os.path.expanduser("~/.config/burnbar/state.json")
CACHE_READ_WEIGHT = 0.1          # cache reads are ~10x lighter; down-weight burn
PEAK_FLOOR = 300_000             # floor for the auto-calibrated 100% baseline
MONO = "Menlo"
FONT = f"font={MONO} size=13"
HEADER_FONT = f"font={MONO} size=12"

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


def merge(dst, src):
    for k in dst:
        dst[k] += src[k]


def weighted(t):
    return (t["input"] + t["output"] + t["cache_creation"]
            + t["cache_read"] * CACHE_READ_WEIGHT)


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


def pretty_project(dirname):
    p = dirname.replace("-", "/")
    home = os.path.expanduser("~")
    if p.startswith(home):
        p = "~" + p[len(home):]
    base = p.rstrip("/").split("/")[-1] or p
    return base


# ─────────────────────── SwiftBar emit helpers ───────────────────────
def emit(text, sub=0, color=None, sfimage=None, refresh=False,
         bash=None, param1=None, header=False):
    prefix = "--" * sub
    params = [HEADER_FONT if header else FONT]
    if color:
        params.append(f"color={color}")
    if sfimage:
        params.append(f"sfimage={sfimage}")
    if refresh:
        params.append("refresh=true")
    if bash:
        params.append(f"bash={bash}")
    if param1 is not None:
        params.append(f'param1="{param1}"')
        params.append("terminal=false")
    print(f"{prefix}{text} | {' '.join(params)}")


def sep(sub=0):
    print("--" * sub + "---")


# ─────────────────────────── data load ───────────────────────────
def load_records():
    """Deduped assistant turns: list of dict(ts, model, usage, project, session)."""
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
                    out.append({
                        "ts": parse_ts(ts), "model": msg.get("model", "?"),
                        "u": u, "project": project, "session": session,
                    })
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
    return bar if len(bar) >= cells else bar + "░" * (cells - len(bar))


def spark(values):
    ticks = "▁▂▃▄▅▆▇█"
    mx = max(values) if values else 0
    if mx <= 0:
        return "·" * len(values)
    out = []
    for v in values:
        if v <= 0:
            out.append("·")
        else:
            out.append(ticks[min(7, int(v / mx * 7 + 0.999))])
    return "".join(out)


def color_for(pct):
    if pct >= 90:
        return "#ff453a"
    if pct >= 70:
        return "#ff9f0a"
    if pct >= 40:
        return "#ffd60a"
    return "#30d158"


# ─────────────────────────── main ───────────────────────────
def main():
    now = datetime.now(timezone.utc)
    tz = datetime.now().astimezone().tzinfo
    today = datetime.now().astimezone(tz).date()
    window = timedelta(hours=BLOCK_HOURS)

    records = load_records()
    state = load_state()

    if not records:
        emit("⚡ burnbar")
        sep()
        emit("No Claude Code usage found yet")
        emit(f"Looked in {PROJECTS_GLOB}")
        emit("Refresh", refresh=True)
        return

    blocks = build_blocks(records)

    # ── aggregations ──
    all_tok, all_msgs = new_tokens(), len(records)
    by_model_all, by_project, by_session = {}, {}, {}
    by_day = {}            # date -> [weighted, msgs]
    hour_profile = [0.0] * 24
    today_tok, today_msgs, today_models = new_tokens(), 0, {}
    today_hours = [0.0] * 24
    today_sessions = set()
    month = today.replace(day=1)
    month_w = 0.0
    week_start = today - timedelta(days=6)
    week_w = 0.0

    for r in records:
        u, ts = r["u"], r["ts"]
        lts = ts.astimezone(tz)
        d, hr = lts.date(), lts.hour
        add_tokens(all_tok, u)
        add_tokens(by_model_all.setdefault(r["model"], new_tokens()), u)
        proj = by_project.setdefault(r["project"], {"t": new_tokens(), "m": 0,
                                                    "s": set()})
        add_tokens(proj["t"], u); proj["m"] += 1; proj["s"].add(r["session"])
        sess = by_session.setdefault(r["session"], {"t": new_tokens(), "m": 0,
                                                    "p": r["project"], "last": ts})
        add_tokens(sess["t"], u); sess["m"] += 1
        sess["last"] = max(sess["last"], ts)
        wt = weighted_one(u)
        agg = by_day.setdefault(d, [0.0, 0]); agg[0] += wt; agg[1] += 1
        hour_profile[hr] += wt
        if d == today:
            add_tokens(today_tok, u); today_msgs += 1
            add_tokens(today_models.setdefault(r["model"], new_tokens()), u)
            today_hours[hr] += wt
            today_sessions.add(r["session"])
        if d >= month:
            month_w += wt
        if d >= week_start:
            week_w += wt

    # ── active block + peak calibration ──
    last = blocks[-1]
    active = last if now - last["start"] < window else None
    completed = [b for b in blocks if b is not active]
    completed_w = [weighted(b["tokens"]) for b in completed]
    peak = max(completed_w + [state.get("peak", 0), PEAK_FLOOR])
    if completed_w:
        np = max(state.get("peak", 0), max(completed_w))
        if np != state.get("peak", 0):
            state["peak"] = np
            save_state(state)

    # ── records / peaks ──
    peak_block = max(blocks, key=lambda b: weighted(b["tokens"]))
    busiest_day = max(by_day.items(), key=lambda kv: kv[1][0])

    # ════════════════ MENU BAR TITLE ════════════════
    if active is None:
        print(f"{render_bar(0)} idle | {FONT} color=#8e8e93")
    else:
        burn = weighted(active["tokens"])
        frac = burn / peak if peak else 0
        pct = round(frac * 100)
        print(f"{render_bar(frac)} {pct}% · {compact(burn)} | "
              f"{FONT} color={color_for(pct)}")
    sep()

    # ════════════════ CURRENT BLOCK ════════════════
    emit("CURRENT 5-HOUR BLOCK", color="#8e8e93", sfimage="gauge.with.dots.needle.bottom.50percent", header=True)
    if active is None:
        emit(f"Idle · last activity {fmt_dur(now - last['last'])} ago",
             color="#8e8e93")
    else:
        burn = weighted(active["tokens"])
        pct = round(burn / peak * 100) if peak else 0
        end = active["start"] + window
        elapsed = now - active["start"]
        elapsed_min = max(1.0, elapsed.total_seconds() / 60)
        rate = burn / elapsed_min
        projected = rate * BLOCK_HOURS * 60
        s_l = active["start"].astimezone(tz).strftime("%H:%M")
        e_l = end.astimezone(tz).strftime("%H:%M")
        emit(f"{render_bar(burn/peak if peak else 0)}  {pct}% of peak")
        emit(f"Burn        {compact(burn):>8} tok")
        emit(f"Messages    {active['msgs']:>8}")
        emit(f"Window      {s_l}–{e_l}")
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
            emit(f"{m.replace('claude-',''):<16}{compact(weighted(mt)):>8}",
                 sub=1)
    sep()

    # ════════════════ TODAY ════════════════
    emit("TODAY", color="#8e8e93", sfimage="calendar", header=True)
    emit(f"Total       {compact(weighted(today_tok)):>8} tok")
    emit(f"Messages    {today_msgs:>8}")
    emit(f"Sessions    {len(today_sessions):>8}")
    busy_hr = today_hours.index(max(today_hours)) if any(today_hours) else None
    if busy_hr is not None:
        emit(f"Peak hour   {busy_hr:02d}:00")
    emit(f"By hour  {spark(today_hours)}")
    emit("By model")
    for m, mt in sorted(today_models.items(), key=lambda kv: -weighted(kv[1])):
        emit(f"{m.replace('claude-',''):<16}{compact(weighted(mt)):>8}", sub=1)
    sep()

    # ════════════════ LAST 7 DAYS ════════════════
    emit("LAST 7 DAYS", color="#8e8e93", sfimage="chart.bar.fill", header=True)
    days = sorted(by_day.items(), reverse=True)[:7]
    daymax = max((v[0] for _, v in days), default=1) or 1
    for d, (tok, msgs) in days:
        tag = "  ·today" if d == today else ""
        emit(f"{d.strftime('%a %m-%d')} {render_bar(tok/daymax, 8)} "
             f"{compact(tok):>6}{tag}")
    emit(f"Week total  {compact(week_w):>8} tok")
    emit(f"Month total {compact(month_w):>8} tok")
    sep()

    # ════════════════ ALL TIME ════════════════
    first = records[0]["ts"].astimezone(tz)
    span_days = (today - first.date()).days + 1
    emit("ALL TIME", color="#8e8e93", sfimage="clock.arrow.circlepath", header=True)
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
    top_sess = sorted(by_session.items(), key=lambda kv: -weighted(kv[1]["t"]))[:8]
    for sid, sv in top_sess:
        when = sv["last"].astimezone(tz).strftime("%m-%d")
        emit(f"{sv['p'][:12]:<12} {when} {compact(weighted(sv['t'])):>7} "
             f"{sv['m']:>4}m", sub=1)
    sep()

    # ════════════════ RECORDS ════════════════
    emit("RECORDS", color="#8e8e93", sfimage="trophy.fill", header=True)
    pb_when = peak_block["start"].astimezone(tz).strftime("%Y-%m-%d %H:%M")
    emit(f"Peak block  {compact(weighted(peak_block['tokens'])):>8} tok")
    emit(f"            {pb_when}", color="#8e8e93")
    bd, (bw, bm) = busiest_day
    emit(f"Busiest day {compact(bw):>8} tok")
    emit(f"            {bd.strftime('%Y-%m-%d')} · {bm} msgs", color="#8e8e93")
    emit(f"Calibrated  {compact(peak):>8} tok = 100%")
    sep()

    # ════════════════ RECENT BLOCKS ════════════════
    emit("Recent blocks")
    for b in list(reversed(blocks))[:10]:
        s = b["start"].astimezone(tz).strftime("%m-%d %H:%M")
        live = " ● live" if (b is blocks[-1] and active is not None) else ""
        emit(f"{s}  {compact(weighted(b['tokens'])):>7} · {b['msgs']:>3}m{live}",
             sub=1, color="#30d158" if live else None)
    sep()

    emit("Refresh", refresh=True, sfimage="arrow.clockwise")
    emit("Open transcripts folder", bash="/usr/bin/open",
         param1=os.path.expanduser("~/.claude/projects"),
         sfimage="folder")


def weighted_one(u):
    return ((u.get("input_tokens", 0) or 0)
            + (u.get("output_tokens", 0) or 0)
            + (u.get("cache_creation_input_tokens", 0) or 0)
            + (u.get("cache_read_input_tokens", 0) or 0) * CACHE_READ_WEIGHT)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("⚡ burnbar !")
        print("---")
        print(f"Error: {e} | {FONT} color=#ff453a")
        import traceback
        for ln in traceback.format_exc().splitlines():
            print(f"{ln} | {FONT} size=10")
        print("Refresh | refresh=true")
