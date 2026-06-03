# burnbar

A [SwiftBar](https://swiftbar.app) plugin that renders your **current Claude Code
5-hour usage block** as a literal progress bar that fills up, right in the macOS
menu bar — visual *and* text:

```
███████▍░░ 74% · 1.8M
```

It reads Claude Code's own local transcripts and shows how hard you're leaning on
the active block. No `ccusage`, no API keys, no network, no pricing tables.

Click it and you get a full **Stats-style dropdown** with SF Symbol section
headers:

- **Current 5-hour block** — burn, % of peak, reset countdown, live tokens/min
  burn rate, projected end-of-block total, input/output/cache breakdown, model split
- **Today** — total, messages, sessions, peak hour, an hourly sparkline, by model
- **Last 7 days** — per-day mini bars, week total, month total
- **All time** — totals, raw vs effective tokens, sessions, projects, daily
  average, 24-hour activity sparkline, by model, by project, top sessions
- **Records** — peak block ever, busiest day, the calibrated 100% baseline
- **Recent blocks** — the last 10 rolling windows, live one highlighted

## How it works

- Reads `~/.claude/projects/**/*.jsonl` (Claude Code's local session transcripts).
- Sums the token usage on every assistant turn (`input`, `output`,
  `cache_creation`, `cache_read`), deduped by message id + request id.
- Groups turns into rolling **5-hour blocks** — the same way usage limits reset.
- The menu bar shows the **active** block: a fill bar, a `%`, and a compact token
  count, colored green → yellow → orange → red as it climbs.
- The dropdown breaks down input/output/cache, model split, reset countdown,
  today's total, last 7 days, and recent blocks.

### What "100%" means

100% is **auto-calibrated** to your busiest-ever 5-hour block — a self-tuning
high-water mark persisted to `~/.config/burnbar/state.json`. No plan limit or
dollar figure needed; the gauge gets more accurate the more you use it. Until you
build up history it's anchored to a `PEAK_FLOOR` (300K effective tokens).

Cache-read tokens are down-weighted (`CACHE_READ_WEIGHT = 0.1`) since they're far
lighter than fresh tokens — this makes the bar track real burn rather than cache
reuse. Set it to `1.0` in the script to count every token equally.

## Settings (no JSON editing)

Click the menu bar item → **⚙ Settings** to change things live; selections are
marked with ● and saved to `~/.config/burnbar/config.json`:

- **View** — `Default` (full panel) or `Compact` (just Current Block + Today,
  with the heavy stats tucked under a *More stats* submenu)
- **Theme** — `Default`, `Mono`, `Nord`, `Dracula`, `Solarized`, `Matrix`
  (recolors the bar + accent text)
- **Menu-bar tokens** — show/hide the `· 1.1M` token count next to the `%`
- **Menu-bar width** — bar cells: 3 / 5 / 8 / 10

## Install

```sh
git clone https://github.com/dashpes/burnbar.git
cd burnbar
./install.sh
```

`install.sh` will: install SwiftBar (via Homebrew) if missing, symlink the
plugin into SwiftBar's folder, offer to **launch SwiftBar at login** (so burnbar
runs on startup), and refresh. That's it.

<details>
<summary>Manual install</summary>

```sh
brew install --cask swiftbar          # then launch & pick a plugin folder
ln -sf "$PWD/burnbar.30s.py" ~/.swiftbar/burnbar.30s.py
open "swiftbar://refreshallplugins"
```
</details>

The `30s` in the filename is the refresh interval — rename to `burnbar.1m.py`,
`burnbar.10s.py`, etc. to taste. Works with [xbar](https://xbarapp.com) too.

## Tuning

The Settings submenu covers the common knobs. Deeper ones are constants at the
top of `burnbar.30s.py`:

| Constant            | Default   | Meaning                                      |
| ------------------- | --------- | -------------------------------------------- |
| `BLOCK_HOURS`       | `5`       | Length of a usage block                      |
| `BAR_CELLS`         | `10`      | Bar width inside the dropdown                |
| `CACHE_READ_WEIGHT` | `0.1`     | Weight applied to cache-read tokens          |
| `PEAK_FLOOR`        | `300_000` | Min denominator before history kicks in      |
| `THEMES`            | —         | Add your own `name: (low, mid, high, max)`   |

## License

MIT
