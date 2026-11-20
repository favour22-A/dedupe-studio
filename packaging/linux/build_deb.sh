#!/usr/bin/env bash
# Builds dedupe-studio_<version>_all.deb for Debian / Ubuntu.
#   GUI:  dedupe-studio        (app window via WebKitGTK; falls back to the browser)
#   CLI:  dedupe-studio-cli    (dedupe.sh: preview / apply / undo in the terminal)
# Usage: bash build_deb.sh      (needs python3, pip, dpkg-deb; internet for pywebview)
set -euo pipefail
cd "$(dirname "$0")/../.."
VER=$(grep -oP "^VERSION = '\K[^']+" dedupe_studio.py)
# assemble on a Linux filesystem: NTFS/shared drives can't hold the 0755 modes dpkg needs
WORK=$(mktemp -d)
PKG=$WORK/dedupe-studio_${VER}_all
mkdir -p packaging/linux/build "$PKG/DEBIAN" "$PKG/opt/dedupe-studio/lib" "$PKG/usr/bin" "$PKG/usr/share/applications" "$PKG/usr/share/doc/dedupe-studio"

cp dedupe_studio.py dedupe_studio_app.py dedupe.sh "$PKG/opt/dedupe-studio/"
chmod -R u+rwX,go+rX "$PKG"
cp LICENSE "$PKG/usr/share/doc/dedupe-studio/copyright"
# pywebview is pure Python on Linux (no .NET); bundle it so no pip is needed on the target
python3 -m pip install --quiet --no-compile --target "$PKG/opt/dedupe-studio/lib" \
  pywebview==6.2.1 bottle proxy_tools typing_extensions --no-deps

cat > "$PKG/usr/bin/dedupe-studio" <<'SH'
#!/bin/sh
export PYTHONPATH=/opt/dedupe-studio/lib${PYTHONPATH:+:$PYTHONPATH}
exec python3 /opt/dedupe-studio/dedupe_studio_app.py "$@"
SH
cat > "$PKG/usr/bin/dedupe-studio-cli" <<'SH'
#!/bin/sh
exec bash /opt/dedupe-studio/dedupe.sh "$@"
SH
chmod 755 "$PKG/usr/bin/"* "$PKG/opt/dedupe-studio/dedupe.sh"

cat > "$PKG/usr/share/applications/dedupe-studio.desktop" <<'DT'
[Desktop Entry]
Type=Application
Name=Dedupe Studio
Comment=Find duplicate files and move them to a trash folder you can undo
Exec=dedupe-studio
Icon=edit-find
Terminal=false
Categories=Utility;FileTools;
DT

SIZE=$(du -sk "$PKG" | cut -f1)
cat > "$PKG/DEBIAN/control" <<CT
Package: dedupe-studio
Version: ${VER}
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.8)
Recommends: python3-gi, gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0
Installed-Size: ${SIZE}
Maintainer: Ibitoru Cookey-Gam <noreply@users.noreply.github.com>
Description: Find duplicate files by content and clean them up safely
 Duplicates are moved to a _dedupe_trash folder, never deleted, and every
 move is logged so Undo can put files back. Game, system, hidden, sync-cache
 and code folders are skipped by default. Includes a GUI (dedupe-studio) and
 a command-line tool (dedupe-studio-cli).
CT
find "$PKG" -name '__pycache__' -prune -exec rm -rf {} +
chmod 755 "$PKG/DEBIAN"; find "$PKG" -type d -exec chmod 755 {} +; find "$PKG" -type f -exec chmod 644 {} +
chmod 755 "$PKG/usr/bin/"* "$PKG/opt/dedupe-studio/dedupe.sh"
dpkg-deb --build --root-owner-group "$PKG" >/dev/null
cp "$PKG.deb" packaging/linux/build/ && rm -rf "$WORK"
echo "Built: packaging/linux/build/$(basename "$PKG").deb"
