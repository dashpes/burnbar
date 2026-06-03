#!/usr/bin/env bash
# burnbar installer — symlinks the plugin into SwiftBar's folder and
# (optionally) makes SwiftBar launch at login so burnbar runs on startup.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN="$HERE/burnbar.30s.py"

# 1. Ensure SwiftBar is installed.
if [ ! -d "/Applications/SwiftBar.app" ]; then
  echo "SwiftBar is not installed."
  if command -v brew >/dev/null 2>&1; then
    read -r -p "Install it now with Homebrew? [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] && brew install --cask swiftbar
  else
    echo "Install it from https://swiftbar.app then re-run this script."
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
read -r -p "Make SwiftBar launch at login? [Y/n] " yn
if [[ ! "$yn" =~ ^[Nn]$ ]]; then
  osascript >/dev/null 2>&1 <<'OSA' || echo "  (couldn't add login item automatically — toggle it in SwiftBar > Preferences)"
tell application "System Events"
  if not (exists login item "SwiftBar") then
    make login item at end with properties {path:"/Applications/SwiftBar.app", hidden:true}
  end if
end tell
OSA
  echo "  SwiftBar set to launch at login."
fi

# 5. Live usage bridge — real 5h/7d limits + reset times, captured from Claude
#    Code's statusLine (the same data the /usage command shows).
BRIDGE="$HERE/burnbar-statusline.py"
chmod +x "$BRIDGE"
read -r -p "Enable live usage (real limits + reset times via Claude Code statusLine)? [Y/n] " yn
if [[ ! "$yn" =~ ^[Nn]$ ]]; then
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
    print("  NOTE: you already have a statusLine configured — leaving it as-is.")
    print("        Re-run with  FORCE=1 ./install.sh  to replace it, or have your")
    print("        command also write rate_limits to ~/.config/burnbar/usage.json (see README).")
else:
    d["statusLine"] = {"type": "command", "command": bridge, "padding": 0}
    with open(p, "w") as f:
        json.dump(d, f, indent=2)
    print("  Live usage enabled. It populates on your next Claude Code message.")
PY
fi

# 6. Launch / refresh.
open -a SwiftBar
open "swiftbar://refreshallplugins" >/dev/null 2>&1 || true
echo "Done. Look for the burnbar bar in your menu bar; click it for stats + ⚙ Settings."
