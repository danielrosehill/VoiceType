use anyhow::{Context, Result};
use clap::{Arg, Command};
use nix::unistd::{getgid, getuid, setgid, setuid, Gid, Uid};
use std::env;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;
use tracing::{debug, error, info};

mod audio_input;
mod input_event;
mod stt_client;
mod virtual_keyboard;

use audio_input::AudioInput;
use stt_client::{AudioBuffer, SttClient};
use virtual_keyboard::{RealKeyboardHardware, VirtualKeyboard};
use std::time::Instant;

#[derive(Debug)]
struct OriginalUser {
    uid: Uid,
    gid: Gid,
    home: Option<String>,
    user: Option<String>,
}

impl OriginalUser {
    fn capture() -> Self {
        // If we're running under sudo, get the original user info
        let uid = if let Ok(sudo_uid) = env::var("SUDO_UID") {
            Uid::from_raw(sudo_uid.parse().unwrap_or_else(|_| getuid().as_raw()))
        } else {
            getuid()
        };

        let gid = if let Ok(sudo_gid) = env::var("SUDO_GID") {
            Gid::from_raw(sudo_gid.parse().unwrap_or_else(|_| getgid().as_raw()))
        } else {
            getgid()
        };

        let home = env::var("HOME").ok();
        let user = env::var("SUDO_USER").ok().or_else(|| env::var("USER").ok());

        Self {
            uid,
            gid,
            home,
            user,
        }
    }

    fn drop_privileges(&self) -> Result<()> {
        if getuid().is_root() {
            debug!(
                "Dropping root privileges to uid={}, gid={}",
                self.uid, self.gid
            );

            // Preserve important environment variables
            let pulse_runtime_path = env::var("PULSE_RUNTIME_PATH").ok();
            let xdg_runtime_dir = env::var("XDG_RUNTIME_DIR").ok();
            let display = env::var("DISPLAY").ok();
            let wayland_display = env::var("WAYLAND_DISPLAY").ok();

            // Drop group first, then user (required order)
            setgid(self.gid).context("Failed to drop group privileges")?;
            setuid(self.uid).context("Failed to drop user privileges")?;

            // Restore environment variables for the original user
            // Safety: called during single-threaded startup before spawning async tasks
            unsafe {
                if let Some(ref home) = self.home {
                    env::set_var("HOME", home);
                }
                if let Some(ref user) = self.user {
                    env::set_var("USER", user);
                }

                // Restore audio-related environment variables
                if let Some(pulse_path) = pulse_runtime_path {
                    env::set_var("PULSE_RUNTIME_PATH", pulse_path);
                }
                if let Some(xdg_path) = xdg_runtime_dir {
                    env::set_var("XDG_RUNTIME_DIR", xdg_path);
                }
                if let Some(disp) = display {
                    env::set_var("DISPLAY", disp);
                }
                if let Some(wayland_disp) = wayland_display {
                    env::set_var("WAYLAND_DISPLAY", wayland_disp);
                }
            }

            debug!("Successfully dropped privileges to user");

            // Give audio system a moment to be ready
            std::thread::sleep(std::time::Duration::from_millis(100));
        } else {
            debug!("Not running as root, no privilege dropping needed");
        }

        Ok(())
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();

    info!("Starting VoiceType v{}", env!("CARGO_PKG_VERSION"));

    // Capture original user info before we do anything
    let original_user = OriginalUser::capture();

    let matches = Command::new("voicetype")
        .version(env!("CARGO_PKG_VERSION"))
        .about("VoiceType — real-time voice keyboard input")
        .arg(
            Arg::new("test-audio")
                .long("test-audio")
                .help("Test audio input")
                .action(clap::ArgAction::SetTrue),
        )
        .arg(
            Arg::new("test-stt")
                .long("test-stt")
                .help("Test speech-to-text functionality")
                .action(clap::ArgAction::SetTrue),
        )
        .arg(
            Arg::new("debug-stt")
                .long("debug-stt")
                .help("Debug speech-to-text (print transcripts without typing)")
                .action(clap::ArgAction::SetTrue),
        )
        .arg(
            Arg::new("stt-url")
                .long("stt-url")
                .help("Custom STT service URL")
                .value_name("URL"),
        )
        .arg(
            Arg::new("voice-enter")
                .long("voice-enter")
                .help("Interpret the word 'enter' at end-of-turn as an Enter key press")
                .action(clap::ArgAction::SetTrue),
        )
        .arg(
            Arg::new("uppercase")
                .long("uppercase")
                .help("Convert all typed text to uppercase")
                .action(clap::ArgAction::SetTrue),
        )
        .arg(
            Arg::new("json-output")
                .long("json-output")
                .help("Print transcript events as JSON lines to stdout (for GUI)")
                .action(clap::ArgAction::SetTrue),
        )
        .get_matches();

    let device_name = "VoiceType";

    // Step 1: Create virtual keyboard while we have root privileges
    debug!("Creating virtual keyboard device (requires root privileges)...");
    let hardware =
        RealKeyboardHardware::new(device_name).context("Failed to create keyboard hardware")?;
    let mut keyboard = VirtualKeyboard::new(hardware);
    let voice_enter_enabled = matches.get_flag("voice-enter");
    let uppercase_enabled = matches.get_flag("uppercase");
    keyboard.set_voice_enter_enabled(voice_enter_enabled);
    keyboard.set_uppercase_enabled(uppercase_enabled);
    debug!("Virtual keyboard created successfully");

    // Step 2: Drop root privileges before initializing audio
    original_user
        .drop_privileges()
        .context("Failed to drop root privileges")?;

    if matches.get_flag("test-audio") {
        test_audio().await?;
    } else if matches.get_flag("test-stt") {
        let stt_url = matches
            .get_one::<String>("stt-url")
            .map(|s| s.as_str())
            .unwrap_or(stt_client::STT_URL);
        test_stt(keyboard, stt_url, false).await?;
    } else {
        let debug_mode = matches.get_flag("debug-stt");
        let stt_url = matches
            .get_one::<String>("stt-url")
            .map(|s| s.as_str())
            .unwrap_or(stt_client::STT_URL);

        let json_output = matches.get_flag("json-output");

        if debug_mode {
            debug_stt(stt_url).await?;
        } else {
            test_stt(keyboard, stt_url, json_output).await?;
        }
    }

    Ok(())
}

async fn test_audio() -> Result<()> {
    info!("Testing audio input...");

    // List available devices
    info!("Available input devices:");
    let devices = AudioInput::list_available_devices()?;
    for (i, device) in devices.iter().enumerate() {
        info!("  {}: {}", i + 1, device);
    }

    // Create audio input
    let mut audio_input = AudioInput::new()?;
    debug!(
        "Using audio device with {} channels at {} Hz",
        audio_input.get_channels(),
        audio_input.get_sample_rate()
    );

    // Test recording for 5 seconds
    let (tx, rx) = mpsc::channel();

    audio_input.start_recording(move |data| {
        let level = data.iter().map(|&x| x.abs()).sum::<f32>() / data.len() as f32;
        let _ = tx.send(level);
    })?;

    info!("Recording for 5 seconds...");
    let start = std::time::Instant::now();

    while start.elapsed() < Duration::from_secs(5) {
        if let Ok(level) = rx.try_recv() {
            let bar_length = (level * 50.0) as usize;
            let bar: String = "#".repeat(bar_length);
            info!("Level: {:.2} [{}]", level, bar);
        }
        thread::sleep(Duration::from_millis(50));
    }

    info!("Audio test completed!");
    Ok(())
}

async fn test_stt(keyboard: VirtualKeyboard<RealKeyboardHardware>, stt_url: &str, json_output: bool) -> Result<()> {
    info!("Testing speech-to-text functionality...");

    // Wrap keyboard in a mutex to allow mutable access from the closure
    let keyboard = std::sync::Arc::new(std::sync::Mutex::new(keyboard));
    let keyboard_clone = keyboard.clone();
    // Rate-limit plain Update logs to at most once per second
    let last_update_log = std::sync::Arc::new(std::sync::Mutex::new(None::<Instant>));
    let last_update_log_cloned = last_update_log.clone();

    run_stt(stt_url, move |result| {
        // Emit JSON line to stdout for GUI consumption
        if json_output {
            if let Ok(json) = serde_json::to_string(&result) {
                println!("{}", json);
            }
        }

        if !result.transcript.is_empty() {
            if result.event == "Update" {
                let now = Instant::now();
                let mut last = last_update_log_cloned.lock().unwrap();
                let should_log = match *last {
                    Some(prev) => now.duration_since(prev) >= Duration::from_secs(1),
                    None => true,
                };
                if should_log {
                    info!("Transcription [{}]: {}", result.event, result.transcript);
                    *last = Some(now);
                }
            } else {
                // Always log non-Update events (StartOfTurn, EagerEndOfTurn, TurnResumed, EndOfTurn)
                info!("Transcription [{}]: {}", result.event, result.transcript);
            }
        }

        let mut kb = keyboard_clone.lock().unwrap();

        // Handle different event types
        match result.event.as_str() {
            "EndOfTurn" => {
                // Finalize the transcript; treat failure as fatal
                if let Err(e) = kb.finalize_transcript() {
                    error!("Failed to finalize transcript: {}", e);
                    std::process::exit(1);
                }
            }
            _ => {
                // Handle incremental updates; treat failure as fatal
                if let Err(e) = kb.update_transcript(&result.transcript) {
                    error!("Failed to update transcript: {}", e);
                    std::process::exit(1);
                }
            }
        }
    }).await
}

async fn debug_stt(stt_url: &str) -> Result<()> {
    info!("Debugging speech-to-text functionality...");
    info!("STT Service URL: {}", stt_url);

    run_stt(stt_url, |result| {
        // Only show non-empty transcriptions
        if !result.transcript.is_empty() {
            info!("Transcription [{}]: {}", result.event, result.transcript);
        }
    })
    .await
}

async fn run_stt<F>(stt_url: &str, on_transcription: F) -> Result<()>
where
    F: Fn(stt_client::TranscriptionResult) + Send + 'static,
{
    // Orphan detection: if our parent process dies (e.g. GUI kills pkexec),
    // we get reparented to PID 1. Check periodically and exit cleanly.
    let original_ppid = unsafe { libc::getppid() };
    thread::spawn(move || {
        loop {
            thread::sleep(Duration::from_secs(1));
            let current_ppid = unsafe { libc::getppid() };
            if current_ppid != original_ppid {
                info!("Parent process died (was {}, now {}), exiting", original_ppid, current_ppid);
                std::process::exit(0);
            }
        }
    });

    let mut audio_input = AudioInput::new()?;
    debug!(
        "Using audio device with {} channels at {} Hz",
        audio_input.get_channels(),
        audio_input.get_sample_rate()
    );

    let mut audio_buffer = AudioBuffer::new(audio_input.get_sample_rate(), 160);
    let stt_client = SttClient::new(stt_url, audio_input.get_sample_rate());

    info!(?stt_url, "Connecting to STT service...");
    let (audio_tx, handle) = stt_client
        .connect_and_transcribe(on_transcription)
        .await
        .context("Failed to connect to STT service")?;

    info!("Listening for speech... Speak into your microphone!");
    info!("Press Ctrl+C to stop.");

    let audio_tx = std::sync::Arc::new(audio_tx);
    let audio_tx_clone = audio_tx.clone();
    let channels = audio_input.get_channels();

    // Start recording
    audio_input.start_recording(move |data| {
        debug!("Received audio data: {} samples", data.len());

        // Average stereo channels to mono
        let mono_data: Vec<f32> = if channels == 2 {
            let mut mono = Vec::with_capacity(data.len() / 2);
            for chunk in data.chunks_exact(2) {
                mono.push((chunk[0] + chunk[1]) / 2.0);
            }
            debug!("Averaged samples: {}", mono.len());
            mono
        } else {
            data.to_vec()
        };

        // Create audio chunks and send them (non-blocking to avoid stalling the audio thread)
        let chunks = audio_buffer.add_samples(&mono_data);
        for chunk in chunks {
            debug!("Sending audio chunk: {} bytes", chunk.len());
            match audio_tx_clone.try_send(chunk) {
                Ok(_) => {}
                Err(tokio::sync::mpsc::error::TrySendError::Full(_)) => {
                    debug!("Audio channel full, dropping chunk");
                }
                Err(e) => {
                    error!("Failed to send audio chunk: {}", e);
                }
            }
        }
    })?;

    // Just wait for the STT client to finish (will be interrupted by Ctrl+C)
    handle.await??;

    Ok(())
}
