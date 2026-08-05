# burnbar

A [SwiftBar](https://swiftbar.app) plugin that tracks **CLI agent usage** —
Claude Code and Cursor Agent today — as a progress bar in the macOS menu bar:

```
███▉░ 37% · 3h09m
```

It auto-detects what's installed and shows a **Claude** section and/or a
**Cursor** section. Offline-first: live numbers come from each CLI's statusLine
bridge written to local files; token/session stats come from local transcripts.
No API keys, no pricing tables, no telemetry — your usage never leaves your
machine. (The lone exception: an optional once-a-day check to GitHub for a newer
version — it sends nothing about you, and it's one toggle away in Settings.)

<p align="center">
  <img src="docs/demo.gif" alt="burnbar demo" width="400">
</p>

<p align="center">
  <img src="docs/screenshot-compact.png" alt="burnbar compact view" width="370">
  &nbsp;&nbsp;
  <img src="docs/screenshot.png" alt="burnbar detailed view" width="370">
</p>
<p align="center"><em>Compact view (left, the default) and Detailed view (right).</em></p>

Click it for a **Stats-style dropdown** with SF Symbol section headers:

- **Claude** — live context windows for open sessions (hottest / at-risk first),
  live 5-hour / 7-day / Opus limits, Today / 7-day / all-time token stats from
  `~/.claude/projects/**/*.jsonl`
- **Cursor** — live multi-session context fill (via statusLine bridge; sessions
  ranked by context size so rot stands out), today's turn counts and recent
  sessions from `~/.cursor`
- **CONTEXT AT RISK** — top-of-menu strip tracking two *separate* risks:
  **quality decay** (`drifting` / `degraded` / `rot`) and **compaction
  proximity** on % of window (`near full` 70% · `compacting` 85%) — plus a line
  on what to do about it

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
- **Settings** — providers (auto / Claude / Cursor / both), view, theme,
  menu-bar trailer & width (no JSON editing)

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

Click the menu bar item → **Settings** to change things live; selections are
marked with `[x]` and saved to `~/.config/burnbar/config.json`:

- **View** — `Compact` (default: just Today + a *More stats* submenu) or
  `Detailed` (the full stats panel)
- **Providers** — `Auto-detect` (default: show whichever CLIs are installed),
  `Claude only`, `Cursor only`, or `Claude + Cursor`
- **Theme** — `Default`, `Mono`, `Nord`, `Dracula`, `Solarized`, `Matrix`
  (recolors the bar + accent text)
- **Menu-bar trailer** — what shows after the `%`: `Reset countdown` (e.g.
  `· 3h40m`), `Token count`, or `None`
- **Menu-bar width** — bar cells: 3 / 5 / 8 / 10
- **Context window** — how the *Context* section sizes each bar: `Auto-detect`,
  `200K`, or `1M` (see below)
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
  wired; otherwise the Cursor section shows session activity only.
- **Claude token stats** are read from `~/.claude/projects/**/*.jsonl`: every
  assistant turn's `input` / `output` / `cache_creation` / `cache_read`, deduped
  by message id + request id, grouped into rolling 5-hour blocks and rolled up
  by day / model / project / session.
- **Cursor session stats** are read from `~/.cursor/projects/**/agent-transcripts`
  and `~/.cursor/chats/**/meta.json` (turn counts and titles — no billed tokens
  on disk).
- Providers are **auto-detected** (`claude` / `agent` on PATH, or their config
  dirs). Override under Settings → Providers.
- The menu bar fill bar is colored green → yellow → orange → red as it climbs,
  followed by a reset countdown when Claude limits are live (swap to a token
  count or nothing in Settings).

Claude Code's transcripts don't record the window *size*, so `Auto-detect` goes
by the model running the latest turn: Opus is the 1M-context model, so an Opus
session is sized to 1M; Haiku/Sonnet sessions use the standard 200K (with a
fallback to 1M for anything that's somehow crossed 200K). If you run a fixed
setup, pin it to `200K` or `1M` in Settings.

Cache-read tokens are down-weighted (`CACHE_READ_WEIGHT = 0.1`) in the Claude
token stats, since they're far lighter than fresh tokens — this makes "effective
tokens" track real burn rather than cache reuse. Set it to `1.0` to count every
token equally.

## Footprint

burnbar creates no growing files of its own — `claude/usage.json`,
`cursor/live.json`, `cursor/sessions.json`, `config.json`, and `cache.json` are
all fixed-size or overwritten (sessions prune after 2h idle). The transcripts it
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
