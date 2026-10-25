#!/bin/sh
# Install Kestrel for the current user: ~/.local/bin/kestrel plus an application-menu entry.
# Usage: ./install.sh        ./install.sh --uninstall
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"
if [ "$1" = "--uninstall" ]; then
  rm -f "$BIN/kestrel" "$APPS/kestrel.desktop"
  update-desktop-database "$APPS" 2>/dev/null || true
  echo "Removed. Settings/cache left in ~/.config/kestrel and ~/.cache/kestrel."
  exit 0
fi
if ! python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null; then
  echo "PyGObject/GTK 3 missing."
  echo "  Rocky/RHEL/Fedora:  sudo dnf install python3-gobject gtk3"
  echo "  Debian/Ubuntu:      sudo apt install python3-gi gir1.2-gtk-3.0"
  echo "  Arch/CachyOS:       sudo pacman -S python-gobject gtk3"
  exit 1
fi
mkdir -p "$BIN" "$APPS"
cp "$HERE/kestrel.py" "$BIN/kestrel"; chmod +x "$BIN/kestrel"
sed "s|^Exec=.*|Exec=$BIN/kestrel %U|" "$HERE/packaging/kestrel.desktop" > "$APPS/kestrel.desktop"
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed: kestrel [folder]   (app menu: 'Kestrel File Browser')"
echo "Default folder handler:  xdg-mime default kestrel.desktop inode/directory"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN is not on your PATH yet.";; esac
