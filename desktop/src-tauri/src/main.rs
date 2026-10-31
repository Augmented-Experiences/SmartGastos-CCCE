// App de escritorio nativa SmartSuite (Tauri v2)
#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::{Emitter, Manager, RunEvent, Url, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

mod ollama;

const SHUTDOWN_WAIT_ATTEMPTS: u32 = 25;

#[derive(Clone, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct Tier {
    max_ram_gb: f64,
    model: String,
    #[serde(default)]
    extra_models: Vec<String>,
    #[serde(default)]
    id: String,
    #[serde(default)]
    label: String,
}

fn default_block_below_gb() -> f64 {
    7.0
}

fn default_warn_below_gb() -> f64 {
    13.0
}

#[derive(Clone, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct AccessPolicy {
    #[serde(default = "default_block_below_gb")]
    block_below_gb: f64,
    #[serde(default = "default_warn_below_gb")]
    warn_below_gb: f64,
}

impl Default for AccessPolicy {
    fn default() -> Self {
        AccessPolicy {
            block_below_gb: default_block_below_gb(),
            warn_below_gb: default_warn_below_gb(),
        }
    }
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct AppConfig {
    product_name: String,
    data_dir_name: String,
    ollama_tiers: Vec<Tier>,
    #[serde(default)]
    extra_models: Vec<String>,
    #[serde(default)]
    access: AccessPolicy,
}

static APP_CONFIG_JSON: &str = include_str!("../appconfig.json");

fn app_config() -> &'static AppConfig {
    static CFG: OnceLock<AppConfig> = OnceLock::new();
    CFG.get_or_init(|| serde_json::from_str(APP_CONFIG_JSON).expect("appconfig.json invalido"))
}

struct BackendState(Mutex<Option<CommandChild>>);
/// Solo contiene el proceso `ollama serve` lanzado por esta instancia.
struct OllamaState(Mutex<Option<Child>>);
struct ShutdownState(AtomicBool);
struct BootstrapStarted(Mutex<Instant>);
#[derive(Clone)]
struct ShutdownTarget {
    label: &'static str,
    pid: u32,
    port: Option<u16>,
    clear_owned_pid: bool,
}
struct PendingStops(Mutex<Vec<ShutdownTarget>>);
struct Navigated(Mutex<bool>);
struct LastProbe(Mutex<String>);
/// Puerto HTTP de Ollama para esta sesión (11434 u otro loopback).
#[allow(dead_code)]
struct OllamaListenPort(u16);

#[derive(Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct Status {
    phase: String,
    message: String,
    percent: i32,
    backend_url: Option<String>,
    can_continue: bool,
    ollama_done: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    backend_error: Option<String>,
}

impl Status {
    fn initial() -> Self {
        Status {
            phase: "starting".into(),
            message: "Iniciando servicios...".into(),
            percent: -1,
            backend_url: None,
            can_continue: false,
            ollama_done: false,
            backend_error: None,
        }
    }
}

struct AppStatus(Mutex<Status>);
struct BackendPort(u16);

fn persist_status_snapshot(snapshot: &Status) {
    let path = user_data_dir().join("desktop_status.json");
    if let Ok(json) = serde_json::to_string(snapshot) {
        let _ = std::fs::write(path, json);
    }
}

fn update_status(app: &tauri::AppHandle, f: impl FnOnce(&mut Status)) {
    let snapshot = {
        let state = app.state::<AppStatus>();
        let mut s = state.0.lock().unwrap();
        f(&mut s);
        s.clone()
    };
    persist_status_snapshot(&snapshot);
    let _ = app.emit("status", snapshot);
}

#[tauri::command]
fn current_status(state: tauri::State<AppStatus>) -> Status {
    state.0.lock().unwrap().clone()
}

#[tauri::command]
fn navigate_to_backend(
    app: tauri::AppHandle,
    port: tauri::State<BackendPort>,
) -> Result<(), String> {
    open_splash_on_backend(&app, port.0)
}

#[tauri::command]
fn retry_backend(app: tauri::AppHandle, port: tauri::State<BackendPort>) -> Result<String, String> {
    let p = port.0;
    if try_mark_backend_ready(&app, p) {
        open_splash_on_backend(&app, p)?;
        Ok(backend_app_url(p))
    } else {
        let detail = app.state::<LastProbe>().0.lock().unwrap().clone();
        Err(format!(
            "El servidor aun no responde con la UI. Diagnostico: {}",
            detail
        ))
    }
}

fn home_dir() -> PathBuf {
    #[cfg(windows)]
    {
        std::env::var("USERPROFILE")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("."))
    }
    #[cfg(not(windows))]
    {
        std::env::var("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("."))
    }
}

fn user_data_dir() -> PathBuf {
    let app = app_config().data_dir_name.as_str();
    #[cfg(target_os = "windows")]
    {
        let base = std::env::var("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|_| home_dir().join("AppData").join("Roaming"));
        base.join(app)
    }
    #[cfg(target_os = "macos")]
    {
        home_dir()
            .join("Library")
            .join("Application Support")
            .join(app)
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let base = std::env::var("XDG_DATA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| home_dir().join(".local").join("share"));
        base.join(app)
    }
}

fn log_dir() -> PathBuf {
    let dir = user_data_dir().join("logs");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

fn ollama_log(line: &str) {
    let path = log_dir().join("ollama.log");
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = writeln!(f, "{}", line);
    }
}

/// Mismo archivo que los mensajes de Ollama (`logs/ollama.log` bajo APPDATA/SmartGastos).
fn backend_log(line: &str) {
    ollama_log(line);
}

fn process_is_alive(pid: u32) -> bool {
    let pid = sysinfo::Pid::from_u32(pid);
    let mut system = sysinfo::System::new();
    system.refresh_processes(sysinfo::ProcessesToUpdate::Some(&[pid]), true);
    system.process(pid).is_some()
}

fn port_is_released(port: u16) -> bool {
    if TcpStream::connect(("127.0.0.1", port)).is_ok() {
        return false;
    }
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

fn register_shutdown_target(app: &tauri::AppHandle, target: ShutdownTarget) {
    let pending = app.state::<PendingStops>();
    let mut targets = pending.0.lock().unwrap();
    if !targets
        .iter()
        .any(|known| known.label == target.label && known.pid == target.pid)
    {
        targets.push(target);
    }
}

fn complete_shutdown_target(app: &tauri::AppHandle, target: &ShutdownTarget) {
    if target.clear_owned_pid {
        ollama::clear_owned_pid(&user_data_dir());
    }
    let pending = app.state::<PendingStops>();
    pending
        .0
        .lock()
        .unwrap()
        .retain(|known| known.label != target.label || known.pid != target.pid);
}

fn shutdown_target_released(target: &ShutdownTarget) -> (bool, bool) {
    let pid_dead = !process_is_alive(target.pid);
    let port_released = target.port.map(port_is_released).unwrap_or(true);
    (pid_dead, port_released)
}

fn wait_for_owned_shutdown(app: &tauri::AppHandle, target: &ShutdownTarget) -> bool {
    let mut pid_dead = false;
    let mut port_released = target.port.is_none();
    for i in 0..SHUTDOWN_WAIT_ATTEMPTS {
        (pid_dead, port_released) = shutdown_target_released(target);
        if pid_dead && port_released {
            ollama_log(&format!(
                "{}: pid={} terminado; puerto {:?} sin listener y enlazable",
                target.label, target.pid, target.port
            ));
            complete_shutdown_target(app, target);
            return true;
        }
        // PyInstaller deja hijos (uvicorn). Si el PID padre ya murió, no retener
        // la ventana por TIME_WAIT / un listener huérfano.
        if pid_dead && i >= 5 {
            ollama_log(&format!(
                "{}: pid={} muerto; se cierra sin esperar el puerto {:?}",
                target.label, target.pid, target.port
            ));
            complete_shutdown_target(app, target);
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    ollama_log(&format!(
        "WARNING {}: espera agotada pid_dead={} port_released={} pid={} port={:?}",
        target.label, pid_dead, port_released, target.pid, target.port
    ));
    complete_shutdown_target(app, target);
    true
}

fn wait_for_pending_shutdown(app: &tauri::AppHandle) -> bool {
    for i in 0..SHUTDOWN_WAIT_ATTEMPTS {
        let targets = app.state::<PendingStops>().0.lock().unwrap().clone();
        if targets.is_empty() {
            return true;
        }
        for target in targets {
            let (pid_dead, port_released) = shutdown_target_released(&target);
            if pid_dead && (port_released || i >= 5) {
                complete_shutdown_target(app, &target);
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let targets = app.state::<PendingStops>().0.lock().unwrap().clone();
    for target in &targets {
        let (pid_dead, port_released) = shutdown_target_released(target);
        ollama_log(&format!(
            "WARNING cierre pendiente {}: pid_dead={} port_released={} pid={} port={:?}",
            target.label, pid_dead, port_released, target.pid, target.port
        ));
        if pid_dead {
            complete_shutdown_target(app, target);
        }
    }
    true
}

fn stop_backend_child_registered(
    app: &tauri::AppHandle,
    child: CommandChild,
    port: Option<u16>,
) -> bool {
    let pid = child.pid();
    let target = ShutdownTarget {
        label: "kill_backend_child",
        pid,
        port,
        clear_owned_pid: false,
    };
    ollama_log(&format!(
        "kill_backend_child: terminando sidecar backend (pid={})",
        pid
    ));
    ollama::kill_pid_tree(pid);
    if let Some(listen_port) = port {
        ollama::kill_listeners_on_port(listen_port);
    }
    if let Err(e) = child.kill() {
        ollama_log(&format!("kill_backend_child: kill pid={} {}", pid, e));
    }
    let stopped = wait_for_owned_shutdown(app, &target);
    if stopped {
        ollama_log("kill_backend_child: OK");
    }
    stopped
}

fn kill_backend_child(app: &tauri::AppHandle, port: Option<u16>) -> bool {
    let child = {
        let state = app.state::<BackendState>();
        let mut slot = state.0.lock().unwrap();
        let child = slot.take();
        if let Some(child) = child.as_ref() {
            register_shutdown_target(
                app,
                ShutdownTarget {
                    label: "kill_backend_child",
                    pid: child.pid(),
                    port,
                    clear_owned_pid: false,
                },
            );
        }
        child
    };
    if let Some(child) = child {
        stop_backend_child_registered(app, child, port)
    } else {
        true
    }
}

fn stop_ollama_child_registered(
    app: &tauri::AppHandle,
    mut child: Child,
    port: Option<u16>,
) -> bool {
    let pid = child.id();
    let target = ShutdownTarget {
        label: "kill_ollama_child",
        pid,
        port,
        clear_owned_pid: true,
    };
    ollama_log(&format!(
        "kill_ollama_child: terminando Ollama iniciado por esta app (pid={})",
        pid
    ));
    ollama::kill_owned_child(&mut child);
    let stopped = wait_for_owned_shutdown(app, &target);
    if stopped {
        ollama_log("kill_ollama_child: OK");
    }
    stopped
}

fn kill_ollama_child(app: &tauri::AppHandle, port: Option<u16>) -> bool {
    let child = {
        let state = app.state::<OllamaState>();
        let mut slot = state.0.lock().unwrap();
        let child = slot.take();
        if let Some(child) = child.as_ref() {
            register_shutdown_target(
                app,
                ShutdownTarget {
                    label: "kill_ollama_child",
                    pid: child.id(),
                    port,
                    clear_owned_pid: true,
                },
            );
        }
        child
    };
    if let Some(child) = child {
        stop_ollama_child_registered(app, child, port)
    } else {
        true
    }
}

fn shutdown_requested(app: &tauri::AppHandle) -> bool {
    app.state::<ShutdownState>().0.load(Ordering::Acquire)
}

fn ignore_stale_early_close(app: &tauri::AppHandle) -> bool {
    let splash_started = *app.state::<Navigated>().0.lock().unwrap();
    let started = *app.state::<BootstrapStarted>().0.lock().unwrap();
    !splash_started && started.elapsed() < Duration::from_secs(2)
}

fn shutdown_app(app: &tauri::AppHandle, reason: &str) -> bool {
    if app.state::<ShutdownState>().0.swap(true, Ordering::AcqRel) {
        return wait_for_pending_shutdown(app);
    }
    ollama_log(&format!("shutdown_app: {}", reason));
    let backend_port = app.try_state::<BackendPort>().map(|port| port.0);
    let ollama_port = app.try_state::<OllamaListenPort>().map(|port| port.0);
    let backend_stopped = kill_backend_child(app, backend_port);
    let ollama_stopped = kill_ollama_child(app, ollama_port);
    let pending_stopped = wait_for_pending_shutdown(app);
    let stopped = backend_stopped && ollama_stopped && pending_stopped;
    if stopped {
        ollama_log("shutdown_app: procesos propios terminados y puertos liberados");
    } else {
        ollama_log(
            "WARNING shutdown_app: timeout parcial; se cierra la ventana de todos modos",
        );
    }
    true
}

fn pick_port() -> u16 {
    for p in [7860u16, 7861, 7862, 7863] {
        if TcpListener::bind(("127.0.0.1", p)).is_ok() {
            return p;
        }
    }
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(7860)
}

fn backend_app_url(port: u16) -> String {
    format!("http://127.0.0.1:{}/", port)
}

fn splash_url(port: u16) -> String {
    format!("http://127.0.0.1:{}/__splash/", port)
}

fn wait_for_health(app: &tauri::AppHandle, port: u16, attempts: u32) -> bool {
    let health_url = format!("http://127.0.0.1:{}/api/health", port);
    for _ in 0..attempts {
        if shutdown_requested(app) {
            return false;
        }
        match ureq::get(&health_url).call() {
            Ok(r) if r.status() == 200 => {
                ollama_log("GET /api/health 200 — sidecar arriba");
                return true;
            }
            Ok(r) => ollama_log(&format!("GET /api/health status {}", r.status())),
            Err(e) => ollama_log(&format!("GET /api/health error: {}", e)),
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

fn navigate_webview_external(app: &tauri::AppHandle, url_str: &str) -> Result<(), String> {
    let parsed = Url::parse(url_str).map_err(|e| format!("URL invalida: {}", e))?;
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "ventana main no encontrada".to_string())?;
    window
        .navigate(parsed)
        .map_err(|e| format!("navigate fallo: {}", e))?;
    ollama_log(&format!("WebView navigate OK -> {}", url_str));
    Ok(())
}

fn open_splash_on_backend(app: &tauri::AppHandle, port: u16) -> Result<(), String> {
    if shutdown_requested(app) {
        return Err("cierre en curso".into());
    }
    let url = splash_url(port);
    let handle = app.clone();
    ollama_log(&format!("abriendo splash same-origin: {}", url));
    app.run_on_main_thread(move || {
        if shutdown_requested(&handle) {
            ollama_log("open_splash_on_backend: cancelado por cierre");
            return;
        }
        let navigated_state = handle.state::<Navigated>();
        let mut navigated = navigated_state.0.lock().unwrap();
        if *navigated {
            return;
        }
        if let Err(e) = navigate_webview_external(&handle, &url) {
            ollama_log(&format!("open_splash_on_backend navigate: {}", e));
            return;
        }
        *navigated = true;
        drop(navigated);
        if let Some(w) = handle.get_webview_window("main") {
            let _ = w.show();
            let _ = w.set_focus();
        } else {
            ollama_log("open_splash_on_backend: ventana main no encontrada despues de navigate");
        }
    })
    .map_err(|e| format!("open_splash_on_backend: run_on_main_thread {}", e))?;
    ollama_log("open_splash_on_backend: run_on_main_thread OK");
    Ok(())
}

struct ProbeResult {
    ok: bool,
    detail: String,
}

fn probe_backend(port: u16) -> ProbeResult {
    let health_url = format!("http://127.0.0.1:{}/api/health", port);
    let root_url = backend_app_url(port);
    let health_status = match ureq::get(&health_url).call() {
        Ok(r) => r.status(),
        Err(e) => {
            return ProbeResult {
                ok: false,
                detail: format!("GET /api/health error: {}", e),
            };
        }
    };
    if health_status != 200 {
        return ProbeResult {
            ok: false,
            detail: format!("GET /api/health status {}", health_status),
        };
    }

    let ui_url = format!("http://127.0.0.1:{}/api/desktop-ui", port);
    if let Ok(r) = ureq::get(&ui_url).call() {
        if r.status() == 200 {
            if let Ok(body) = r.into_string() {
                ollama_log(&format!("/api/desktop-ui: {}", body));
                if body.contains("\"indexExists\":false") || body.contains("\"indexExists\": false")
                {
                    return ProbeResult {
                        ok: false,
                        detail: "sidecar sin app/index.html empaquetado (rebuild PyInstaller)"
                            .into(),
                    };
                }
            }
        }
    }

    match ureq::get(&root_url).call() {
        Ok(r) => {
            let status = r.status();
            let body = r.into_string().unwrap_or_default();
            let snippet: String = body.chars().take(120).collect();
            if status == 200 && (body.contains("<!DOCTYPE") || body.contains("<html")) {
                ProbeResult {
                    ok: true,
                    detail: "health 200 + HTML en /".into(),
                }
            } else {
                ProbeResult {
                    ok: false,
                    detail: format!("GET / status {} (no HTML). head={:?}", status, snippet),
                }
            }
        }
        Err(e) => ProbeResult {
            ok: false,
            detail: format!("GET / error: {}", e),
        },
    }
}

fn wait_for_backend_ready(app: &tauri::AppHandle, port: u16, attempts: u32) -> bool {
    for _ in 0..attempts {
        if shutdown_requested(app) {
            return false;
        }
        let probe = probe_backend(port);
        *app.state::<LastProbe>().0.lock().unwrap() = probe.detail.clone();
        if probe.ok {
            ollama_log(&format!("sidecar listo: {}", probe.detail));
            return true;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

fn apply_backend_ready(app: &tauri::AppHandle, port: u16) {
    let url = backend_app_url(port);
    update_status(app, |s| {
        s.backend_url = Some(url);
        s.can_continue = true;
        s.backend_error = None;
        if s.ollama_done {
            s.phase = "ready".into();
            s.percent = 100;
            s.message = "Abriendo la aplicación...".into();
        }
        // No pisar el progreso de descarga/extracción/arranque/pull de Ollama.
    });
}

fn try_mark_backend_ready(app: &tauri::AppHandle, port: u16) -> bool {
    let probe = probe_backend(port);
    *app.state::<LastProbe>().0.lock().unwrap() = probe.detail.clone();
    ollama_log(&format!("probe try_mark_backend_ready: {}", probe.detail));
    if probe.ok {
        apply_backend_ready(app, port);
        true
    } else {
        false
    }
}

fn ram_gb() -> f64 {
    let mut sys = sysinfo::System::new();
    sys.refresh_memory();
    sys.total_memory() as f64 / 1024.0 / 1024.0 / 1024.0
}

fn profile_for_ram() -> Tier {
    let gb = ram_gb();
    let cfg = app_config();
    let mut chosen = None;
    for t in &cfg.ollama_tiers {
        if t.max_ram_gb > 0.0 && gb < t.max_ram_gb {
            chosen = Some(t.clone());
            break;
        }
    }
    let mut profile = chosen
        .or_else(|| cfg.ollama_tiers.last().cloned())
        .unwrap_or_else(|| Tier {
            max_ram_gb: 0.0,
            model: "llama3.2:3b".to_string(),
            extra_models: Vec::new(),
            id: "estandar".to_string(),
            label: "Estándar".to_string(),
        });
    if profile.id.is_empty() {
        profile.id = "auto".to_string();
    }
    if profile.label.is_empty() {
        profile.label = profile.model.clone();
    }
    ollama_log(&format!(
        "perfil {} ({:.1} GB RAM) modelo={} extras={:?}",
        profile.label, gb, profile.model, profile.extra_models
    ));
    profile
}

fn model_for_ram() -> String {
    profile_for_ram().model
}

fn low_ram_override() -> bool {
    for key in ["SMARTSUITE_ALLOW_LOW_RAM", "SMARTGASTOS_ALLOW_LOW_RAM"] {
        if let Ok(v) = std::env::var(key) {
            let t = v.trim().to_ascii_lowercase();
            if t == "1" || t == "true" || t == "yes" || t == "on" {
                return true;
            }
        }
    }
    false
}

fn ram_access_level(gb: f64) -> &'static str {
    if low_ram_override() || gb <= 0.0 {
        return "ok";
    }
    let access = &app_config().access;
    let block_at = if access.block_below_gb > 0.0 {
        access.block_below_gb
    } else {
        7.0
    };
    let warn_at = if access.warn_below_gb > 0.0 {
        access.warn_below_gb
    } else {
        13.0
    };
    if gb < block_at {
        "block"
    } else if gb < warn_at {
        "warn"
    } else {
        "ok"
    }
}

fn blocked_ram_message(gb: f64) -> String {
    format!(
        "Este equipo no cumple el mínimo. Se midieron {:.1} GB de RAM y se necesitan al menos 8 GB.",
        gb
    )
}

fn persist_active_profile(profile: &Tier) {
    let home = ollama::ollama_home(&user_data_dir());
    let _ = std::fs::create_dir_all(&home);
    let _ = std::fs::write(home.join("active_model.txt"), &profile.model);
    let gb = ram_gb();
    let payload = serde_json::json!({
        "id": profile.id,
        "label": profile.label,
        "model": profile.model,
        "extraModels": profile.extra_models,
        "ramGb": gb,
        "access": ram_access_level(gb),
    });
    if let Ok(text) = serde_json::to_string_pretty(&payload) {
        let _ = std::fs::write(home.join("active_profile.json"), text);
    }
}

fn status_progress(app: &tauri::AppHandle, phase: &str, percent: i32, message: &str) {
    ollama_log(message);
    update_status(app, |s| {
        s.phase = phase.into();
        s.message = message.into();
        s.percent = percent;
    });
}

fn take_owned_slot(app: &tauri::AppHandle, child: Child, port: u16) -> bool {
    let pid = child.id();
    let ollama_state = app.state::<OllamaState>();
    let mut slot = ollama_state.0.lock().unwrap();
    if shutdown_requested(app) {
        register_shutdown_target(
            app,
            ShutdownTarget {
                label: "kill_ollama_child",
                pid,
                port: Some(port),
                clear_owned_pid: true,
            },
        );
        drop(slot);
        stop_ollama_child_registered(app, child, Some(port));
        return false;
    }
    if let Some(previous) = slot.take() {
        register_shutdown_target(
            app,
            ShutdownTarget {
                label: "kill_ollama_child",
                pid: previous.id(),
                port: Some(port),
                clear_owned_pid: true,
            },
        );
        drop(slot);
        stop_ollama_child_registered(app, previous, Some(port));
        slot = ollama_state.0.lock().unwrap();
        if shutdown_requested(app) {
            register_shutdown_target(
                app,
                ShutdownTarget {
                    label: "kill_ollama_child",
                    pid,
                    port: Some(port),
                    clear_owned_pid: true,
                },
            );
            drop(slot);
            stop_ollama_child_registered(app, child, Some(port));
            return false;
        }
    }
    slot.replace(child);
    ollama::record_owned_pid(&user_data_dir(), pid);
    drop(slot);
    ollama_log(&format!(
        "Ollama portable iniciado por esta instancia (pid={})",
        pid
    ));
    true
}

fn pull_model_with_progress(app: &tauri::AppHandle, port: u16, model: &str) -> bool {
    ollama::pull_model(port, model, |pct, msg| {
        status_progress(app, "downloading", pct, msg);
    })
}

fn finish_ollama_bootstrap(app: &tauri::AppHandle, backend_port: u16) {
    ollama_log("== finish_ollama_bootstrap (post extra_models)");
    let _ = try_mark_backend_ready(app, backend_port);

    update_status(app, |s| {
        s.ollama_done = true;
        s.backend_error = None;
        if s.can_continue {
            s.percent = 100;
            s.phase = "ready".into();
            s.message = "Abriendo la aplicación...".into();
            if s.backend_url.is_none() {
                s.backend_url = Some(backend_app_url(backend_port));
            }
        } else {
            let detail = app.state::<LastProbe>().0.lock().unwrap().clone();
            ollama_log(&format!(
                "UI aun no lista (se sigue esperando, sin mostrar el error): {}",
                detail
            ));
            s.percent = -1;
            s.phase = "loading-ui".into();
            s.message = "Cargando la aplicación...".into();
        }
    });
    ollama_log("ollama_done=true; splash espera can_continue y luego location.replace('/')");
}

fn finish_ollama_failure(app: &tauri::AppHandle, backend_port: u16) {
    let _ = try_mark_backend_ready(app, backend_port);
    update_status(app, |s| {
        s.ollama_done = false;
        s.percent = -1;
        s.phase = "warning".into();
        s.backend_error = Some(s.message.clone());
    });
    ollama_log("ollama_done=false; el fallo no se publica como 100%");
}

fn bootstrap_ollama(
    app: tauri::AppHandle,
    backend_port: u16,
    ollama_port: u16,
    already_healthy: bool,
) {
    std::thread::spawn(move || {
        if shutdown_requested(&app) {
            return;
        }
        let gb = ram_gb();
        if ram_access_level(gb) == "block" {
            persist_active_profile(&profile_for_ram());
            let msg = blocked_ram_message(gb);
            ollama_log(&msg);
            update_status(&app, |s| {
                s.phase = "blocked".into();
                s.message = msg;
                s.percent = -1;
                s.can_continue = false;
                s.ollama_done = false;
            });
            return;
        }
        status_progress(&app, "ollama", -1, "Verificando el motor de IA (Ollama)...");

        let data_dir = user_data_dir();
        if already_healthy {
            ollama_log(&format!(
                "Usando Ollama ya en marcha en {} (no se registra PID propio)",
                ollama::api_base(ollama_port)
            ));
        } else {
            match ollama::ensure_portable_binary(&data_dir, |pct, msg| {
                let phase = if msg.starts_with("Extrayendo") {
                    "extract"
                } else {
                    "download"
                };
                status_progress(&app, phase, pct, msg);
            }) {
                Ok(bin) => {
                    if shutdown_requested(&app) {
                        return;
                    }
                    if ollama::api_healthy(ollama_port) {
                        ollama_log(
                            "Ollama respondio en el puerto elegido antes de spawn; no se toma PID",
                        );
                    } else {
                        status_progress(&app, "start", -1, "Iniciando el servicio de IA...");
                        match ollama::spawn_serve(&bin, ollama_port, &ollama::models_dir(&data_dir))
                        {
                            Ok(child) => {
                                if !take_owned_slot(&app, child, ollama_port) {
                                    return;
                                }
                            }
                            Err(e) => {
                                ollama_log(&format!("No se pudo iniciar Ollama portable: {}", e));
                                status_progress(
                                    &app,
                                    "warning",
                                    -1,
                                    "No se pudo iniciar Ollama. Revise logs/ollama.log.",
                                );
                                finish_ollama_failure(&app, backend_port);
                                return;
                            }
                        }
                        if !ollama::wait_until_healthy(ollama_port, 60) {
                            ollama_log("Ollama portable no respondio a /api/tags");
                            status_progress(
                                &app,
                                "warning",
                                -1,
                                "Ollama no respondio a tiempo. Revise logs/ollama.log.",
                            );
                            finish_ollama_failure(&app, backend_port);
                            return;
                        }
                        ollama_log(&format!(
                            "Ollama portable listo en {}",
                            ollama::api_base(ollama_port)
                        ));
                    }
                }
                Err(e) => {
                    ollama_log(&format!("Ollama portable no disponible: {}", e));
                    status_progress(
                        &app,
                        "warning",
                        -1,
                        "No se pudo descargar Ollama. Revise logs/ollama.log.",
                    );
                    finish_ollama_failure(&app, backend_port);
                    return;
                }
            }
        }

        if shutdown_requested(&app) {
            return;
        }
        if !ollama::api_healthy(ollama_port) {
            ollama_log("API de Ollama no saludable; se omite pull");
            status_progress(
                &app,
                "warning",
                -1,
                "Ollama no esta disponible. Revise logs/ollama.log.",
            );
            finish_ollama_failure(&app, backend_port);
            return;
        }

        let profile = profile_for_ram();
        if pull_model_with_progress(&app, ollama_port, &profile.model) {
            persist_active_profile(&profile);
            ollama_log(&format!(
                "Modelo {} listo (perfil {}); chat debe usar este si el agente pide otro",
                profile.model, profile.label
            ));
        }

        let extras = if profile.extra_models.is_empty() {
            Vec::new()
        } else {
            profile.extra_models.clone()
        };
        for extra in extras {
            if shutdown_requested(&app) {
                return;
            }
            let _ = pull_model_with_progress(&app, ollama_port, &extra);
            ollama_log(&format!("Modelo adicional '{}' listo.", extra));
            status_progress(
                &app,
                "downloading",
                100,
                &format!("Componente {} listo.", extra),
            );
        }

        ollama_log("extra_models terminado; entrando a finish_ollama_bootstrap");
        finish_ollama_bootstrap(&app, backend_port);
    });
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(BackendState(Mutex::new(None)))
        .manage(OllamaState(Mutex::new(None)))
        .manage(ShutdownState(AtomicBool::new(false)))
        .manage(BootstrapStarted(Mutex::new(Instant::now())))
        .manage(PendingStops(Mutex::new(Vec::new())))
        .manage(AppStatus(Mutex::new(Status::initial())))
        .manage(Navigated(Mutex::new(false)))
        .manage(LastProbe(Mutex::new(String::new())))
        .invoke_handler(tauri::generate_handler![
            current_status,
            retry_backend,
            navigate_to_backend
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            *handle.state::<BootstrapStarted>().0.lock().unwrap() = Instant::now();
            *handle.state::<Navigated>().0.lock().unwrap() = false;
            persist_status_snapshot(&Status::initial());

            kill_backend_child(&handle, None);
            let port = pick_port();
            app.manage(BackendPort(port));
            ollama_log(&format!("Puerto backend elegido: {}", port));

            let (ollama_port, ollama_already_healthy) = ollama::decide_listen_port();
            app.manage(OllamaListenPort(ollama_port));
            ollama_log(&format!(
                "Ollama HTTP {} (ya saludable={})",
                ollama::api_base(ollama_port),
                ollama_already_healthy
            ));

            let data_dir = user_data_dir().to_string_lossy().to_string();
            let backend_state = app.state::<BackendState>();
            if backend_state.0.lock().unwrap().is_some() {
                ollama_log("WARN: sidecar ya registrado; omitiendo segundo spawn");
            }
            let ollama_url = ollama::api_base(ollama_port);
            let ollama_host = format!("127.0.0.1:{}", ollama_port);
            let ram_profile = profile_for_ram();
            let ram_model = ram_profile.model.clone();
            ollama_log(&format!(
                "sidecar env OLLAMA_URL={} OLLAMA_MODEL={} OLLAMA_PROFILE={} (agentes deben usar este si su modelo no esta)",
                ollama_url,
                ram_model,
                ram_profile.id
            ));
            persist_active_profile(&ram_profile);
            if ram_access_level(ram_gb()) == "block" {
                let msg = blocked_ram_message(ram_gb());
                ollama_log(&msg);
                update_status(&handle, |s| {
                    s.phase = "blocked".into();
                    s.message = msg;
                    s.percent = -1;
                    s.can_continue = false;
                    s.ollama_done = false;
                });
            }
            let sidecar = app.shell().sidecar("backend");
            match sidecar {
                Ok(cmd) => match cmd
                    .env("PORT", port.to_string())
                    .env("DATA_DIR", data_dir)
                    .env("RUN_BY_TAURI", "1")
                    .env("PYTHONIOENCODING", "utf-8")
                    .env("OLLAMA_URL", ollama_url)
                    .env("OLLAMA_HOST", ollama_host)
                    .env("OLLAMA_MODEL", ram_model)
                    .env("OLLAMA_PROFILE", ram_profile.id.clone())
                    .spawn()
                {
                    Ok((mut rx, child)) => {
                        let backend_state = app.state::<BackendState>();
                        let mut slot = backend_state.0.lock().unwrap();
                        if shutdown_requested(&handle) {
                            register_shutdown_target(
                                &handle,
                                ShutdownTarget {
                                    label: "kill_backend_child",
                                    pid: child.pid(),
                                    port: Some(port),
                                    clear_owned_pid: false,
                                },
                            );
                            drop(slot);
                            stop_backend_child_registered(&handle, child, Some(port));
                            return Ok(());
                        }
                        if let Some(previous) = slot.take() {
                            ollama_log("WARN: sidecar slot ocupado; matando instancia duplicada");
                            register_shutdown_target(
                                &handle,
                                ShutdownTarget {
                                    label: "kill_backend_child",
                                    pid: previous.pid(),
                                    port: Some(port),
                                    clear_owned_pid: false,
                                },
                            );
                            drop(slot);
                            stop_backend_child_registered(&handle, previous, Some(port));
                            slot = backend_state.0.lock().unwrap();
                            if shutdown_requested(&handle) {
                                register_shutdown_target(
                                    &handle,
                                    ShutdownTarget {
                                        label: "kill_backend_child",
                                        pid: child.pid(),
                                        port: Some(port),
                                        clear_owned_pid: false,
                                    },
                                );
                                drop(slot);
                                stop_backend_child_registered(&handle, child, Some(port));
                                return Ok(());
                            }
                        }
                        slot.replace(child);
                        let sidecar_handle = handle.clone();
                        tauri::async_runtime::spawn(async move {
                            while let Some(event) = rx.recv().await {
                                if let CommandEvent::Stderr(bytes) | CommandEvent::Stdout(bytes) =
                                    event
                                {
                                    backend_log(&String::from_utf8_lossy(&bytes));
                                }
                            }
                            ollama_log("sidecar backend: canal de eventos cerrado (proceso terminó?)");
                            kill_backend_child(&sidecar_handle, Some(port));
                        });
                    }
                    Err(e) => {
                        backend_log(&format!("spawn error: {}", e));
                        update_status(&handle, |s| {
                            s.phase = "warning".into();
                            s.message = format!("No se pudo iniciar el servidor: {}", e);
                            s.backend_error = Some(s.message.clone());
                        });
                    }
                },
                Err(e) => {
                    backend_log(&format!("sidecar missing: {}", e));
                    update_status(&handle, |s| {
                        s.phase = "warning".into();
                        s.message = format!("Backend no encontrado en el instalador: {}", e);
                        s.backend_error = Some(s.message.clone());
                    });
                }
            }

            let splash_handle = handle.clone();
            std::thread::spawn(move || {
                if wait_for_health(&splash_handle, port, 120) {
                    if let Err(e) = open_splash_on_backend(&splash_handle, port) {
                        ollama_log(&format!("open_splash_on_backend: {}", e));
                    }
                } else if !shutdown_requested(&splash_handle) {
                    ollama_log(
                        "ERROR: /api/health no respondio; sidecar caido (revise traceback en este log)",
                    );
                    update_status(&splash_handle, |s| {
                        s.phase = "loading-ui".into();
                        s.percent = -1;
                        s.message = "Cargando la aplicación...".into();
                        s.backend_error = None;
                    });
                }
            });

            bootstrap_ollama(handle.clone(), port, ollama_port, ollama_already_healthy);

            let ready_handle = handle.clone();
            std::thread::spawn(move || {
                if wait_for_backend_ready(&ready_handle, port, 3600) {
                    apply_backend_ready(&ready_handle, port);
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error al construir la app de escritorio")
        .run(|app_handle, event| {
            match event {
                RunEvent::ExitRequested { .. } => {
                    let _ = shutdown_app(app_handle, "RunEvent::ExitRequested");
                }
                RunEvent::Exit => {
                    let _ = shutdown_app(app_handle, "RunEvent::Exit");
                }
                RunEvent::WindowEvent { label, event, .. } if label == "main" => {
                    match event {
                        WindowEvent::CloseRequested { api, .. } => {
                            if ignore_stale_early_close(app_handle) {
                                api.prevent_close();
                                ollama_log(
                                    "CloseRequested temprano ignorado hasta iniciar el splash",
                                );
                                return;
                            }
                            let _ = shutdown_app(
                                app_handle,
                                "RunEvent::WindowEvent CloseRequested",
                            );
                            app_handle.exit(0);
                        }
                        WindowEvent::Destroyed => {
                            let _ =
                                shutdown_app(app_handle, "RunEvent::WindowEvent Destroyed");
                            app_handle.exit(0);
                        }
                        _ => {}
                    }
                }
                _ => {}
            }
        });
}
