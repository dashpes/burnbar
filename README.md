# burnbar

A [SwiftBar](https://swiftbar.app) plugin that renders your **current Claude Code
5-hour usage block** as a literal progress bar that fills up, right in the macOS
menu bar — visual *and* text:

```
███████▍░░ 74% · 1.8M
```

It reads Claude Code's own local transcripts and shows how hard you're leaning on
the active block. No `ccusage`, no API keys, no network, no pricing tables.

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

## Install

1. Install SwiftBar:
   ```sh
   brew install --cask swiftbar
   ```
2. Launch SwiftBar once and pick a plugin folder when prompted (e.g.
   `~/.swiftbar`).
3. Symlink the plugin into that folder (or run `./install.sh`):
   ```sh
   ln -sf "$PWD/burnbar.30s.py" ~/.swiftbar/burnbar.30s.py
   ```
4. In SwiftBar, **Refresh All**. Done.

The `30s` in the filename is the refresh interval — rename to `burnbar.1m.py`,
`burnbar.10s.py`, etc. to taste.

> Works with [xbar](https://xbarapp.com) too — same plugin format.

## Tuning

All knobs are constants at the top of `burnbar.30s.py`:

| Constant            | Default   | Meaning                                      |
| ------------------- | --------- | -------------------------------------------- |
| `BLOCK_HOURS`       | `5`       | Length of a usage block                      |
| `BAR_CELLS`         | `10`      | Width of the menu-bar bar                    |
| `CACHE_READ_WEIGHT` | `0.1`     | Weight applied to cache-read tokens          |
| `PEAK_FLOOR`        | `300_000` | Min denominator before history kicks in      |

## License

MIT
