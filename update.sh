#!/usr/bin/env bash

# VoiceType Update Script
# Rebuilds and updates the installed Debian package

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/app"
DEB_DIR="$APP_DIR/target/debian"

echo "=========================================="
echo "VoiceType Update Script"
echo "=========================================="
echo ""

# Check if running as root
if [ "$EUID" -eq 0 ]; then
  echo "Error: Don't run this script as root. It will use sudo when needed."
  exit 1
fi

# Check if the package is installed
if ! dpkg -l | grep -q "voicetype"; then
  echo "Error: voicetype is not currently installed."
  echo "Run ./install.sh first to install the package."
  exit 1
fi

# Check for cargo
if ! command -v cargo &> /dev/null; then
  echo "Error: Rust/Cargo is not installed."
  echo "Install from: https://rustup.rs/"
  exit 1
fi

# Check for cargo-deb
if ! cargo install --list | grep -q "^cargo-deb "; then
  echo "Installing cargo-deb..."
  cargo install cargo-deb
fi

# Navigate to app directory
cd "$APP_DIR"

# Clean previous build artifacts
echo "Cleaning previous builds..."
cargo clean

echo ""
echo "Building release version..."
cargo build --release

if [ $? -ne 0 ]; then
  echo "Build failed!"
  exit 1
fi

echo ""
echo "Creating Debian package..."
cargo deb

if [ $? -ne 0 ]; then
  echo "Debian package creation failed!"
  exit 1
fi

# Find the generated .deb file
DEB_FILE=$(find "$DEB_DIR" -name "*.deb" -type f | head -n 1)

if [ -z "$DEB_FILE" ]; then
  echo "Error: Could not find generated .deb file"
  exit 1
fi

echo ""
echo "Found package: $(basename "$DEB_FILE")"
echo ""
echo "Removing old version..."
sudo dpkg -r voicetype

echo ""
echo "Installing updated package..."
sudo dpkg -i "$DEB_FILE"

# Fix any dependency issues
echo ""
echo "Fixing dependencies if needed..."
sudo apt-get install -f -y

echo ""
echo "=========================================="
echo "Update complete!"
echo "=========================================="
echo ""
echo "Changes have been applied."
echo "If voicetype-gui is running, restart it to use the new version."
echo ""
