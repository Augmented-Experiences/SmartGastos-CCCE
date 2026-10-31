//! Portable in-app Ollama: reuse a healthy system daemon on 11434, otherwise
//! download the official zip/tgz into the product data dir, start `serve`,
//! and pull models. Never runs the Windows MSI / system installer.

use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
#[cfg(windows)]
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

pub const DEFAULT_PORT: u16 = 11434;
const OWNED_PID_FILE: &str = "owned.pid";

/// Official GitHub release assets (followed via HTTP redirects).
#[allow(dead_code)]
pub const WINDOWS_AMD64_ZIP: &str =
    "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip";
#[allow(dead_code)]
pub const LINUX_AMD64_TGZ: &str =
    "https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tgz";
#[allow(dead_code)]
pub const LINUX_ARM64_TGZ: &str =
    "https://github.com/ollama/ollama/releases/latest/download/ollama-linux-arm64.tgz";
#[allow(dead_code)]
pub const DARWIN_TGZ: &str =
    "https://github.com/ollama/ollama/releases/latest/download/ollama-darwin.tgz";
#[allow(dead_code)]
pub const WINDOWS_AMD64_ZIP_FALLBACK: &str = "https://ollama.com/download/ollama-windows-amd64.zip";
#[allow(dead_code)]
pub const LINUX_AMD64_TGZ_FALLBACK: &str = "https://ollama.com/download/ollama-linux-amd64.tgz";

pub fn ollama_home(data_dir: &Path) -> PathBuf {
    data_dir.join("ollama")
}

pub fn models_dir(data_dir: &Path) -> PathBuf {
    ollama_home(data_dir).join("models")
}

fn download_dir(data_dir: &Path) -> PathBuf {
    ollama_home(data_dir).join("download")
}

fn owned_pid_path(data_dir: &Path) -> PathBuf {
    ollama_home(data_dir).join(OWNED_PID_FILE)
}

pub fn record_owned_pid(data_dir: &Path, pid: u32) {
    let home = ollama_home(data_dir);
    let _ = fs::create_dir_all(&home);
    let _ = fs::write(owned_pid_path(data_dir), pid.to_string());
}

pub fn clear_owned_pid(data_dir: &Path) {
    let _ = fs::remove_file(owned_pid_path(data_dir));
}

pub fn api_base(port: u16) -> String {
    format!("http://127.0.0.1:{}", port)
}

fn http_agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(30))
        .timeout_read(Duration::from_secs(120))
        .timeout(Duration::from_secs(6 * 3600))
        .build()
}

pub fn api_healthy(port: u16) -> bool {
    let url = format!("{}/api/tags", api_base(port));
    match ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(3))
        .build()
        .get(&url)
        .call()
    {
        Ok(r) => r.status() == 200,
        Err(_) => false,
    }
}

fn tcp_open(port: u16) -> bool {
    TcpStream::connect(("127.0.0.1", port)).is_ok()
}

fn port_bindable(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// Prefer a healthy system Ollama on 11434. Otherwise pick 11434 if free, else
/// another loopback port for the portable `serve`.
pub fn decide_listen_port() -> (u16, bool) {
    if api_healthy(DEFAULT_PORT) {
        return (DEFAULT_PORT, true);
    }
    if tcp_open(DEFAULT_PORT) {
        for _ in 0..6 {
            std::thread::sleep(Duration::from_millis(500));
            if api_healthy(DEFAULT_PORT) {
                return (DEFAULT_PORT, true);
            }
        }
        return (pick_alternate_port(), false);
    }
    (DEFAULT_PORT, false)
}

fn pick_alternate_port() -> u16 {
    for p in 11435u16..=11450 {
        if port_bindable(p) {
            return p;
        }
    }
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(11435)
}

pub fn wait_until_healthy(port: u16, attempts: u32) -> bool {
    for _ in 0..attempts {
        if api_healthy(port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

pub fn binary_name() -> &'static str {
    if cfg!(windows) {
        "ollama.exe"
    } else {
        "ollama"
    }
}

pub fn find_portable_binary(data_dir: &Path) -> Option<PathBuf> {
    let home = ollama_home(data_dir);
    if !home.is_dir() {
        return None;
    }
    find_named(&home, binary_name(), 4)
}

fn find_named(dir: &Path, want: &str, depth: u8) -> Option<PathBuf> {
    if depth == 0 {
        return None;
    }
    let direct = dir.join(want);
    if direct.is_file() {
        return Some(direct);
    }
    let rd = fs::read_dir(dir).ok()?;
    for ent in rd.flatten() {
        let p = ent.path();
        if p.is_dir() {
            if let Some(found) = find_named(&p, want, depth - 1) {
                return Some(found);
            }
        } else if p.file_name().map(|n| n == want).unwrap_or(false) {
            return Some(p);
        }
    }
    None
}

struct ArchiveSpec {
    url: &'static str,
    fallback: Option<&'static str>,
    filename: &'static str,
    kind: ArchiveKind,
}

enum ArchiveKind {
    Zip,
    Tgz,
}

fn archive_for_host() -> Result<ArchiveSpec, String> {
    #[cfg(all(windows, target_arch = "x86_64"))]
    {
        return Ok(ArchiveSpec {
            url: WINDOWS_AMD64_ZIP,
            fallback: Some(WINDOWS_AMD64_ZIP_FALLBACK),
            filename: "ollama-windows-amd64.zip",
            kind: ArchiveKind::Zip,
        });
    }
    #[cfg(all(windows, target_arch = "aarch64"))]
    {
        return Ok(ArchiveSpec {
            url:
                "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-arm64.zip",
            fallback: None,
            filename: "ollama-windows-arm64.zip",
            kind: ArchiveKind::Zip,
        });
    }
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    {
        return Ok(ArchiveSpec {
            url: LINUX_AMD64_TGZ,
            fallback: Some(LINUX_AMD64_TGZ_FALLBACK),
            filename: "ollama-linux-amd64.tgz",
            kind: ArchiveKind::Tgz,
        });
    }
    #[cfg(all(target_os = "linux", target_arch = "aarch64"))]
    {
        return Ok(ArchiveSpec {
            url: LINUX_ARM64_TGZ,
            fallback: None,
            filename: "ollama-linux-arm64.tgz",
            kind: ArchiveKind::Tgz,
        });
    }
    #[cfg(target_os = "macos")]
    {
        return Ok(ArchiveSpec {
            url: DARWIN_TGZ,
            fallback: None,
            filename: "ollama-darwin.tgz",
            kind: ArchiveKind::Tgz,
        });
    }
    #[allow(unreachable_code)]
    Err("No hay paquete portable de Ollama para esta arquitectura.".into())
}

pub fn ensure_portable_binary(
    data_dir: &Path,
    mut progress: impl FnMut(i32, &str),
) -> Result<PathBuf, String> {
    if let Some(existing) = find_portable_binary(data_dir) {
        return Ok(existing);
    }
    let spec = archive_for_host()?;
    let dl = download_dir(data_dir);
    fs::create_dir_all(&dl).map_err(|e| format!("No se pudo crear {}: {}", dl.display(), e))?;
    let archive_path = dl.join(spec.filename);
    let mut last_err = String::new();
    let mut downloaded = false;
    for url in [Some(spec.url), spec.fallback].into_iter().flatten() {
        progress(-1, "Descargando Ollama (copia portable)...");
        match download_file(url, &archive_path, &mut progress) {
            Ok(()) => {
                downloaded = true;
                break;
            }
            Err(e) => {
                last_err = format!("{}: {}", url, e);
            }
        }
    }
    if !downloaded {
        return Err(format!("No se pudo descargar Ollama ({})", last_err));
    }
    progress(0, "Extrayendo Ollama...");
    let home = ollama_home(data_dir);
    fs::create_dir_all(&home).map_err(|e| e.to_string())?;
    match spec.kind {
        ArchiveKind::Zip => extract_zip(&archive_path, &home, &mut progress)?,
        ArchiveKind::Tgz => extract_tgz(&archive_path, &home, &mut progress)?,
    }
    let _ = fs::remove_file(&archive_path);
    let bin = find_portable_binary(data_dir).ok_or_else(|| {
        format!(
            "El archivo se extrajo en {} pero no aparece {}",
            home.display(),
            binary_name()
        )
    })?;
    ensure_executable(&bin);
    Ok(bin)
}

fn ensure_executable(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = fs::metadata(path) {
            let mut perms = meta.permissions();
            perms.set_mode(perms.mode() | 0o755);
            let _ = fs::set_permissions(path, perms);
        }
    }
    #[cfg(not(unix))]
    let _ = path;
}

fn download_file(
    url: &str,
    dest: &Path,
    progress: &mut impl FnMut(i32, &str),
) -> Result<(), String> {
    let tmp = dest.with_extension("part");
    let _ = fs::remove_file(&tmp);
    let mut errors = Vec::new();

    progress(-1, "Descargando Ollama con curl...");
    match download_with_curl(url, &tmp).and_then(|()| validate_download(&tmp)) {
        Ok(()) => return promote_download(&tmp, dest),
        Err(e) => {
            errors.push(e);
            let _ = fs::remove_file(&tmp);
        }
    }

    #[cfg(windows)]
    {
        progress(-1, "curl no disponible; intentando PowerShell...");
        match download_with_powershell(url, &tmp).and_then(|()| validate_download(&tmp)) {
            Ok(()) => return promote_download(&tmp, dest),
            Err(e) => {
                errors.push(e);
                let _ = fs::remove_file(&tmp);
            }
        }
    }

    Err(format!(
        "fallaron los descargadores del sistema: {}",
        errors.join("; ")
    ))
}

fn download_with_curl(url: &str, tmp: &Path) -> Result<(), String> {
    let program = if cfg!(windows) { "curl.exe" } else { "curl" };
    let mut cmd = Command::new(program);
    cmd.args([
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--connect-timeout",
        "30",
        "--max-time",
        "21600",
        "--retry",
        "2",
        "--retry-delay",
        "2",
        "--output",
    ])
    .arg(tmp)
    .arg(url)
    .stdout(Stdio::null())
    .stderr(Stdio::null());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    match cmd.status() {
        Ok(status) if status.success() => Ok(()),
        Ok(status) => Err(format!("{} termino con {}", program, status)),
        Err(e) => Err(format!("no se pudo ejecutar {}: {}", program, e)),
    }
}

#[cfg(windows)]
fn download_with_powershell(url: &str, tmp: &Path) -> Result<(), String> {
    let script = concat!(
        "$ProgressPreference='SilentlyContinue';",
        "$ErrorActionPreference='Stop';",
        "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;",
        "Invoke-WebRequest -UseBasicParsing ",
        "-Uri $env:SMARTGASTOS_OLLAMA_URL ",
        "-OutFile $env:SMARTGASTOS_OLLAMA_DEST"
    );
    let mut cmd = Command::new("powershell.exe");
    cmd.args([
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ])
    .env("SMARTGASTOS_OLLAMA_URL", url)
    .env("SMARTGASTOS_OLLAMA_DEST", tmp)
    .stdout(Stdio::null())
    .stderr(Stdio::null())
    .creation_flags(CREATE_NO_WINDOW);

    match cmd.status() {
        Ok(status) if status.success() => Ok(()),
        Ok(status) => Err(format!("powershell.exe termino con {}", status)),
        Err(e) => Err(format!("no se pudo ejecutar powershell.exe: {}", e)),
    }
}

fn validate_download(path: &Path) -> Result<(), String> {
    match fs::metadata(path) {
        Ok(meta) if meta.is_file() && meta.len() > 0 => Ok(()),
        Ok(_) => Err(format!("{} quedo vacio", path.display())),
        Err(e) => Err(format!("{} no fue creado: {}", path.display(), e)),
    }
}

fn promote_download(tmp: &Path, dest: &Path) -> Result<(), String> {
    if dest.exists() {
        fs::remove_file(dest)
            .map_err(|e| format!("No se pudo reemplazar {}: {}", dest.display(), e))?;
    }
    fs::rename(tmp, dest).map_err(|e| format!("rename: {}", e))
}

fn safe_join(dest: &Path, name: &Path) -> Option<PathBuf> {
    if name.is_absolute() {
        return None;
    }
    if name.components().any(|c| matches!(c, Component::ParentDir)) {
        return None;
    }
    Some(dest.join(name))
}

fn extract_zip(
    zip_path: &Path,
    dest: &Path,
    progress: &mut impl FnMut(i32, &str),
) -> Result<(), String> {
    let file = File::open(zip_path).map_err(|e| e.to_string())?;
    let mut archive = zip::ZipArchive::new(file).map_err(|e| format!("zip: {}", e))?;
    let n = archive.len().max(1);
    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| e.to_string())?;
        let rel = entry.mangled_name();
        let out = match safe_join(dest, &rel) {
            Some(p) => p,
            None => continue,
        };
        if entry.is_dir() || rel.as_os_str().to_string_lossy().ends_with('/') {
            fs::create_dir_all(&out).map_err(|e| e.to_string())?;
        } else {
            if let Some(parent) = out.parent() {
                fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            }
            let mut outfile = File::create(&out).map_err(|e| e.to_string())?;
            io::copy(&mut entry, &mut outfile).map_err(|e| e.to_string())?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if let Some(mode) = entry.unix_mode() {
                    let _ = fs::set_permissions(&out, fs::Permissions::from_mode(mode));
                }
            }
        }
        let pct = (((i + 1) * 100) / n) as i32;
        progress(pct, &format!("Extrayendo Ollama — {}%", pct));
    }
    Ok(())
}

fn extract_tgz(
    tgz_path: &Path,
    dest: &Path,
    progress: &mut impl FnMut(i32, &str),
) -> Result<(), String> {
    let file = File::open(tgz_path).map_err(|e| e.to_string())?;
    let gz = flate2::read::GzDecoder::new(file);
    let mut archive = tar::Archive::new(gz);
    let entries = archive.entries().map_err(|e| format!("tar: {}", e))?;
    let mut i = 0u64;
    for entry in entries {
        let mut entry = entry.map_err(|e| e.to_string())?;
        let rel = entry.path().map_err(|e| e.to_string())?.into_owned();
        if safe_join(dest, &rel).is_none() {
            continue;
        }
        entry.unpack_in(dest).map_err(|e| e.to_string())?;
        i += 1;
        if i == 1 || i % 8 == 0 {
            progress(-1, &format!("Extrayendo Ollama ({} archivos)...", i));
        }
    }
    progress(100, "Extrayendo Ollama — 100%");
    Ok(())
}

pub fn spawn_serve(bin: &Path, port: u16, models: &Path) -> io::Result<Child> {
    let _ = fs::create_dir_all(models);
    let host = format!("127.0.0.1:{}", port);
    let mut cmd = Command::new(bin);
    cmd.arg("serve")
        .env("OLLAMA_HOST", &host)
        .env("OLLAMA_MODELS", models)
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Some(dir) = bin.parent() {
        cmd.current_dir(dir);
    }
    #[cfg(windows)]
    {
        cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    }
    #[cfg(unix)]
    {
        cmd.process_group(0);
    }
    cmd.spawn()
}

/// Kill whoever is LISTENING on `port` (orphan uvicorn after PyInstaller parent dies).
pub fn kill_listeners_on_port(port: u16) {
    #[cfg(windows)]
    {
        let Ok(output) = Command::new("netstat")
            .args(["-ano", "-p", "tcp"])
            .creation_flags(CREATE_NO_WINDOW)
            .output()
        else {
            return;
        };
        let text = String::from_utf8_lossy(&output.stdout);
        let port_s = port.to_string();
        let mut pids = std::collections::BTreeSet::new();
        for line in text.lines() {
            let cols: Vec<&str> = line.split_whitespace().collect();
            if cols.len() < 5 || !cols.iter().any(|c| *c == "LISTENING") {
                continue;
            }
            let local = cols[1];
            let local_port = local.rsplit(':').next().unwrap_or("");
            if local_port != port_s {
                continue;
            }
            if let Ok(pid) = cols[cols.len() - 1].parse::<u32>() {
                if pid > 0 {
                    pids.insert(pid);
                }
            }
        }
        for pid in pids {
            kill_pid_tree(pid);
        }
    }
    #[cfg(unix)]
    {
        if let Ok(output) = Command::new("lsof")
            .args(["-iTCP", &format!(":{}", port), "-sTCP:LISTEN", "-t"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output()
        {
            let text = String::from_utf8_lossy(&output.stdout);
            for line in text.lines() {
                if let Ok(pid) = line.trim().parse::<u32>() {
                    if pid > 0 {
                        kill_pid_tree(pid);
                    }
                }
            }
        }
    }
}

/// Kill a process and its descendants (Windows: taskkill /T /F).
pub fn kill_pid_tree(pid: u32) {
    #[cfg(windows)]
    {
        let mut killer = Command::new("taskkill");
        killer
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        killer.creation_flags(CREATE_NO_WINDOW);
        let _ = killer.status();
    }
    #[cfg(unix)]
    {
        let pg = format!("-{}", pid);
        let _ = Command::new("kill")
            .args(["-TERM", &pg])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        std::thread::sleep(Duration::from_millis(400));
        let _ = Command::new("kill")
            .args(["-KILL", &pg])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

/// Kill only the process tree of an Ollama we spawned. Never kill by process name.
pub fn kill_owned_child(child: &mut Child) {
    kill_pid_tree(child.id());
    let _ = child.kill();
    let _ = child.wait();
}

pub fn model_is_present(port: u16, model: &str) -> bool {
    let url = format!("{}/api/tags", api_base(port));
    let Ok(resp) = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(5))
        .build()
        .get(&url)
        .call()
    else {
        return false;
    };
    let Ok(body) = resp.into_string() else {
        return false;
    };
    let needle = format!("\"name\":\"{}\"", model);
    let needle_sp = format!("\"name\": \"{}\"", model);
    body.contains(&needle) || body.contains(&needle_sp)
}

pub fn pull_model(port: u16, model: &str, mut progress: impl FnMut(i32, &str)) -> bool {
    if model_is_present(port, model) {
        progress(100, &format!("Modelo {} ya estaba disponible.", model));
        return true;
    }
    let body = format!("{{\"name\":\"{}\"}}", model);
    let url = format!("{}/api/pull", api_base(port));
    let resp = http_agent()
        .post(&url)
        .set("Content-Type", "application/json")
        .send_string(&body);
    let resp = match resp {
        Ok(r) => r,
        Err(_) => return false,
    };
    let reader = std::io::BufReader::new(resp.into_reader());
    let mut ok = false;
    for line in std::io::BufRead::lines(reader) {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let v: serde_json::Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        if v.get("error").is_some() {
            ok = false;
            break;
        }
        let status = v.get("status").and_then(|s| s.as_str()).unwrap_or("");
        let total = v.get("total").and_then(|t| t.as_u64());
        let completed = v.get("completed").and_then(|c| c.as_u64());
        let pct: i32 = match (total, completed) {
            (Some(t), Some(c)) if t > 0 => ((c.min(t) * 100) / t) as i32,
            _ => -1,
        };
        let msg = if pct >= 0 {
            format!("Descargando el modelo {} — {}%", model, pct)
        } else {
            format!("Preparando el modelo {} ({})...", model, status)
        };
        progress(pct, &msg);
        if status == "success" {
            ok = true;
        }
    }
    ok
}
