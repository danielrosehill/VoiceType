#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PKG_NAME="voicetype"
VERSION="0.4.0"
ARCH="amd64"
DEB_NAME="${PKG_NAME}_${VERSION}_${ARCH}.deb"
BUILD_DIR="$(mktemp -d)"
INSTALL_PREFIX="$BUILD_DIR/opt/$PKG_NAME"

echo "==> Building $DEB_NAME"

# 1. Create venv and install the package into it
echo "    Creating venv at $INSTALL_PREFIX ..."
uv venv "$INSTALL_PREFIX" --python python3 --quiet
VIRTUAL_ENV="$INSTALL_PREFIX" uv pip install --quiet "$SCRIPT_DIR"'[vad]'

# 2. Create wrapper script
mkdir -p "$BUILD_DIR/usr/bin"
cat > "$BUILD_DIR/usr/bin/$PKG_NAME" << 'WRAPPER'
#!/bin/sh
exec /opt/voicetype/bin/python -m voicetype.main "$@"
WRAPPER
chmod 755 "$BUILD_DIR/usr/bin/$PKG_NAME"

# 3. Desktop file
mkdir -p "$BUILD_DIR/usr/share/applications"
cat > "$BUILD_DIR/usr/share/applications/voicetype.desktop" << 'DESKTOP'
[Desktop Entry]
Name=VoiceType
Comment=Real-time voice keyboard
Exec=/usr/bin/voicetype
Icon=audio-input-microphone
Terminal=false
Type=Application
Categories=Utility;Accessibility;
Keywords=voice;dictation;keyboard;speech;
DESKTOP

# 4. DEBIAN control
mkdir -p "$BUILD_DIR/DEBIAN"
INSTALLED_SIZE=$(du -sk "$BUILD_DIR" | cut -f1)
cat > "$BUILD_DIR/DEBIAN/control" << EOF
Package: $PKG_NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $INSTALLED_SIZE
Maintainer: Daniel Rosehill <public@danielrosehill.com>
Description: VoiceType — real-time Linux voice keyboard
 Real-time voice-to-keyboard input using Deepgram Flux STT.
 Features VAD (voice activity detection) to avoid billing for silence,
 PyQt6 GUI with system tray, and no root required.
EOF

# 5. Build the deb
echo "    Packaging ..."
fakeroot dpkg-deb --build "$BUILD_DIR" "$SCRIPT_DIR/dist/$DEB_NAME"

# Cleanup
rm -rf "$BUILD_DIR"

echo "==> Built: dist/$DEB_NAME"
