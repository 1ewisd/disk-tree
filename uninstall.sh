#!/usr/bin/env bash
# Remove Disk Tree. Add --purge to also delete saved settings.
set -euo pipefail
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
rm -rf "$DATA_HOME/disktree"
rm -f  "$HOME/.local/bin/disktree" \
       "$DATA_HOME/applications/disktree.desktop" \
       "$DATA_HOME/icons/hicolor/scalable/apps/disktree.svg"
[ "${1:-}" = "--purge" ] && rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/disktree"
command -v update-desktop-database >/dev/null && update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
echo "Disk Tree removed."
