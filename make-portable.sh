#!/bin/sh
# Build the portable zip: dist/kestrel-<version>-portable.zip
# Unzip anywhere and run ./kestrel — nothing is installed. Needs python3 + python3-gobject + gtk3 on the host.
set -e
cd "$(dirname "$0")"
./build.sh >/dev/null
VER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' kestrel.py)
OUT="kestrel-$VER-portable"
rm -rf "dist/$OUT" && mkdir -p "dist/$OUT"
cp kestrel.py README.md LICENSE install.sh "dist/$OUT/"
mkdir -p "dist/$OUT/packaging" "dist/$OUT/docs" && cp packaging/kestrel.desktop "dist/$OUT/packaging/" && cp docs/*.md "dist/$OUT/docs/"
cat > "dist/$OUT/kestrel" <<'SH'
#!/bin/sh
# Portable launcher: runs kestrel.py from this folder, keeps settings in ./config when present.
HERE=$(cd "$(dirname "$0")" && pwd)
[ -d "$HERE/config" ] && export XDG_CONFIG_HOME="$HERE/config" XDG_CACHE_HOME="$HERE/config/cache"
exec python3 "$HERE/kestrel.py" "$@"
SH
chmod +x "dist/$OUT/kestrel" "dist/$OUT/kestrel.py" "dist/$OUT/install.sh"
cat > "dist/$OUT/PORTABLE.txt" <<'TXT'
Kestrel File Browser — portable build

  ./kestrel [folder]        run from here (no install)
  mkdir config              make it self-contained: settings + index live in ./config instead of ~/.config
  ./install.sh              optional: install to ~/.local/bin and the app menu

Requires: python3 (3.9+), python3-gobject, gtk3.
  Rocky/RHEL/Fedora:  sudo dnf install python3-gobject gtk3
  Debian/Ubuntu:      sudo apt install python3-gi gir1.2-gtk-3.0
Optional extras (video, 3D formats, archives…): Preferences → Optional inside the app.
TXT
(cd dist && rm -f "$OUT.zip" && zip -qr "$OUT.zip" "$OUT")
echo "dist/$OUT.zip"
