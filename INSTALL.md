# Installation Guide

This document covers how to install and update VoiceType on your system.

## Quick Install

The easiest way to install VoiceType is using the provided installation script:

```bash
./install.sh
```

This will:
1. Build the release version of the application
2. Create a Debian package
3. Install the package on your system
4. Set up desktop integration

## Requirements

Before installation, ensure you have:

- **Rust & Cargo**: Install from [rustup.rs](https://rustup.rs/)
- **System dependencies**:
  ```bash
  sudo apt install libasound2-dev
  ```
- **Deepgram API Key**: Get one from [Deepgram Console](https://console.deepgram.com/)

## Installation Steps

### 1. Clone the Repository

```bash
git clone https://github.com/danielrosehill/Real-Time-Text-Keyboard.git
cd voicetype-linux
```

### 2. Run the Installer

```bash
./install.sh
```

The script will:
- Install `cargo-deb` if not already present
- Build the release binaries
- Create a `.deb` package
- Install the package system-wide

### 3. Post-Installation

After installation, you can:

**Launch the GUI:**
```bash
voicetype-gui
```

Or find "VoiceType" in your application menu.

**Use the CLI version:**
```bash
sudo -E voicetype --test-stt
```

## Updating

To update to the latest version:

```bash
# Pull the latest changes
git pull

# Run the update script
./update.sh
```

The update script will:
1. Clean previous builds
2. Rebuild the application
3. Create a new package
4. Remove the old version
5. Install the new version

**Note**: If the GUI is running, restart it after updating.

## Development Mode

For development and testing without installing the package:

```bash
./run.sh --test-stt
```

This runs the application directly from the source directory.

## Uninstallation

To remove VoiceType from your system:

```bash
sudo apt remove voicetype
```

Or using dpkg:

```bash
sudo dpkg -r voicetype
```

## Directory Structure

```
voicetype-linux/
├── app/                    # Application source code
│   ├── src/               # Rust source files
│   ├── Cargo.toml         # Package configuration
│   └── target/            # Build artifacts (gitignored)
├── install.sh             # Installation script
├── update.sh              # Update script
├── run.sh                 # Development runner
├── README.md              # General documentation
└── INSTALL.md            # This file
```

## Troubleshooting

### cargo-deb not found

If `cargo-deb` is not installed, the install script will install it automatically. You can also install it manually:

```bash
cargo install cargo-deb
```

### Build fails

Ensure all dependencies are installed:

```bash
sudo apt install libasound2-dev build-essential
```

### Package installation fails

If there are dependency issues:

```bash
sudo apt-get install -f
```

### Permission errors

The scripts should not be run as root. They will use `sudo` internally when needed.

## Manual Installation

If you prefer to install manually:

```bash
cd app
cargo build --release
cargo deb
sudo dpkg -i target/debian/voicetype_*.deb
sudo apt-get install -f
```

## System Integration

The installed package includes:

- **Binaries**: `/usr/bin/voicetype` and `/usr/bin/voicetype-gui`
- **Documentation**: `/usr/share/doc/voicetype/`
- **Desktop file**: `/usr/share/applications/voicetype-gui.desktop`

The GUI stores configuration in:
- `~/.config/voicetype/config.json`
