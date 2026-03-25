# VoiceType GUI

A graphical user interface for VoiceType that makes it easy to control voice dictation with Deepgram.

## Features

- **API Key Management**: Securely store your Deepgram API key in a persistent configuration file
- **Global Hotkey**: Toggle dictation on/off using a customizable hotkey (defaults to F13)
- **Audio Feedback**: Audible beeps when starting and stopping dictation
  - **Start**: Distinctive double beep (bright, high pitch)
  - **Stop**: Single lower pitch beep
- **Visual Feedback**: Clean interface showing current status and recording state
- **One-Click Control**: Start and stop dictation with a single button click

## Installation

### From Debian Package

```bash
sudo dpkg -i voice-keyboard_0.1.0-1_amd64.deb
```

### From Source

```bash
cargo build --release
sudo cp target/release/voice-keyboard-gui /usr/bin/
sudo cp target/release/voice-keyboard /usr/bin/
```

## Usage

### First Time Setup

1. **Launch the GUI**:
   ```bash
   voice-keyboard-gui
   ```
   Or find "Voice Keyboard GUI" in your application menu (Utility → Accessibility)

2. **Configure Your API Key**:
   - Enter your Deepgram API key in the "Deepgram API Key" field
   - Get your API key from: https://developers.deepgram.com/docs/create-additional-api-keys

3. **Set Your Hotkey** (Optional):
   - The default hotkey is F13
   - You can change it to any key (e.g., F13, F14, etc.)

4. **Save Configuration**:
   - Click "Save Configuration" to persist your settings
   - Settings are saved to: `~/.config/deepgram/voice-keyboard/config.json` (Linux)

### Starting Dictation

**Method 1: Use the GUI Button**
- Click the green "Start Dictation" button
- You'll hear a distinctive double beep (high-pitched) indicating dictation has started
- The button will turn red and say "Stop Dictation"

**Method 2: Use the Hotkey**
- Press your configured hotkey (default: F13)
- You'll hear the same double beep
- Works even when the GUI is minimized or in the background

### Stopping Dictation

**Method 1: Use the GUI Button**
- Click the red "Stop Dictation" button
- You'll hear a single low-pitched beep indicating dictation has stopped

**Method 2: Use the Hotkey**
- Press your configured hotkey again (default: F13)
- You'll hear the same low beep

## Audio Feedback

The GUI provides clear audio cues:

- **Start Recording**: Double beep pattern
  - First beep: 1000 Hz (80ms)
  - Short pause: 50ms
  - Second beep: 1200 Hz (80ms) - brighter tone

- **Stop Recording**: Single beep
  - 400 Hz (100ms) - lower, calmer tone

This makes it easy to know the current state without looking at the screen.

## Configuration File

The configuration is stored in JSON format at:
- **Linux**: `~/.config/deepgram/voice-keyboard/config.json`

Example configuration:
```json
{
  "api_key": "your-deepgram-api-key-here",
  "hotkey_code": "F13"
}
```

**Security Note**: The API key is stored in plain text. Protect this file:
```bash
chmod 600 ~/.config/deepgram/voice-keyboard/config.json
```

## Permissions

The voice keyboard requires root privileges to create the virtual keyboard device. When you start dictation, you'll be prompted for your password via `pkexec` (PolicyKit).

This is required because:
- The virtual keyboard needs to create a `/dev/uinput` device
- This requires root access for security reasons
- After creating the device, privileges are dropped to access your audio system

## Troubleshooting

### "Failed to start" Error

1. **Check your API key**: Ensure your Deepgram API key is correct and saved
2. **Verify permissions**: Make sure you have `pkexec` installed
3. **Test the core binary**: Try running the voice-keyboard directly:
   ```bash
   export DEEPGRAM_API_KEY="your-key-here"
   sudo -E ./target/release/voice-keyboard --test-stt
   ```

### No Sound / Beeps Not Working

1. **Check audio output**: Ensure your system audio is working and not muted
2. **Volume levels**: The beeps play at 30-35% volume - increase system volume if needed
3. **Audio system**: Make sure PipeWire or PulseAudio is running

### Hotkey Not Working

1. **Key availability**: Ensure your keyboard has the configured key (e.g., F13)
2. **Key conflicts**: Check if another application is using the same hotkey
3. **Try a different key**: Some keys may not be available as global hotkeys
4. **Permissions**: Some desktop environments require special permissions for global hotkeys

### No Text Appearing

1. **Check microphone**: Ensure your microphone is working and selected correctly
2. **Test audio input**: Run `voice-keyboard --test-audio` to verify microphone levels
3. **Deepgram API**: Verify your API key is valid and has credits
4. **Focus**: Make sure the target application is focused and accepts text input

### Build Errors

If you encounter build errors:

```bash
# Update Rust
rustup update

# Install required system dependencies (Ubuntu/Debian)
sudo apt install libasound2-dev

# Install required system dependencies (Fedora/RHEL)
sudo dnf install alsa-lib-devel

# Clean and rebuild
cargo clean
cargo build --release
```

## Technical Details

- **GUI Framework**: iced (native Rust GUI)
- **Global Hotkeys**: global-hotkey crate
- **Audio Playback**: rodio (for beep sounds)
- **Configuration Storage**: serde + JSON
- **Platform**: Linux (tested on Ubuntu with KDE Plasma/Wayland)

## Command Line Interface

The core `voice-keyboard` binary also supports command-line options:

```bash
voice-keyboard [OPTIONS]

OPTIONS:
    --test-audio        Test audio input and show levels
    --test-stt          Test speech-to-text functionality (default)
    --debug-stt         Debug speech-to-text (print transcripts without typing)
    --stt-url <URL>     Custom STT service URL
    --voice-enter       Interpret "enter" at end-of-turn as Enter key
    --uppercase         Convert all typed text to uppercase
```

## Contributing

This is a demonstration application for Deepgram's Flux API. For issues or improvements, please check the project repository:
https://github.com/danielrosehill/deepgram-voice-keyboard

## License

ISC License - See LICENSE.txt in the project root
