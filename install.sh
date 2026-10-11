#!/usr/bin/env bash
# Installs HDHomeRun Viewer for the current user (no root needed).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

command -v mpv >/dev/null || echo "Warning: mpv not found (sudo apt install mpv)"
python3 -c "import tkinter" 2>/dev/null || echo "Warning: tkinter missing (sudo apt install python3-tk)"

mkdir -p "$BIN" "$APPS"
install -m 755 "$DIR/hdhr_viewer.py" "$BIN/hdhr-viewer"
sed "s|@BIN@|$BIN/hdhr-viewer|" "$DIR/hdhr-viewer.desktop.in" > "$APPS/hdhr-viewer.desktop"
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed. Find 'HDHomeRun Viewer' in your application menu."
