use anyhow::{Context, Result};
use directories::ProjectDirs;
use global_hotkey::{
    hotkey::{Code, HotKey},
    GlobalHotKeyEvent, GlobalHotKeyManager,
};
use iced::{
    time,
    widget::{button, column, container, horizontal_rule, row, scrollable, text, text_input, Space},
    Color, Element, Font, Length, Subscription, Task, Theme,
};
use rodio::{source::SineWave, OutputStream, Sink, Source};
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{BufRead, BufReader};
use std::os::unix::io::AsRawFd;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use reqwest::Client;

// ── Config ──────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Config {
    api_key: String,
    hotkey_code: String,
    project_id: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            api_key: String::new(),
            hotkey_code: "F13".to_string(),
            project_id: String::new(),
        }
    }
}

impl Config {
    fn config_path() -> Result<PathBuf> {
        let project_dirs = ProjectDirs::from("com", "voicetype", "voicetype")
            .context("Failed to get project directories")?;
        let config_dir = project_dirs.config_dir();
        fs::create_dir_all(config_dir)?;
        Ok(config_dir.join("config.json"))
    }

    fn load() -> Result<Self> {
        let path = Self::config_path()?;
        if path.exists() {
            let contents = fs::read_to_string(&path)?;
            Ok(serde_json::from_str(&contents)?)
        } else {
            Ok(Self::default())
        }
    }

    fn save(&self) -> Result<()> {
        use std::os::unix::fs::OpenOptionsExt;
        let path = Self::config_path()?;
        let contents = serde_json::to_string_pretty(self)?;
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&path)?;
        use std::io::Write;
        file.write_all(contents.as_bytes())?;
        Ok(())
    }
}

// ── Billing ─────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct BillingBalance {
    #[allow(dead_code)]
    balance_id: String,
    amount: f64,
    units: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct BillingResponse {
    balances: Vec<BillingBalance>,
}

// ── Transcript event from JSON output ───────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct TranscriptEvent {
    event: String,
    transcript: String,
    #[serde(default)]
    end_of_turn_confidence: f64,
}

// ── Application state ───────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq)]
enum AppView {
    Main,
    Settings,
}

#[derive(Debug, Clone)]
enum Message {
    // Navigation
    SwitchView(AppView),
    // Dictation
    ToggleDictation,
    #[allow(dead_code)]
    TranscriptLine(String),
    // Settings
    ApiKeyChanged(String),
    HotkeyChanged(String),
    ProjectIdChanged(String),
    SaveConfig,
    // Billing
    CheckBalance,
    BalanceReceived(Result<BillingResponse, String>),
    // Hotkey triggered from background thread
    #[allow(dead_code)]
    HotkeyTriggered,
    // Periodic tick to read child stdout
    Tick,
}

struct VoiceKeyboardGui {
    config: Config,
    view: AppView,
    // Settings inputs
    api_key_input: String,
    hotkey_input: String,
    project_id_input: String,
    // Dictation state
    is_recording: bool,
    status_message: String,
    transcript_lines: Vec<String>,
    current_transcript: String,
    // Billing
    balance_info: String,
    // Process management
    voice_keyboard_process: Arc<Mutex<Option<Child>>>,
    child_stdout: Arc<Mutex<Option<BufReader<std::process::ChildStdout>>>>,
    // Audio
    _audio_output_stream: OutputStream,
    audio_sink: Arc<Mutex<Sink>>,
    // Hotkey
    _hotkey_manager: GlobalHotKeyManager,
    // HTTP
    http_client: Client,
    // Hotkey receiver
    hotkey_rx: Arc<Mutex<Option<std::sync::mpsc::Receiver<()>>>>,
}

impl Drop for VoiceKeyboardGui {
    fn drop(&mut self) {
        if let Some(mut child) = self.voice_keyboard_process.lock().unwrap().take() {
            let _ = child.kill();
            let start = std::time::Instant::now();
            while start.elapsed() < Duration::from_secs(1) {
                match child.try_wait() {
                    Ok(Some(_)) => break,
                    Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                    Err(_) => break,
                }
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

// ── Styling helpers (light theme) ────────────────────────────────────────

const ACCENT: Color = Color {
    r: 0.176,
    g: 0.447,
    b: 0.882,
    a: 1.0,
};

const RECORDING_RED: Color = Color {
    r: 0.847,
    g: 0.212,
    b: 0.212,
    a: 1.0,
};

const SUCCESS_GREEN: Color = Color {
    r: 0.180,
    g: 0.600,
    b: 0.302,
    a: 1.0,
};

// Card / header background — light gray
const SURFACE: Color = Color {
    r: 0.945,
    g: 0.949,
    b: 0.957,
    a: 1.0,
};

// Secondary surfaces / borders
const SURFACE_LIGHT: Color = Color {
    r: 0.898,
    g: 0.906,
    b: 0.918,
    a: 1.0,
};

// Muted text
const TEXT_DIM: Color = Color {
    r: 0.420,
    g: 0.447,
    b: 0.494,
    a: 1.0,
};

// Primary text
const TEXT_PRIMARY: Color = Color {
    r: 0.118,
    g: 0.133,
    b: 0.165,
    a: 1.0,
};

// App background — white
const BG: Color = Color::WHITE;

fn record_button_style(
    recording: bool,
) -> impl Fn(&Theme, button::Status) -> button::Style {
    move |_theme: &Theme, _status: button::Status| {
        let bg_color = if recording { RECORDING_RED } else { SUCCESS_GREEN };
        button::Style {
            background: Some(iced::Background::Color(bg_color)),
            text_color: Color::WHITE,
            border: iced::Border {
                radius: 8.0.into(),
                ..Default::default()
            },
            ..button::Style::default()
        }
    }
}

fn nav_button_style(active: bool) -> impl Fn(&Theme, button::Status) -> button::Style {
    move |_theme: &Theme, _status: button::Status| {
        let bg = if active { ACCENT } else { SURFACE_LIGHT };
        let text_color = if active { Color::WHITE } else { TEXT_DIM };
        button::Style {
            background: Some(iced::Background::Color(bg)),
            text_color,
            border: iced::Border {
                radius: 6.0.into(),
                ..Default::default()
            },
            ..button::Style::default()
        }
    }
}

fn secondary_button_style(_theme: &Theme, _status: button::Status) -> button::Style {
    button::Style {
        background: Some(iced::Background::Color(ACCENT)),
        text_color: Color::WHITE,
        border: iced::Border {
            radius: 6.0.into(),
            ..Default::default()
        },
        ..button::Style::default()
    }
}

fn card_style(_theme: &Theme) -> container::Style {
    container::Style {
        background: Some(iced::Background::Color(SURFACE)),
        border: iced::Border {
            radius: 10.0.into(),
            ..Default::default()
        },
        ..container::Style::default()
    }
}

fn transcript_area_style(_theme: &Theme) -> container::Style {
    container::Style {
        background: Some(iced::Background::Color(SURFACE)),
        border: iced::Border {
            radius: 8.0.into(),
            color: SURFACE_LIGHT,
            width: 1.0,
        },
        ..container::Style::default()
    }
}

// ── Application logic ───────────────────────────────────────────────────

impl VoiceKeyboardGui {
    fn new() -> (Self, Task<Message>) {
        let config = Config::load().unwrap_or_default();
        let api_key_input = config.api_key.clone();
        let hotkey_input = config.hotkey_code.clone();
        let project_id_input = config.project_id.clone();

        // Set API key from saved config
        if !config.api_key.is_empty() {
            unsafe { std::env::set_var("DEEPGRAM_API_KEY", &config.api_key) };
        }

        // Audio
        let (stream, stream_handle) = OutputStream::try_default().unwrap();
        let sink = Sink::try_new(&stream_handle).unwrap();

        // Hotkey
        let hotkey_manager = GlobalHotKeyManager::new().unwrap();
        let hotkey = HotKey::new(None, Code::F13);
        hotkey_manager.register(hotkey).ok();

        // Channel for hotkey events
        let (hotkey_tx, hotkey_rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let receiver = GlobalHotKeyEvent::receiver();
            loop {
                if receiver.recv().is_ok() {
                    let _ = hotkey_tx.send(());
                }
            }
        });

        let gui = Self {
            config,
            view: AppView::Main,
            api_key_input,
            hotkey_input,
            project_id_input,
            is_recording: false,
            status_message: "Ready — press F13 or click Start".to_string(),
            transcript_lines: Vec::new(),
            current_transcript: String::new(),
            balance_info: String::new(),
            voice_keyboard_process: Arc::new(Mutex::new(None)),
            child_stdout: Arc::new(Mutex::new(None)),
            _audio_output_stream: stream,
            audio_sink: Arc::new(Mutex::new(sink)),
            _hotkey_manager: hotkey_manager,
            http_client: Client::new(),
            hotkey_rx: Arc::new(Mutex::new(Some(hotkey_rx))),
        };

        // Start a subscription-like tick to poll hotkey and child stdout
        let task = Task::none();

        (gui, task)
    }

    fn play_start_beep(&self) {
        if let Ok(sink) = self.audio_sink.lock() {
            let beep1 = SineWave::new(1000.0)
                .take_duration(Duration::from_millis(80))
                .amplify(0.35);
            sink.append(beep1);
            let beep2 = SineWave::new(1200.0)
                .take_duration(Duration::from_millis(80))
                .amplify(0.35);
            sink.append(beep2);
        }
    }

    fn play_stop_beep(&self) {
        if let Ok(sink) = self.audio_sink.lock() {
            let source = SineWave::new(400.0)
                .take_duration(Duration::from_millis(100))
                .amplify(0.3);
            sink.append(source);
        }
    }

    fn start_dictation(&mut self) {
        if self.config.api_key.is_empty() {
            self.status_message = "No API key — go to Settings".to_string();
            return;
        }

        unsafe { std::env::set_var("DEEPGRAM_API_KEY", &self.config.api_key) };

        self.play_start_beep();
        self.current_transcript.clear();

        let exe_path = std::env::current_exe()
            .unwrap()
            .parent()
            .unwrap()
            .join("voicetype");

        let mut cmd = Command::new("pkexec");
        cmd.arg("env")
            .arg(format!("DEEPGRAM_API_KEY={}", &self.config.api_key));

        // Preserve environment variables
        for var in &[
            "PULSE_RUNTIME_PATH",
            "XDG_RUNTIME_DIR",
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "HOME",
            "USER",
        ] {
            if let Ok(val) = std::env::var(var) {
                cmd.arg(format!("{}={}", var, val));
            }
        }

        cmd.arg(&exe_path)
            .arg("--test-stt")
            .arg("--json-output")
            .stdout(Stdio::piped())
            .stderr(Stdio::null());

        match cmd.spawn() {
            Ok(mut child) => {
                let stdout = child.stdout.take();
                // Set stdout pipe to non-blocking so read_line won't freeze the GUI
                if let Some(ref stdout) = stdout {
                    let fd = stdout.as_raw_fd();
                    unsafe {
                        let flags = libc::fcntl(fd, libc::F_GETFL);
                        libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK);
                    }
                }
                *self.voice_keyboard_process.lock().unwrap() = Some(child);
                *self.child_stdout.lock().unwrap() =
                    stdout.map(BufReader::new);
                self.is_recording = true;
                self.status_message = "Listening...".to_string();
            }
            Err(e) => {
                self.status_message = format!("Failed to start: {}", e);
            }
        }
    }

    fn stop_dictation(&mut self) {
        self.play_stop_beep();

        // Drop stdout reader first — this closes the read end of the pipe,
        // causing voice-keyboard's next stdout write to get EPIPE and exit
        *self.child_stdout.lock().unwrap() = None;

        if let Some(mut child) = self.voice_keyboard_process.lock().unwrap().take() {
            // Kill the pkexec wrapper
            let _ = child.kill();
            let _ = child.wait();
            // The voice-keyboard root child will self-terminate via orphan detection
            // (parent PID becomes 1 when pkexec dies)
        }

        // Finalize current transcript
        if !self.current_transcript.is_empty() {
            self.transcript_lines
                .push(self.current_transcript.clone());
            self.current_transcript.clear();
        }

        self.is_recording = false;
        self.status_message = "Stopped".to_string();
    }

    fn read_child_output(&mut self) {
        let mut stdout_lock = self.child_stdout.lock().unwrap();
        if let Some(reader) = stdout_lock.as_mut() {
            let mut line = String::new();
            // Non-blocking: read available lines
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) => break, // EOF
                    Ok(_) => {
                        let trimmed = line.trim();
                        if trimmed.is_empty() {
                            continue;
                        }
                        if let Ok(evt) = serde_json::from_str::<TranscriptEvent>(trimmed) {
                            match evt.event.as_str() {
                                "EndOfTurn" => {
                                    if !self.current_transcript.is_empty() {
                                        self.transcript_lines
                                            .push(self.current_transcript.clone());
                                    }
                                    self.current_transcript.clear();
                                    // Keep last 50 lines
                                    if self.transcript_lines.len() > 50 {
                                        self.transcript_lines.drain(0..self.transcript_lines.len() - 50);
                                    }
                                }
                                "Update" | "StartOfTurn" | "EagerEndOfTurn" | "TurnResumed" => {
                                    self.current_transcript = evt.transcript;
                                }
                                _ => {}
                            }
                        }
                    }
                    Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => break, // No data available yet
                    Err(_) => break,
                }
            }
        }

        // Check if child process has exited
        let mut proc_lock = self.voice_keyboard_process.lock().unwrap();
        if let Some(child) = proc_lock.as_mut() {
            match child.try_wait() {
                Ok(Some(_status)) => {
                    // Process exited
                    drop(proc_lock);
                    *self.child_stdout.lock().unwrap() = None;
                    self.is_recording = false;
                    self.status_message = "Ready — press F13 or click Start".to_string();
                }
                _ => {}
            }
        }
    }

    fn update(&mut self, message: Message) -> Task<Message> {
        match message {
            Message::SwitchView(view) => {
                self.view = view;
            }
            Message::ToggleDictation | Message::HotkeyTriggered => {
                if self.is_recording {
                    self.stop_dictation();
                } else {
                    self.start_dictation();
                }
            }
            Message::TranscriptLine(line) => {
                self.transcript_lines.push(line);
            }
            Message::Tick => {
                // Check hotkey — collect events first, then act
                let mut hotkey_triggered = false;
                if let Ok(rx_lock) = self.hotkey_rx.lock() {
                    if let Some(rx) = rx_lock.as_ref() {
                        while rx.try_recv().is_ok() {
                            hotkey_triggered = true;
                        }
                    }
                }
                if hotkey_triggered {
                    if self.is_recording {
                        self.stop_dictation();
                    } else {
                        self.start_dictation();
                    }
                }
                // Read child output
                if self.is_recording {
                    self.read_child_output();
                }
            }
            Message::ApiKeyChanged(value) => {
                self.api_key_input = value;
            }
            Message::HotkeyChanged(value) => {
                self.hotkey_input = value;
            }
            Message::ProjectIdChanged(value) => {
                self.project_id_input = value;
            }
            Message::SaveConfig => {
                self.config.api_key = self.api_key_input.clone();
                self.config.hotkey_code = self.hotkey_input.clone();
                self.config.project_id = self.project_id_input.clone();
                match self.config.save() {
                    Ok(_) => {
                        unsafe {
                            std::env::set_var("DEEPGRAM_API_KEY", &self.config.api_key);
                        }
                        self.status_message = "Settings saved".to_string();
                    }
                    Err(e) => {
                        self.status_message = format!("Save failed: {}", e);
                    }
                }
            }
            Message::CheckBalance => {
                let api_key = self.config.api_key.clone();
                let project_id = self.config.project_id.clone();
                let client = self.http_client.clone();

                if project_id.is_empty() || api_key.is_empty() {
                    self.balance_info = "Set API key and Project ID first".to_string();
                    return Task::none();
                }

                self.balance_info = "Checking...".to_string();

                return Task::future(async move {
                    let url = format!(
                        "https://api.deepgram.com/v1/projects/{}/balances",
                        project_id
                    );
                    let result = client
                        .get(&url)
                        .header("Authorization", format!("Token {}", api_key))
                        .send()
                        .await;

                    match result {
                        Ok(response) => {
                            if response.status().is_success() {
                                match response.json::<BillingResponse>().await {
                                    Ok(billing) => Message::BalanceReceived(Ok(billing)),
                                    Err(e) => {
                                        Message::BalanceReceived(Err(format!("Parse error: {}", e)))
                                    }
                                }
                            } else {
                                Message::BalanceReceived(Err(format!(
                                    "API error: {}",
                                    response.status()
                                )))
                            }
                        }
                        Err(e) => Message::BalanceReceived(Err(format!("Request failed: {}", e))),
                    }
                });
            }
            Message::BalanceReceived(result) => match result {
                Ok(billing) => {
                    if billing.balances.is_empty() {
                        self.balance_info = "No balance data".to_string();
                    } else {
                        self.balance_info = billing
                            .balances
                            .iter()
                            .map(|b| format!("${:.2} ({})", b.amount, b.units))
                            .collect::<Vec<_>>()
                            .join("  |  ");
                    }
                }
                Err(e) => {
                    self.balance_info = format!("Error: {}", e);
                }
            },
        }
        Task::none()
    }

    fn subscription(&self) -> Subscription<Message> {
        time::every(Duration::from_millis(50)).map(|_| Message::Tick)
    }

    // ── Views ───────────────────────────────────────────────────────────

    fn view(&self) -> Element<Message> {
        let content = column![
            self.view_header(),
            self.view_nav(),
            match self.view {
                AppView::Main => self.view_main(),
                AppView::Settings => self.view_settings(),
            }
        ]
        .spacing(0);

        container(content)
            .width(Length::Fill)
            .height(Length::Fill)
            .style(|_theme: &Theme| container::Style {
                background: Some(iced::Background::Color(BG)),
                ..container::Style::default()
            })
            .into()
    }

    fn view_header(&self) -> Element<Message> {
        let status_color = if self.is_recording {
            RECORDING_RED
        } else {
            TEXT_DIM
        };

        let status_dot = text(if self.is_recording { "●" } else { "○" })
            .size(14)
            .color(status_color);

        let title = text("VoiceType")
            .size(22)
            .font(Font::DEFAULT)
            .color(TEXT_PRIMARY);

        let status = text(&self.status_message)
            .size(13)
            .color(status_color);

        container(
            row![
                status_dot,
                title,
                Space::with_width(Length::Fill),
                status,
            ]
            .spacing(10)
            .align_y(iced::Alignment::Center),
        )
        .padding(iced::Padding::from([14, 20]))
        .width(Length::Fill)
        .style(|_theme: &Theme| container::Style {
            background: Some(iced::Background::Color(SURFACE)),
            ..container::Style::default()
        })
        .into()
    }

    fn view_nav(&self) -> Element<Message> {
        let main_btn = button(text("Dictation").size(13))
            .on_press(Message::SwitchView(AppView::Main))
            .padding(iced::Padding::from([6, 16]))
            .style(nav_button_style(self.view == AppView::Main));

        let settings_btn = button(text("Settings").size(13))
            .on_press(Message::SwitchView(AppView::Settings))
            .padding(iced::Padding::from([6, 16]))
            .style(nav_button_style(self.view == AppView::Settings));

        container(row![main_btn, settings_btn].spacing(6))
            .padding(iced::Padding::from([8, 20]))
            .width(Length::Fill)
            .into()
    }

    fn view_main(&self) -> Element<Message> {
        // Record button
        let btn_label = if self.is_recording {
            "Stop Dictation"
        } else {
            "Start Dictation"
        };

        let record_btn = button(
            text(btn_label)
                .size(16)
                .center()
                .width(Length::Fill),
        )
        .on_press(Message::ToggleDictation)
        .padding(iced::Padding::from([12, 0]))
        .width(Length::Fill)
        .style(record_button_style(self.is_recording));

        // Transcript area
        let mut transcript_content: Vec<Element<Message>> = Vec::new();

        for line in &self.transcript_lines {
            transcript_content.push(
                text(line)
                    .size(14)
                    .color(TEXT_DIM)
                    .into(),
            );
        }

        // Current (live) transcript
        if !self.current_transcript.is_empty() {
            transcript_content.push(
                text(&self.current_transcript)
                    .size(14)
                    .color(TEXT_PRIMARY)
                    .into(),
            );
        }

        if transcript_content.is_empty() {
            transcript_content.push(
                text(if self.is_recording {
                    "Listening for speech..."
                } else {
                    "Transcripts will appear here"
                })
                .size(13)
                .color(TEXT_DIM)
                .into(),
            );
        }

        let transcript_col = column(transcript_content).spacing(4);

        let transcript_area = container(
            scrollable(
                container(transcript_col)
                    .padding(12)
                    .width(Length::Fill),
            )
            .height(Length::Fill),
        )
        .width(Length::Fill)
        .height(Length::Fill)
        .style(transcript_area_style);

        let transcript_label = text("Transcript")
            .size(12)
            .color(TEXT_DIM);

        // Hotkey hint
        let hotkey_hint = text(format!("Hotkey: {}", self.config.hotkey_code))
            .size(12)
            .color(TEXT_DIM);

        container(
            column![
                container(
                    column![record_btn, hotkey_hint]
                        .spacing(8)
                        .align_x(iced::Alignment::Center),
                )
                .width(Length::Fill)
                .style(card_style)
                .padding(16),
                transcript_label,
                transcript_area,
            ]
            .spacing(8),
        )
        .padding(iced::Padding::from([8, 20]))
        .width(Length::Fill)
        .height(Length::Fill)
        .into()
    }

    fn view_settings(&self) -> Element<Message> {
        let api_section = container(
            column![
                text("Deepgram API").size(14).color(ACCENT),
                text("API Key").size(12).color(TEXT_DIM),
                text_input("dg_...", &self.api_key_input)
                    .on_input(Message::ApiKeyChanged)
                    .padding(10)
                    .size(14),
                text("Project ID (for billing)").size(12).color(TEXT_DIM),
                text_input("Project ID", &self.project_id_input)
                    .on_input(Message::ProjectIdChanged)
                    .padding(10)
                    .size(14),
            ]
            .spacing(6),
        )
        .style(card_style)
        .padding(16)
        .width(Length::Fill);

        let hotkey_section = container(
            column![
                text("Hotkey").size(14).color(ACCENT),
                text("Toggle key").size(12).color(TEXT_DIM),
                text_input("F13", &self.hotkey_input)
                    .on_input(Message::HotkeyChanged)
                    .padding(10)
                    .size(14),
            ]
            .spacing(6),
        )
        .style(card_style)
        .padding(16)
        .width(Length::Fill);

        let save_btn = button(
            text("Save Settings")
                .size(14)
                .center()
                .width(Length::Fill),
        )
        .on_press(Message::SaveConfig)
        .padding(iced::Padding::from([10, 0]))
        .width(Length::Fill)
        .style(secondary_button_style);

        // Billing
        let balance_display: String = if self.balance_info.is_empty() {
            "Click to check account balance".into()
        } else {
            self.balance_info.clone()
        };

        let billing_section = container(
            column![
                text("Billing").size(14).color(ACCENT),
                row![
                    button(text("Check Balance").size(13))
                        .on_press(Message::CheckBalance)
                        .padding(iced::Padding::from([6, 12]))
                        .style(secondary_button_style),
                    text(balance_display).size(13).color(TEXT_DIM),
                ]
                .spacing(10)
                .align_y(iced::Alignment::Center),
            ]
            .spacing(6),
        )
        .style(card_style)
        .padding(16)
        .width(Length::Fill);

        container(
            scrollable(
                column![
                    api_section,
                    hotkey_section,
                    horizontal_rule(1),
                    save_btn,
                    horizontal_rule(1),
                    billing_section,
                ]
                .spacing(12),
            ),
        )
        .padding(iced::Padding::from([8, 20]))
        .width(Length::Fill)
        .height(Length::Fill)
        .into()
    }
}

// ── Entry point ─────────────────────────────────────────────────────────

fn main() -> iced::Result {
    iced::application("VoiceType", VoiceKeyboardGui::update, VoiceKeyboardGui::view)
        .subscription(VoiceKeyboardGui::subscription)
        .theme(|_| Theme::Light)
        .window_size((480.0, 560.0))
        .centered()
        .run_with(VoiceKeyboardGui::new)
}
