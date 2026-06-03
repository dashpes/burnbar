# burnbar — project writeup

Source material for a website / blog post. Plain prose, lift whatever you want.

---

## The one-liner

**burnbar** is a macOS menu-bar gauge for Claude Code usage. It shows your real
5-hour and weekly usage limits — the same numbers the `/usage` command shows —
as a little bar that fills up, with a live countdown to reset, plus a drop-down
of token stats. It runs as a single Python script inside
[SwiftBar](https://swiftbar.app), reads only local files, and sends nothing
anywhere.

```
███▉░ 37% · 3h09m
```

---

## The problem

If you use Claude Code heavily, you live with two questions: *how close am I to
my 5-hour limit?* and *when does it reset?* The answers exist — the `/usage`
slash command shows them — but only when you stop and type it, inside the
terminal. There was no at-a-glance, always-visible version.

The obvious tool in this space, `ccusage`, doesn't actually answer those
questions. It reads Claude Code's local transcript logs and **estimates** cost
and token volume after the fact. Useful, but it's a rear-view mirror, not a fuel
gauge — it has no idea what your real limit is or when it resets, and it can't
see usage from other devices or from claude.ai on the web.

burnbar set out to be the fuel gauge.

---

## What it shows

**In the menu bar:** a fill bar of your current 5-hour usage percentage, colored
green → yellow → orange → red as it climbs, followed by a reset countdown
(`3h09m`). One glance tells you how much room you have and how long until it
clears.

**In the drop-down (click it):** a stats panel with monochrome SF Symbol section
headers:

- **Usage limits (live)** — 5-hour, 7-day, and Opus limits, each a fill bar with
  the exact percentage used and time to reset.
- **Today / Last 7 days / All time** — token totals, message and session counts,
  an hourly-activity sparkline, breakdowns by model, by project, and your top
  sessions.
- **Records** — your busiest 5-hour block and busiest day ever.
- **Settings** — change the theme, switch between the compact and detailed layouts,
  and tweak the menu-bar bar, all by clicking; no config files to edit.

---

## The interesting part: where the real numbers come from

This is the bit worth writing about.

The real usage limits are **not** stored in any local file that a tool like
`ccusage` reads. Claude Code's transcript logs carry a `rateLimits` field, but
it's always `null` on disk. So how does `/usage` know?

Poking at the Claude Code binary (it's a ~200 MB native build) and dumping its
embedded strings turned up the answer — a schema and a worked example:

```
"rate_limits": {            // Claude.ai subscription usage limits.
    "five_hour":  { "used_percentage": ..., "resets_at": <unix epoch> },
    "seven_day":  { ... },
    "opus":       { ... }
}
```

The API returns this `rate_limits` object in its responses, and Claude Code
re-exposes it in exactly one place: the **statusLine** — the little status string
at the bottom of the terminal. Claude Code lets you configure the statusLine as a
shell command and feeds it a JSON blob on stdin every time it updates the UI. The
binary even shipped an example statusLine that read
`.rate_limits.five_hour.used_percentage` from that stdin.

That's the hook. burnbar ships a tiny bridge script you set as your statusLine
command. On every update Claude Code hands it the JSON; the bridge:

1. writes the `rate_limits` object to `~/.config/burnbar/usage.json`, and
2. prints a compact status line back so your terminal gets a nice readout too.

The menu-bar plugin then just reads `usage.json`.

Two things make this better than estimating:

- **The numbers are real and exact.** `used_percentage` is your actual limit
  usage; `resets_at` is the actual reset timestamp. No modeling, no guessing.
- **They're cross-surface.** Anthropic computes the limits server-side, so the
  values already fold in claude.ai on the web, Claude Code, and every machine you
  use. The bridge only has to fire on *one* machine to capture the global truth.

The plugin also reads your plan tier (`Pro` / `Max 5x` / `Max 20x`) straight from
`~/.claude.json`, so it labels the limits with your actual plan.

When the bridge isn't wired up yet, the menu bar simply says `set up` and the
drop-down points you at the install step.

---

## The token stats (the secondary layer)

The live limits answer "how much room is left." The token stats answer "what did
I actually spend it on." Those come from Claude Code's transcript logs in
`~/.claude/projects/**/*.jsonl`.

For every assistant turn, burnbar reads the token usage (`input`, `output`,
`cache_creation`, `cache_read`), de-duplicates by message id + request id, and
rolls everything up by day, model, project, and session. It groups turns into
rolling 5-hour blocks — the same shape as the usage window — to surface things
like your busiest-ever block.

Cache-read tokens are down-weighted to 10% when computing "effective" tokens,
because cached reads are far cheaper than fresh tokens; counting them at face
value would make the numbers a cache-reuse meter rather than a real-effort meter.

---

## Keeping it lean

burnbar is meant to live in the background forever, so resource use mattered.

- **It's not a daemon.** SwiftBar spawns the script, it runs for tens of
  milliseconds, prints its menu, and exits. Between refreshes it uses no RAM and
  no CPU at all.
- **It creates no growing files.** Its three files — `usage.json`, `config.json`,
  `cache.json` — are all fixed-size or overwritten. The only thing that grows is
  Claude Code's own transcript folder, which Claude Code owns.
- **An incremental cache keeps each refresh cheap.** Transcript files don't change
  once a session ends, so burnbar caches each file's rolled-up totals keyed by its
  size and modification time. A refresh only re-reads files that are new, changed,
  or from the last few days; everything older is served straight from the cache.
  Deleted files are pruned automatically, and the cache isn't even rewritten to
  disk unless something actually changed. The result: a refresh stays in the tens
  of milliseconds no matter how many years of history pile up.

---

## Design choices worth a sentence

- **Unicode, not pixels.** The bars and sparklines are block characters
  (`███▉░`, `▁▂▃▅█`). It keeps the whole thing a single text-printing script
  instead of a native app, and it reads fine in a menu.
- **Themes that stay readable.** Six color themes (Default, Mono, Nord, Dracula,
  Solarized, Matrix). The trick is that the menu bar (dark background) and the
  drop-down (light background in light mode) need opposite treatments, so colors
  are adaptive: vibrant in the menu bar, darkened for the light drop-down panel.
- **Settings without a settings window.** Every option is a clickable menu item
  that re-invokes the script to write a small JSON config and refresh — so you
  get a real settings experience with zero native UI code.
- **Compact vs detailed.** A one-click toggle between the default compact view —
  just the live limits, today, and a "More stats" submenu — and the full
  detailed stats panel.

---

## Privacy

Everything stays on the machine. burnbar makes no network calls. It never
touches your auth token — Claude Code already holds that; the bridge only ever
reads the JSON Claude Code hands it and writes a local file.

---

## Stack & footprint

- **One Python file** for the plugin, one for the statusLine bridge. Standard
  library only — no dependencies, no build step.
- Runs inside **SwiftBar** (a free, open-source menu-bar host;
  `brew install --cask swiftbar`). Also works with xbar.
- Install is a one-line `curl | bash` that sets up SwiftBar, launch-at-login, and
  the live-usage bridge.
- **MIT licensed.**

Repo: https://github.com/dashpes/burnbar
