# burnbar

A [SwiftBar](https://swiftbar.app) plugin that tracks **every AI coding agent you
run**, in one place, as a progress bar in the macOS menu bar:

```
███▉░ 37% · 3h09m
```

Claude Code, Cursor Agent and OpenCode ship today, and more are being added — see
[Adding a provider](#adding-a-provider). burnbar auto-detects what's installed
and merges every running session into one list, so "how much context has this
agent burned?" has a single answer regardless of which tool it came from.
Offline-first: live numbers come from each CLI's statusLine bridge written to
local files; token/session stats come from local transcripts.
No API keys, no pricing tables, no telemetry — your usage never leaves your
machine. (The lone exception: an optional once-a-day check to GitHub for a newer
version — it sends nothing about you, and it's one toggle away in Settings.)

<p align="center">
  <img src="docs/demo.gif" alt="burnbar demo" width="400">
</p>

<p align="center">
  <img src="docs/screenshot-compact.png" alt="burnbar dropdown" width="370">
  &nbsp;&nbsp;
  <img src="docs/screenshot.png" alt="burnbar Stats submenu" width="370">
</p>
<p align="center"><em>The dropdown (left) and the Stats submenu (right).</em></p>

Click it for a dropdown that stays short — what's happening now at the top level,
everything else one hover away:

- **LIVE AGENTS** — every running session from every provider in *one* list,
  worst context first, each row naming its provider (plus an SF Symbol: ⚡ Claude,
  ↖ Cursor). Subagents nest under the session that spawned them. Bar *length* is
  window fill; bar *colour* is the rot band — they're independent signals and
  they're meant to be able to disagree.
- **LIMITS** — the real cross-surface 5-hour / 7-day / Opus limits from Anthropic
  (via the statusLine bridge), with the reset countdown
- **TODAY** — tokens, messages and sessions per provider, your git commit count,
  and an hour-by-hour sparkline
- **Stats** — 7-day and all-time totals, by model / project / session, recent
  blocks, records, and Cursor session history
- **Settings** — providers, theme, context window, menu-bar trailer & width,
  commits, update check (no JSON editing)

Two *separate* context risks drive the colours and the one-line advice under
LIVE AGENTS: **quality decay** (`drifting` / `degraded` / `rot`) and
**compaction proximity** on % of window (`near full` 70% · `compacting` 85%).

Rot bands are token counts that scale with the window, sub-linearly:

| window | drifting | degraded | rot |
|---|---|---|---|
| ≤200K | 32K | 60K | 128K |
| 1M | 100K | 200K | 400K |

Neither raw percentage nor a flat token count works alone. Percentage hides
depth — a 1M session at 30% holds 300K tokens and is further gone than a 200K
one at 85%. But a flat threshold over-warns on big windows: a model built for
1M genuinely holds up past where a 200K one gives out. Hence a table.

Calibration: [NoLiMa](https://arxiv.org/html/2502.05167v3) (11 of 13 models
claiming ≥128K fall below half their short-context baseline at 32K),
[RULER](https://arxiv.org/abs/2404.06654) (effective context ≈ 50–65% of
advertised), [Chroma's Context Rot](https://www.trychroma.com/research/context-rot)
(all 18 frontier models degrade before their limit; distractors compound it),
and reported multi-needle effective ranges of 200–400K for current 1M-class
flagships. Bands sit on the conservative side of those numbers because agentic
coding is multi-needle deep comprehension over a codebase full of near-miss
distractors — the hardest case, and harder than what most of these benchmarks
test. They're calibrated heuristics, not laws; tune `CTX_BAND_TABLE` to taste.

## Install

One line, no clone:

```sh
curl -fsSL https://raw.githubusercontent.com/dashpes/burnbar/main/install.sh | bash
```

This downloads the two scripts into `~/.local/share/burnbar`, then does the rest.
Prefer a checkout (so `git pull` updates it)? That works too:

```sh
git clone https://github.com/dashpes/burnbar.git && cd burnbar && ./install.sh
```

Either way `install.sh` will: install SwiftBar (via Homebrew) if missing, symlink
the plugin into SwiftBar's folder, offer to **launch SwiftBar at login** (so
burnbar runs on startup), offer to wire **live usage**, and refresh. That's it.

**Unattended / flags** — take every default with no prompts:

```sh
curl -fsSL https://raw.githubusercontent.com/dashpes/burnbar/main/install.sh | bash -s -- -y
```

| Flag         | Effect                                              |
| ------------ | --------------------------------------------------- |
| `-y, --yes`  | non-interactive; take the default for every prompt  |
| `--no-login` | skip the SwiftBar login item                        |
| `--no-live`  | skip wiring Claude / Cursor statusLine bridges      |

> When piped through `curl`, prompts read from your terminal (`/dev/tty`); with
> no terminal it falls back to the defaults, same as `-y`.

<details>
<summary>Manual install</summary>

```sh
brew install --cask swiftbar          # then launch & pick a plugin folder
ln -sf "$PWD/burnbar.30s.py" ~/.swiftbar/burnbar.30s.py
open "swiftbar://refreshallplugins"
```
</details>

## Updating

burnbar checks GitHub for a newer version **at most once a day** (a plain
version-only GET — nothing about your usage is sent). When one is out, an
**Update to x.y.z** row appears at the top of the dropdown; clicking it opens a
Terminal and updates in place — `git pull` for a checkout, or a re-run of
`install.sh` for a `curl` install — then refreshes SwiftBar.

That daily check is also the only "analytics" burnbar has: it fetches a tiny
`version.txt` attached to the latest release, so GitHub's own **download count**
for that file acts as an anonymous, aggregate tally of active installs. It's the
same version-only GET — no identifier, no usage, nothing about you is sent; the
count lives server-side on GitHub, and it's how I gauge whether anyone's actually
using this.

Don't want the check? Flip **Settings → Check for updates → Off**; burnbar then
makes no network calls at all — which also opts you out of the install count. You
can always update by hand:

```sh
# checkout install
cd /path/to/burnbar && git pull && ./install.sh
# curl install
curl -fsSL https://raw.githubusercontent.com/dashpes/burnbar/main/install.sh | bash -s -- -y
```

The `30s` in the filename is the refresh interval — rename to `burnbar.1m.py`,
`burnbar.10s.py`, etc. to taste. Works with [xbar](https://xbarapp.com) too.

## Settings (no JSON editing)

Click the menu bar item → **Settings** to change things live; the selected option
is checkmarked and saved to `~/.config/burnbar/config.json`:

- **Providers** — `Auto-detect installed agents` (default), or tick individual
  agents to pin exactly the set you want shown
- **Theme** — `Default`, `Mono`, `Nord`, `Dracula`, `Solarized`, `Matrix`
  (recolors the bar + accent text)
- **Menu-bar trailer** — what shows after the `%`: `Reset countdown` (e.g.
  `· 3h40m`), `Token count`, or `None`
- **Menu-bar width** — bar cells: 3 / 5 / 8 / 10
- **Context window** — how *LIVE AGENTS* sizes each bar: `Auto-detect`, `200K`,
  or `1M` (see below)
- **Check for updates** — `Daily` (default; a version-only GET to GitHub, see
  [Updating](#updating)) or `Off` (no network calls at all)
- **Commits today** — `On` (default) shows a count of *your* git commits made
  today in the Today section, or `Off` to hide it (which also skips the scan).
  Auto-detects you from your `git config` identity and scans common code folders
  (`~/Developer`, `~/Projects`, `~/Code`, `~/dev`, `~/src`, `~/repos`); override
  the author or folders with `commit_author` / `commit_dirs` in `config.json`.

## How the live limits work

### Claude Code

The real numbers aren't in any file that `ccusage`-style tools read — Claude Code
only exposes them through its **statusLine** (a command it feeds a JSON blob on
every UI update). `burnbar-statusline.py` captures that `rate_limits` object to
`~/.config/burnbar/claude/usage.json` and prints a compact status line back to
Claude Code, led by the session's own title so you can tell which terminal/tab
is which:

```
Add context tracking to burnbar  5h ████░░░░ 48%·2h31m  7d 31%  Opus 4.8
```

`install.sh` offers to wire it for you. Manual setup — add to
`~/.claude/settings.json`:

```json
"statusLine": { "type": "command", "command": "/abs/path/to/burnbar-statusline.py", "padding": 0 }
```

Because Anthropic computes the limits server-side, the captured values already
include claude.ai web, Claude Code, and every machine you use — and the reset
times are exact. They refresh whenever you use Claude Code on this Mac; when
idle, burnbar shows the last-known reading with an "as of" time.

### Cursor CLI

Cursor's statusLine (same shape as Claude's) exposes **context window** fill, not
plan/quota. `burnbar-cursor-statusline.py` captures that to
`~/.config/burnbar/cursor/live.json`. Manual setup — add to
`~/.cursor/cli-config.json`:

```json
"statusLine": { "type": "command", "command": "/abs/path/to/burnbar-cursor-statusline.py", "padding": 0 }
```

Cursor plan/quota (Auto % / API %) is server-side only and is **not** fetched by
burnbar — keeping the menu bar offline-first.

> Everything stays on your machine — the bridges only write local files. No
> network calls, no token handling (the CLIs already hold their OAuth tokens).

## How it works

- **Live Claude limits** come from Anthropic via the statusLine bridge —
  `rate_limits` (5-hour / 7-day / Opus) with exact reset times. The menu bar `%`
  prefers this when available.
- **Live Cursor context** comes from Cursor's statusLine (`context_window`) when
  wired; otherwise Cursor contributes session activity only.
- **Which sessions are live** cross-checks two signals, because each covers the
  other's blind spot. The bridge's session registry (`claude/sessions.json`,
  `cursor/sessions.json`) records a heartbeat per session id every time the
  statusLine hook fires, so it knows exactly *which* sessions are open — but a
  closed tab just stops beating, which looks like idling until the entry ages
  out. A `pgrep` count knows *how many* CLI processes exist but not which. So a
  session heard from in the last 5 minutes is live outright (process enumeration
  can be restricted, and must never veto a fresh heartbeat), while quieter
  entries need the process count to corroborate them, newest first. With no
  bridge at all, burnbar falls back to `pgrep` plus each process's working
  directory, gated on transcript freshness.
- **Claude token stats** are read from `~/.claude/projects/**/*.jsonl`: every
  assistant turn's `input` / `output` / `cache_creation` / `cache_read`, deduped
  by message id + request id, grouped into rolling 5-hour blocks and rolled up
  by day / model / project / session.
- **Cursor session stats** are read from `~/.cursor/projects/**/agent-transcripts`
  and `~/.cursor/chats/**/meta.json` (turn counts and titles — no billed tokens
  on disk).
- **OpenCode** keeps everything in one SQLite database
  (`~/.local/share/opencode/opencode.db`) rather than JSON transcripts, which is
  why its numbers are easy to miss when you go looking for files. burnbar opens it
  **read-only** through a `file:…?mode=ro` URI, so a refresh can never lock or
  write the store the running agent owns. Context is the newest *completed*
  assistant turn's `tokens.total` — the same figure OpenCode shows in its own
  sidebar, so the two never disagree. (A turn still streaming reports all zeros;
  burnbar walks back to the last completed one rather than showing an empty
  context mid-reply.)
- Agents are **auto-detected** (`claude` / `agent` on PATH, or their config
  dirs). Pin the set under Settings → Providers.
- The menu bar fill bar is colored green → yellow → orange → red as it climbs,
  followed by a reset countdown when Claude limits are live (swap to a token
  count or nothing in Settings).

## Adding a provider

Agents live in the `PROVIDERS` registry near the top of `burnbar.30s.py`. One
entry drives detection, the settings row, the menu label and icon, the agent
list and the TODAY line — nothing else needs a new branch:

```python
{"key": "opencode", "label": "OpenCode", "icon": "chevron.left.forwardslash.chevron.right",
 "detect": lambda: bool(_which("opencode")),
 "gather": lambda now, tz, now_epoch: gather_opencode(now, tz),
 "rows":   lambda pdata, cfg, now_epoch: opencode_agent_rows(pdata, now_epoch),
 "today":  lambda pdata: f"{pdata['turns']:>7} turns",
 "stats":  None,                      # optional Stats-submenu section
 "setup":  "#opencode"},              # docs anchor for the setup nudge
```

`rows` returns one dict per live session — `{prov, key, label, tok, win, pct,
age}` — and the shared pipeline does the rest: context-rot banding, ranking
against every other agent, colouring and rendering. `tok` is the session's
current context occupancy and `win` its window size (`None` if the agent doesn't
report one).

If the agent exposes a Claude-Code-style `statusLine` hook, add a bridge script
modelled on `burnbar-cursor-statusline.py`: writing a per-session heartbeat to
`~/.config/burnbar/<agent>/sessions.json` is what lets burnbar tell an open
session from a closed one. Without it, liveness has to be inferred from the
process table, which is markedly worse (see [How it works](#how-it-works)).

Per-provider visibility is stored as `provider_<key>` in `config.json`; the key
is added to the defaults automatically from the registry.

### Context windows for OpenCode models

OpenCode runs both hosted and local models, and is the agent people switch models
in most — so the window is resolved per model, not per session, and re-resolved
the moment you switch. Two steps: the models.dev mirror it already caches
(`~/.cache/opencode/models.json` → `provider.models[id].limit.context`) covers
hosted providers, and for local models — which aren't listed anywhere public —
burnbar asks whichever runtime loaded them. For Ollama that's
`http://127.0.0.1:11434/api/ps`, which reports the context length the model was
actually loaded with. Each row also names the model in play, since a 32K local
model and a 1M hosted one look nothing alike.

burnbar sizes the window by the session's **currently selected** model, not the
one that ran the last turn. Switching down doesn't shrink the context you're
already carrying, so 300K tokens on a 1M model (`30% · degraded`) becomes
`300K/32K · 100% · rot · compacting` the instant you switch to a 32K local model
— which is exactly when that warning earns its keep.

Resolved windows are cached in `~/.config/burnbar/windows.json` (the mirror is
~3.5MB to parse and limits don't move). Two wrinkles that caching has to respect:
a *miss* expires in two minutes, because `/api/ps` only lists models currently
loaded and a model you just switched to legitimately isn't there yet; and a
window that *was* resolved sticks even when the runtime later goes quiet, since
Ollama unloads idle models and then can't say what window they had — a gap in the
answer, not a change to it. Sticky isn't stuck: reload with a different `-c` and
the new size is picked up.

That loopback request goes to a daemon already running on your machine; nothing
leaves it, and it's skipped entirely when Ollama isn't listening or the model was
found in the mirror.

Worth knowing: OpenCode itself reports `0% used` for local models, because it has
no limit to divide by. burnbar shows the real percentage when the runtime can
tell it, and an em dash (`—`) with a dashed bar when nothing can — never `0%`,
which would read as "plenty of room left". The context-rot band still applies
either way, since that reads absolute tokens rather than a percentage.

### Context windows for Claude models

Claude Code reports the exact window it has given each session (`context_window.
context_window_size` in the statusLine payload), and the bridge records it — so
the number is right, and it **follows a `/model` switch on its own** with no
lookup table to keep current.

This matters more than it sounds. Claude Code exposes `claude-opus-5` and
`claude-opus-5[1m]` as *separate* models of the same underlying one, with
different windows, so no name-based guess can be right for both. A public model
table doesn't help either: it lists what a model is capable of, not what Claude
Code hands this session.

Without the bridge there's nothing but the name, so `Auto-detect` guesses
conservatively — the `[1m]` suffix means 1M, a session whose own high-water mark
has passed 200K is clearly bigger than that, and anything else is assumed 200K.
Guessing high would hide rot; guessing low only warns early. If you run a fixed
setup, pin it to `200K` or `1M` in Settings.

Cache-read tokens are down-weighted (`CACHE_READ_WEIGHT = 0.1`) in the Claude
token stats, since they're far lighter than fresh tokens — this makes "effective
tokens" track real burn rather than cache reuse. Set it to `1.0` to count every
token equally.

## Footprint

burnbar creates no growing files of its own — `claude/usage.json`,
`claude/sessions.json`, `cursor/live.json`, `cursor/sessions.json`,
`config.json`, `commits.json`, and `cache.json` are all fixed-size or
overwritten (session registries prune after 2h idle). The transcripts it
reads are written and owned by each CLI, not burnbar.

To stay fast no matter how large that history grows, burnbar keeps an
**incremental cache** (`~/.config/burnbar/cache.json`): each transcript's totals
are cached by size + mtime, so a refresh only re-parses files that are new or
changed (or modified within `RECENT_DAYS`). Entries for deleted files are pruned
automatically. In practice a refresh stays in the tens of milliseconds even with
years of history.

## Tuning

The Settings submenu covers the common knobs. Deeper ones are constants at the
top of `burnbar.30s.py`:

| Constant            | Default   | Meaning                                      |
| ------------------- | --------- | -------------------------------------------- |
| `BLOCK_HOURS`       | `5`       | Length of a usage block                      |
| `BAR_CELLS`         | `10`      | Bar width inside the dropdown                |
| `CACHE_READ_WEIGHT` | `0.1`     | Weight applied to cache-read tokens          |
| `RECENT_DAYS`       | `3`       | Recent window re-parsed each refresh (lean)  |
| `CONTEXT_ACTIVE_MIN`| `120`     | Minutes a main session stays in *Context*    |
| `CONTEXT_AGENT_MIN` | `15`      | Minutes a subagent stays in *Context*        |
| `THEMES`            | —         | Add your own `name: {grad, text, muted}`     |

## License

MIT
