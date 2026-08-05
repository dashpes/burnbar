#!/usr/bin/env bash
# burnbar installer.
#
# Run it either way:
#   ./install.sh                                   # from a git checkout
#   curl -fsSL <raw>/install.sh | bash             # no clone needed
#   curl -fsSL <raw>/install.sh | bash -s -- -y    # unattended (all defaults)
#
# It symlinks the plugin into SwiftBar's folder, optionally makes SwiftBar
# launch at login, and optionally wires Claude + Cursor statusLine bridges.
#
# Flags:
#   -y, --yes     non-interactive; take the default for every prompt
#       --no-login  don't add the SwiftBar login item
#       --no-live   don't wire statusLine bridges
#   -h, --help    show this help
set -euo pipefail

RAW="${BURNBAR_RAW:-https://raw.githubusercontent.com/dashpes/burnbar/main}"

ASSUME_YES=0; DO_LOGIN=1; DO_LIVE=1
for a in "$@"; do
  case "$a" in
    -y|--yes)   ASSUME_YES=1 ;;
    --no-login) DO_LOGIN=0 ;;
    --no-live)  DO_LIVE=0 ;;
    -h|--help)  sed -n '2,16p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "burnbar: unknown option '$a' (try --help)" >&2; exit 2 ;;
  esac
done

# ask <prompt> <default y|n> -> exit 0 for yes. Reads the terminal (/dev/tty)
# so it works even under `curl | bash`; with -y or no terminal, uses <default>.
ask() {
  local prompt="$1" default="$2" yn=""
  if [ "$ASSUME_YES" != 1 ] && [ -r /dev/tty ]; then
    read -r -p "$prompt " yn < /dev/tty || yn=""
    yn="${yn:-$default}"
  else
    yn="$default"
  fi
  [[ "$yn" =~ ^[Yy]$ ]]
}

# 0. Locate the source files. From a checkout they sit next to this script;
#    piped via curl they don't exist yet, so fetch them into a stable home.
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "$PWD")"
if [ -f "$HERE/burnbar.30s.py" ]; then
  SRC="$HERE"
else
  SRC="${BURNBAR_HOME:-$HOME/.local/share/burnbar}"
  mkdir -p "$SRC"
  echo "Fetching burnbar into $SRC ..."
  for f in burnbar.30s.py burnbar-statusline.py burnbar-cursor-statusline.py; do
    # Download to a temp file and make sure it at least parses as Python before
    # swapping it in — a truncated or failed fetch must never clobber a working
    # install. The mv is atomic, so the live symlink target is never half-written.
    tmp="$SRC/.$f.tmp"
    curl -fsSL "$RAW/$f" -o "$tmp"
    if ! python3 -m py_compile "$tmp" 2>/dev/null; then
      rm -f "$tmp"
      echo "burnbar: download of $f failed to validate — aborting; existing install left untouched." >&2
      exit 1
    fi
    mv -f "$tmp" "$SRC/$f"
  done
fi
PLUGIN="$SRC/burnbar.30s.py"
BRIDGE="$SRC/burnbar-statusline.py"
CURSOR_BRIDGE="$SRC/burnbar-cursor-statusline.py"

# 1. Ensure SwiftBar is installed.
if [ ! -d "/Applications/SwiftBar.app" ]; then
  if command -v brew >/dev/null 2>&1; then
    if ask "SwiftBar isn't installed — install it now with Homebrew? [Y/n]" y; then
      brew install --cask swiftbar
    else
      echo "burnbar needs SwiftBar. Install it from https://swiftbar.app then re-run."
      exit 1
    fi
  else
    echo "SwiftBar isn't installed and Homebrew isn't available."
    echo "Install SwiftBar from https://swiftbar.app, then re-run this installer."
    exit 1
  fi
fi

# 2. Resolve SwiftBar's plugin folder (fall back to ~/.swiftbar).
DIR="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
DIR="${DIR:-$HOME/.swiftbar}"
mkdir -p "$DIR"
defaults write com.ameba.SwiftBar PluginDirectory "$DIR" >/dev/null 2>&1 || true

# 3. Symlink the plugin in.
chmod +x "$PLUGIN"
ln -sf "$PLUGIN" "$DIR/burnbar.30s.py"
echo "Linked  -> $DIR/burnbar.30s.py"

# 4. Launch at login (so burnbar runs on startup). Idempotent.
#    A LaunchAgent is used rather than a System Events login item: the AppleScript
#    "make login item" approach silently fails on recent macOS, so SwiftBar would
#    not come back after a reboot. RunAtLoad starts it at login; KeepAlive with
#    SuccessfulExit=false relaunches it if it crashes, but leaves a manual Quit
#    (clean exit) alone.
if [ "$DO_LOGIN" = 1 ] && ask "Make SwiftBar launch at login (and relaunch on crash)? [Y/n]" y; then
  AGENT="$HOME/Library/LaunchAgents/com.ameba.SwiftBar.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$AGENT" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ameba.SwiftBar</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Applications/SwiftBar.app/Contents/MacOS/SwiftBar</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ProcessType</key>
  <string>Interactive</string>
</dict>
</plist>
PLIST
  # (Re)load the agent. Quit any manual instance first so launchd owns the process.
  UID_="$(id -u)"
  osascript -e 'tell application "SwiftBar" to quit' >/dev/null 2>&1 || true
  pkill -x SwiftBar >/dev/null 2>&1 || true
  launchctl bootout "gui/$UID_" "$AGENT" >/dev/null 2>&1 || true
  if launchctl bootstrap "gui/$UID_" "$AGENT" >/dev/null 2>&1; then
    launchctl enable "gui/$UID_/com.ameba.SwiftBar" >/dev/null 2>&1 || true
    echo "  SwiftBar set to launch at login (LaunchAgent installed)."
  else
    echo "  (couldn't load the LaunchAgent automatically — toggle Launch at Login in SwiftBar > Preferences)"
  fi
fi

# 5. Live usage bridges — Claude rate limits + Cursor context window.
#    Captured from each CLI's statusLine; burnbar reads the local files offline.
if [ "$DO_LIVE" = 1 ]; then
  if command -v claude >/dev/null 2>&1 || [ -d "$HOME/.claude" ]; then
    if ask "Enable Claude live usage (real 5h/7d limits + session title in status bar)? [Y/n]" y; then
      chmod +x "$BRIDGE"
      mkdir -p "$HOME/.claude"
      SETTINGS="$HOME/.claude/settings.json"
      [ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.burnbar.bak" && echo "  Backed up -> $SETTINGS.burnbar.bak"
      FORCE="${FORCE:-0}" python3 - "$BRIDGE" "$SETTINGS" <<'PY'
import json, os, sys
bridge, p = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p))
except Exception:
    d = {}
ex = d.get("statusLine")
clash = isinstance(ex, dict) and ex.get("command") and ex.get("command") != bridge
if clash and os.environ.get("FORCE") != "1":
    print("  NOTE: you already have a Claude statusLine — leaving it as-is.")
    print("        Re-run with  FORCE=1 ./install.sh  to replace it.")
else:
    d["statusLine"] = {"type": "command", "command": bridge, "padding": 0}
    with open(p, "w") as f:
        json.dump(d, f, indent=2)
    print("  Claude live usage enabled. Populates on your next Claude Code message.")
PY
    fi
  fi

  if command -v agent >/dev/null 2>&1 || command -v cursor-agent >/dev/null 2>&1 \
     || [ -f "$HOME/.cursor/cli-config.json" ]; then
    if ask "Enable Cursor live context (context-window % in Cursor CLI status bar)? [Y/n]" y; then
      chmod +x "$CURSOR_BRIDGE"
      mkdir -p "$HOME/.cursor"
      CSETTINGS="$HOME/.cursor/cli-config.json"
      [ -f "$CSETTINGS" ] && cp "$CSETTINGS" "$CSETTINGS.burnbar.bak" && echo "  Backed up -> $CSETTINGS.burnbar.bak"
      FORCE="${FORCE:-0}" python3 - "$CURSOR_BRIDGE" "$CSETTINGS" <<'PY'
import json, os, sys
bridge, p = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p))
except Exception:
    d = {}
ex = d.get("statusLine")
clash = isinstance(ex, dict) and ex.get("command") and ex.get("command") != bridge
if clash and os.environ.get("FORCE") != "1":
    print("  NOTE: you already have a Cursor statusLine — leaving it as-is.")
    print("        Re-run with  FORCE=1 ./install.sh  to replace it.")
else:
    d["statusLine"] = {"type": "command", "command": bridge, "padding": 0}
    with open(p, "w") as f:
        json.dump(d, f, indent=2)
    print("  Cursor live context enabled. Populates on your next Cursor Agent update.")
PY
    fi
  fi
fi

# 6. Launch / refresh.
open -a SwiftBar
open "swiftbar://refreshallplugins" >/dev/null 2>&1 || true
echo "Done. Look for the burnbar bar in your menu bar; click it for stats + Settings."
