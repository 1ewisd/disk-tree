#!/usr/bin/env bash
# Disk Tree — user-level installer (no root needed).
#
#   ./install.sh                  install into ~/.local (own venv, launcher, menu entry, icon)
#   ./install.sh --system-packages  reuse PySide6 already installed by your distro
#   ./install.sh --python /path/to/python3
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_HOME/disktree"
BIN_DIR="$HOME/.local/bin"
MIN_MINOR=10
SYSPKGS=0; PY=""

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  \033[36m•\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --system-packages) SYSPKGS=1 ;;
    --python) PY="${2:?--python needs a path}"; shift ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

[ "$(uname -s)" = "Linux" ] || die "this is a Linux app"
[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root (it installs into ~/.local)"

bold "Disk Tree installer"

# 1. Python >= 3.$MIN_MINOR
py_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" 2>/dev/null; }
if [ -z "$PY" ]; then
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then PY="$(command -v "$c")"; break; fi
  done
fi
if [ -z "$PY" ] || ! py_ok "$PY"; then
  die "need Python 3.$MIN_MINOR or newer.
  Debian/Ubuntu: sudo apt install python3 python3-venv
  Fedora:        sudo dnf install python3
  Arch:          sudo pacman -S python"
fi
info "Python: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

# 2. venv module (Debian/Ubuntu ship it separately)
"$PY" -c "import venv, ensurepip" 2>/dev/null || die "Python venv support is missing.
  Debian/Ubuntu: sudo apt install python3-venv"

# 3. virtual environment
VENV="$APP_DIR/venv"
mkdir -p "$APP_DIR" "$BIN_DIR"
if [ -d "$VENV" ]; then info "Updating existing environment"; rm -rf "$VENV"; fi
info "Creating environment in $VENV"
if [ "$SYSPKGS" -eq 1 ]; then "$PY" -m venv --system-site-packages "$VENV"; else "$PY" -m venv "$VENV"; fi

# 4. install the package (+ PySide6 unless provided by the system)
info "Installing Disk Tree and its dependency PySide6 (~100 MB, one-off)"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet "$REPO"

# 5. sanity-check that Qt actually loads here
if ! QT_QPA_PLATFORM=offscreen "$VENV/bin/python" -c \
     "from PySide6.QtWidgets import QApplication; QApplication([])" 2>/tmp/disktree-qt-check.$$; then
  warn "Qt failed to start. Missing system libraries are the usual cause:"
  sed 's/^/      /' /tmp/disktree-qt-check.$$ >&2
  warn "Debian/Ubuntu: sudo apt install libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0"
  warn "Fedora:        sudo dnf install mesa-libEGL mesa-libGL libxkbcommon-x11 xcb-util-cursor"
  rm -f /tmp/disktree-qt-check.$$
  die "install aborted"
fi
rm -f /tmp/disktree-qt-check.$$

# 6. launcher, icon, menu entry
ln -sf "$VENV/bin/disktree" "$BIN_DIR/disktree"
info "Launcher: $BIN_DIR/disktree"

ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
APPS_DIR="$DATA_HOME/applications"
mkdir -p "$ICON_DIR" "$APPS_DIR"
install -m 644 "$REPO/src/disktree/assets/disktree.svg" "$ICON_DIR/disktree.svg"
sed "s|@EXEC@|$BIN_DIR/disktree|" "$REPO/data/disktree.desktop.in" > "$APPS_DIR/disktree.desktop"
chmod 644 "$APPS_DIR/disktree.desktop"
info "Menu entry: $APPS_DIR/disktree.desktop"

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null || true
command -v gtk-update-icon-cache  >/dev/null && gtk-update-icon-cache -qf "$DATA_HOME/icons/hicolor" 2>/dev/null || true
command -v kbuildsycoca6          >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

echo
bold "Done. Launch it from your app menu (System > Disk Tree) or run: disktree"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not on your PATH; add it to run 'disktree' from a terminal" ;; esac
