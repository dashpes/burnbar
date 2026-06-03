#!/usr/bin/env bash
# Symlink burnbar into your SwiftBar plugin folder.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN="$HERE/burnbar.30s.py"

# Try to read SwiftBar's configured plugin folder; fall back to ~/.swiftbar.
DIR="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
DIR="${DIR:-$HOME/.swiftbar}"

mkdir -p "$DIR"
chmod +x "$PLUGIN"
ln -sf "$PLUGIN" "$DIR/burnbar.30s.py"

echo "Linked -> $DIR/burnbar.30s.py"
echo "Now run 'Refresh All' in SwiftBar (or relaunch it)."
