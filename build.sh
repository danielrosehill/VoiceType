#!/usr/bin/env bash
#
# VoiceType — Build & Install Script
# Usage:
#   ./build.sh              Build and install the .deb package
#   ./build.sh --update     Remove old version first, then rebuild and install
#   ./build.sh --build-only Just build the .deb without installing
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/app"
DEB_DIR="$APP_DIR/target/debian"

ACTION="install"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --update)     ACTION="update"; shift ;;
        --build-only) ACTION="build";  shift ;;
        -h|--help)
            echo "Usage: $0 [--update | --build-only]"
            echo "  (none)       Build and install"
            echo "  --update     Remove old package, rebuild, install"
            echo "  --build-only Build .deb only (no install)"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Sanity checks
if [[ "$EUID" -eq 0 ]]; then
    echo "Error: Don't run as root. The script uses sudo when needed."
    exit 1
fi

if ! command -v cargo &>/dev/null; then
    echo "Error: Rust/Cargo not found. Install from https://rustup.rs/"
    exit 1
fi

# Ensure cargo-deb is available
if ! cargo install --list 2>/dev/null | grep -q "^cargo-deb "; then
    echo "Installing cargo-deb..."
    cargo install cargo-deb
fi

cd "$APP_DIR"

# Clean on update to ensure fresh build
if [[ "$ACTION" == "update" ]]; then
    echo "Cleaning previous build..."
    cargo clean
fi

# Build
VERSION=$(grep '^version' Cargo.toml | head -1 | sed 's/.*"\(.*\)"/\1/')
echo "Building voicetype v${VERSION} (release)..."
cargo build --release

echo "Creating Debian package..."
cargo deb --no-build  # binary already built above

# Find the .deb
DEB_FILE=$(find "$DEB_DIR" -name "*.deb" -type f -printf '%T@ %p\n' | sort -rn | head -1 | cut -d' ' -f2)
if [[ -z "$DEB_FILE" ]]; then
    echo "Error: No .deb file found in $DEB_DIR"
    exit 1
fi

echo ""
echo "Built: $(basename "$DEB_FILE")"
echo "  Size: $(du -h "$DEB_FILE" | cut -f1)"
echo "  Path: $DEB_FILE"

if [[ "$ACTION" == "build" ]]; then
    echo ""
    echo "Done (build only). Install with:"
    echo "  sudo dpkg -i $DEB_FILE"
    exit 0
fi

# Remove old version on update
if [[ "$ACTION" == "update" ]]; then
    if dpkg -l voicetype &>/dev/null 2>&1; then
        echo ""
        echo "Removing old version..."
        sudo dpkg -r voicetype
    fi
fi

echo ""
echo "Installing..."
sudo dpkg -i "$DEB_FILE"
sudo apt-get install -f -y 2>/dev/null || true

echo ""
echo "Installed voicetype v${VERSION}"
echo ""
echo "Commands:"
echo "  voicetype       CLI (requires sudo)"
echo "  voicetype-gui   GUI application"
echo ""
echo "Or find 'VoiceType' in your application menu."
