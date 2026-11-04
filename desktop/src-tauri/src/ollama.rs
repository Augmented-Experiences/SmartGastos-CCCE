//! Portable in-app Ollama shared by SmartGastos, SmartRedes, and SmartCaja.
//! Reuse a healthy daemon on 11434. Otherwise download the official zip/tgz
//! into the shared SmartSuite engine home, start `serve` with `OLLAMA_MODELS`
//! pointed at that home, and pull models. A daemon this process starts stays
//! up after the window closes. Never runs the Windows system installer.

use std::fs::{self, File, OpenOptions};
use std::io;
use std::net::{TcpListener, TcpStream};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

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
const SUITE_FOLDER: &str = "SmartSuite";
const LEGACY_APP_DIRS: [&str; 3] = ["SmartGastos", "SmartRedes", "SmartCaja"];
const MODEL_TREE_DIRS: [&str; 2] = ["blobs", "manifests"];
/// Same walk the previous per-app finder used (files up to three directories down).
const BINARY_SEARCH_DEPTH: u8 = 4;

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

/// Per-app state (`active_model.txt`, `active_profile.json`). Not the shared engine.
pub fn ollama_home(data_dir: &Path) -> PathBuf {
    data_dir.join("ollama")
}

fn data_base_from_app(app_data_dir: &Path) -> PathBuf {
    app_data_dir
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| app_data_dir.to_path_buf())
}

/// Shared engine. `app_data_dir` is only used to find the same base as `user_data_dir()`.
pub fn engine_home(app_data_dir: &Path) -> PathBuf {
    data_base_from_app(app_data_dir)
        .join(SUITE_FOLDER)
        .join("ollama")
}

pub fn models_dir(data_dir: &Path) -> PathBuf {
    engine_home(data_dir).join("models")
}

fn download_dir(data_dir: &Path) -> PathBuf {
    engine_home(data_dir).join("download")
}

fn owned_pid_path(data_dir: &Path) -> PathBuf {
    engine_home(data_dir).join(OWNED_PID_FILE)
}

pub fn record_owned_pid(data_dir: &Path, pid: u32) {
    let home = engine_home(data_dir);
    let _ = fs::create_dir_all(&home);
    let _ = fs::write(owned_pid_path(data_dir), pid.to_string());
}

pub fn clear_owned_pid(data_dir: &Path) {
    let _ = fs::remove_file(owned_pid_path(data_dir));
}

fn legacy_engine_homes(app_data_dir: &Path) -> Vec<PathBuf> {
    let base = data_base_from_app(app_data_dir);
    let mut homes: Vec<PathBuf> = LEGACY_APP_DIRS
        .iter()
        .map(|name| base.join(name).join("ollama"))
        .collect();
    let own = ollama_home(app_data_dir);
    if !homes.iter().any(|home| paths_equal(home, &own)) {
        homes.push(own);
    }
    homes
}

fn legacy_model_dirs(app_data_dir: &Path) -> Vec<PathBuf> {
    legacy_engine_homes(app_data_dir)
        .into_iter()
        .map(|home| home.join("models"))
        .collect()
}

fn paths_equal(a: &Path, b: &Path) -> bool {
    if let (Ok(left), Ok(right)) = (fs::canonicalize(a), fs::canonicalize(b)) {
        return left == right;
    }
    a.to_string_lossy()
        .eq_ignore_ascii_case(&b.to_string_lossy())
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

fn find_portable_binary(home: &Path) -> Option<PathBuf> {
    if !home.is_dir() {
        return None;
    }
    find_named(home, binary_name(), BINARY_SEARCH_DEPTH)
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
        let name = ent.file_name();
        if name == "models" || name == "active_model.txt" {
            continue;
        }
        let p = ent.path();
        if p.is_dir() {
            if let Some(found) = find_named(&p, want, depth - 1) {
                return Some(found);
            }
        } else if name == want {
            return Some(p);
        }
    }
    None
}

enum EngineBinary {
    Ready(PathBuf),
    NeedsDownload,
}

fn prepare_engine(data_dir: &Path, is_running: &dyn Fn(&Path) -> bool) -> EngineBinary {
    prepare_engine_at(
        &engine_home(data_dir),
        &legacy_engine_homes(data_dir),
        is_running,
    )
}

fn prepare_engine_at(
    engine: &Path,
    legacy_homes: &[PathBuf],
    is_running: &dyn Fn(&Path) -> bool,
) -> EngineBinary {
    if let Some(bin) = find_portable_binary(engine) {
        return EngineBinary::Ready(bin);
    }
    for legacy in legacy_homes {
        if let Some(bin) = find_portable_binary(legacy) {
            if is_running(&bin) {
                return EngineBinary::Ready(bin);
            }
            let _ = move_engine_tree(legacy, engine);
            if let Some(moved) = find_portable_binary(engine) {
                return EngineBinary::Ready(moved);
            }
            if let Some(still) = find_portable_binary(legacy) {
                return EngineBinary::Ready(still);
            }
        }
    }
    EngineBinary::NeedsDownload
}

fn binary_is_running(path: &Path) -> bool {
    let target = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let mut system = sysinfo::System::new();
    system.refresh_processes_specifics(
        sysinfo::ProcessesToUpdate::All,
        true,
        sysinfo::ProcessRefreshKind::nothing().with_exe(sysinfo::UpdateKind::Always),
    );
    system.processes().values().any(|proc| {
        proc.exe()
            .map(|exe| same_executable(exe, &target))
            .unwrap_or(false)
    })
}

fn same_executable(exe: &Path, target: &Path) -> bool {
    if exe == target {
        return true;
    }
    if let Ok(canon) = fs::canonicalize(exe) {
        if canon == target {
            return true;
        }
    }
    exe.to_string_lossy()
        .eq_ignore_ascii_case(&target.to_string_lossy())
}

fn is_engine_excluded(name: &std::ffi::OsStr) -> bool {
    matches!(
        name.to_str(),
        Some("models" | "active_model.txt" | "active_profile.json" | "owned.pid" | "logs")
    )
}

/// Move the legacy engine tree into the shared home. Relative paths stay
/// (`runtime\ollama.exe` remains `runtime\ollama.exe`). Model blobs and
/// per-app markers stay where they are.
fn move_engine_tree(src: &Path, dst: &Path) -> io::Result<()> {
    fs::create_dir_all(dst)?;
    for ent in fs::read_dir(src)? {
        let ent = ent?;
        if is_engine_excluded(&ent.file_name()) {
            continue;
        }
        let from = ent.path();
        let to = dst.join(ent.file_name());
        if from.is_dir() {
            move_engine_tree(&from, &to)?;
            let _ = fs::remove_dir(&from);
            continue;
        }
        if to.exists() {
            continue;
        }
        relocate_file(&from, &to)?;
    }
    Ok(())
}

fn relocate_file(from: &Path, to: &Path) -> io::Result<()> {
    if let Some(parent) = to.parent() {
        fs::create_dir_all(parent)?;
    }
    if fs::rename(from, to).is_ok() {
        return Ok(());
    }
    fs::copy(from, to)?;
    fs::remove_file(from)?;
    Ok(())
}

#[derive(Default)]
struct ImportCounts {
    moved: u32,
    deleted_same: u32,
    left_different: u32,
    skipped: u32,
}

/// Import legacy blobs and manifests into the shared models directory.
/// If another app holds `migrate.lock`, wait and then continue startup.
pub fn import_legacy_models(app_data_dir: &Path) -> String {
    let engine = engine_home(app_data_dir);
    let sources = legacy_model_dirs(app_data_dir);
    import_model_sources(&engine, &sources, Duration::from_secs(60))
}

fn import_model_sources(engine: &Path, sources: &[PathBuf], wait: Duration) -> String {
    let dest = engine.join("models");
    let Some(_lock) = acquire_migrate_lock(engine, wait) else {
        return "migrate.lock ocupado; se sigue sin importar".into();
    };
    let mut totals = ImportCounts::default();
    for src in sources {
        if paths_equal(src, &dest) {
            continue;
        }
        let counts = import_blobs_and_manifests(src, &dest);
        totals.moved += counts.moved;
        totals.deleted_same += counts.deleted_same;
        totals.left_different += counts.left_different;
        totals.skipped += counts.skipped;
    }
    format!(
        "importados={} duplicados={} distintos={} omitidos={}",
        totals.moved, totals.deleted_same, totals.left_different, totals.skipped
    )
}

fn import_blobs_and_manifests(src_models: &Path, dst_models: &Path) -> ImportCounts {
    let mut counts = ImportCounts::default();
    if paths_equal(src_models, dst_models) {
        return counts;
    }
    for sub in MODEL_TREE_DIRS {
        let src = src_models.join(sub);
        if src.is_dir() {
            import_files(&src, &dst_models.join(sub), &mut counts);
        }
    }
    counts
}

fn import_files(src: &Path, dst: &Path, counts: &mut ImportCounts) {
    let Ok(rd) = fs::read_dir(src) else {
        counts.skipped += 1;
        return;
    };
    if fs::create_dir_all(dst).is_err() {
        counts.skipped += 1;
        return;
    }
    for ent in rd.flatten() {
        let from = ent.path();
        let to = dst.join(ent.file_name());
        if from.is_dir() {
            import_files(&from, &to, counts);
            let _ = fs::remove_dir(&from);
            continue;
        }
        if !from.is_file() {
            continue;
        }
        let src_len = match fs::metadata(&from) {
            Ok(meta) => meta.len(),
            Err(_) => {
                counts.skipped += 1;
                continue;
            }
        };
        match fs::metadata(&to) {
            Ok(dest_meta) => {
                if dest_meta.len() == src_len {
                    match fs::remove_file(&from) {
                        Ok(()) => counts.deleted_same += 1,
                        Err(_) => counts.skipped += 1,
                    }
                } else {
                    counts.left_different += 1;
                }
            }
            Err(_) => match move_file_exclusive(&from, &to) {
                Ok(()) => counts.moved += 1,
                Err(_) => counts.skipped += 1,
            },
        }
    }
}

fn move_file_exclusive(from: &Path, to: &Path) -> io::Result<()> {
    if let Some(parent) = to.parent() {
        fs::create_dir_all(parent)?;
    }
    if fs::rename(from, to).is_ok() {
        return Ok(());
    }
    let tmp_name = format!(
        ".{}.importing",
        to.file_name().and_then(|n| n.to_str()).unwrap_or("blob")
    );
    let tmp = to.with_file_name(tmp_name);
    let copied = fs::copy(from, &tmp)?;
    let src_len = fs::metadata(from)?.len();
    if copied != src_len {
        let _ = fs::remove_file(&tmp);
        return Err(io::Error::new(io::ErrorKind::Other, "copia incompleta"));
    }
    if fs::rename(&tmp, to).is_err() {
        let _ = fs::remove_file(&tmp);
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "destino ocupado",
        ));
    }
    let _ = fs::remove_file(from);
    Ok(())
}

fn acquire_migrate_lock(home: &Path, wait: Duration) -> Option<File> {
    let _ = fs::create_dir_all(home);
    let path = home.join("migrate.lock");
    let deadline = Instant::now() + wait;
    loop {
        match open_exclusive(&path) {
            Ok(file) => return Some(file),
            Err(_) => {
                if Instant::now() >= deadline {
                    return None;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        }
    }
}

fn open_exclusive(path: &Path) -> io::Result<File> {
    let mut opts = OpenOptions::new();
    opts.read(true).write(true).create(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        opts.share_mode(0);
    }
    let file = opts.open(path)?;
    #[cfg(unix)]
    {
        if !flock_exclusive(&file) {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "migrate.lock ocupado",
            ));
        }
    }
    Ok(file)
}

#[cfg(unix)]
fn flock_exclusive(file: &File) -> bool {
    extern "C" {
        fn flock(fd: i32, op: i32) -> i32;
    }
    const LOCK_EX: i32 = 2;
    const LOCK_NB: i32 = 4;
    use std::os::unix::io::AsRawFd;
    let fd = file.as_raw_fd();
    unsafe { flock(fd, LOCK_EX | LOCK_NB) == 0 }
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
    match prepare_engine(data_dir, &binary_is_running) {
        EngineBinary::Ready(existing) => {
            ensure_executable(&existing);
            return Ok(existing);
        }
        EngineBinary::NeedsDownload => {}
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
    let home = engine_home(data_dir);
    fs::create_dir_all(&home).map_err(|e| e.to_string())?;
    match spec.kind {
        ArchiveKind::Zip => extract_zip(&archive_path, &home, &mut progress)?,
        ArchiveKind::Tgz => extract_tgz(&archive_path, &home, &mut progress)?,
    }
    let _ = fs::remove_file(&archive_path);
    let bin = find_portable_binary(&home).ok_or_else(|| {
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

/// Start `ollama serve`. `models` is the shared SmartSuite models directory.
/// The same `OLLAMA_MODELS` value is set for a portable binary and for an
/// `ollama` already on `PATH`.
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

fn tags_body_has_model(body: &str, model: &str) -> bool {
    let needle = format!("\"name\":\"{}\"", model);
    let needle_sp = format!("\"name\": \"{}\"", model);
    body.contains(&needle) || body.contains(&needle_sp)
}

/// `true` when `/api/tags` already lists `model`, so `pull_model` must not POST.
fn pull_suppressed_by_tags(body: &str, model: &str) -> bool {
    tags_body_has_model(body, model)
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
    pull_suppressed_by_tags(&body, model)
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

#[cfg(test)]
mod tests {
    use super::*;

    struct Scratch(PathBuf);

    impl Scratch {
        fn new(label: &str) -> Self {
            let dir = std::env::temp_dir().join(format!(
                "sg-ollama-{label}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_nanos())
                    .unwrap_or(0)
            ));
            let _ = fs::remove_dir_all(&dir);
            fs::create_dir_all(&dir).unwrap();
            Scratch(dir)
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn engine_home_is_smartsuite_and_active_model_stays_under_app_data() {
        let base = PathBuf::from("roaming-base");
        let app = base.join("SmartGastos");
        let engine = engine_home(&app);
        assert_eq!(engine, base.join("SmartSuite").join("ollama"));
        let marker = ollama_home(&app).join("active_model.txt");
        assert_eq!(marker, app.join("ollama").join("active_model.txt"));
        assert!(marker.starts_with(&app));
        assert!(!marker.starts_with(&engine));
        assert_eq!(owned_pid_path(&app), engine.join("owned.pid"));
        assert_eq!(models_dir(&app), engine.join("models"));
        assert_eq!(legacy_engine_homes(&app).len(), 3);
        let profile = ollama_home(&app).join("active_profile.json");
        assert_eq!(profile, app.join("ollama").join("active_profile.json"));
    }

    #[test]
    fn legacy_ollama_exe_suppresses_zip_download() {
        let scratch = Scratch::new("legacy");
        let legacy = scratch.0.join("SmartGastos").join("ollama");
        let runtime = legacy.join("runtime");
        fs::create_dir_all(runtime.join("lib")).unwrap();
        fs::write(runtime.join(binary_name()), b"engine").unwrap();
        fs::write(runtime.join("lib").join("ollama.dll"), b"dll").unwrap();
        fs::create_dir_all(legacy.join("models").join("blobs")).unwrap();
        fs::write(legacy.join("models").join("blobs").join("sha256-abc"), b"blob").unwrap();
        fs::write(legacy.join("models").join(binary_name()), b"not-the-binary").unwrap();
        fs::write(legacy.join("active_model.txt"), b"llama3.2:3b").unwrap();
        fs::write(legacy.join("active_profile.json"), b"{}").unwrap();
        fs::write(legacy.join("owned.pid"), b"99").unwrap();
        fs::create_dir_all(legacy.join("logs")).unwrap();
        fs::write(legacy.join("logs").join("ollama.log"), b"old").unwrap();

        let engine = scratch.0.join("SmartSuite").join("ollama");
        match prepare_engine_at(&engine, &[legacy.clone()], &|_| false) {
            EngineBinary::Ready(path) => {
                assert_eq!(path, engine.join("runtime").join(binary_name()));
            }
            EngineBinary::NeedsDownload => {
                panic!("un ollama.exe legado no debe pedir el zip");
            }
        }
        assert_eq!(
            fs::read(engine.join("runtime").join(binary_name())).unwrap(),
            b"engine"
        );
        assert_eq!(
            fs::read(engine.join("runtime").join("lib").join("ollama.dll")).unwrap(),
            b"dll"
        );
        assert!(!runtime.join(binary_name()).is_file());
        assert_eq!(fs::read(legacy.join("active_model.txt")).unwrap(), b"llama3.2:3b");
        assert!(legacy.join("active_profile.json").is_file());
        assert_eq!(fs::read(legacy.join("owned.pid")).unwrap(), b"99");
        assert_eq!(
            fs::read(legacy.join("logs").join("ollama.log")).unwrap(),
            b"old"
        );
        assert_eq!(
            fs::read(legacy.join("models").join("blobs").join("sha256-abc")).unwrap(),
            b"blob"
        );
        assert_eq!(
            fs::read(legacy.join("models").join(binary_name())).unwrap(),
            b"not-the-binary"
        );
        assert!(!engine.join("active_model.txt").exists());
        assert!(!engine.join("owned.pid").exists());
        assert!(!engine.join("models").exists());
    }

    #[test]
    fn running_legacy_binary_is_used_in_place_without_download() {
        let scratch = Scratch::new("running");
        let legacy = scratch.0.join("SmartRedes").join("ollama");
        let exe = legacy.join("runtime").join(binary_name());
        fs::create_dir_all(exe.parent().unwrap()).unwrap();
        fs::write(&exe, b"live").unwrap();
        let engine = scratch.0.join("SmartSuite").join("ollama");
        match prepare_engine_at(&engine, &[legacy], &|_| true) {
            EngineBinary::Ready(path) => assert_eq!(path, exe),
            EngineBinary::NeedsDownload => panic!("un binario en ejecucion no debe pedir el zip"),
        }
        assert!(exe.is_file());
        assert!(!engine.join("runtime").join(binary_name()).exists());
    }

    #[test]
    fn binary_inside_models_does_not_count_as_engine() {
        let scratch = Scratch::new("models-bin");
        let legacy = scratch.0.join("SmartCaja").join("ollama");
        fs::create_dir_all(legacy.join("models")).unwrap();
        fs::write(legacy.join("models").join(binary_name()), b"nope").unwrap();
        fs::write(legacy.join("active_model.txt"), b"llama").unwrap();
        let engine = scratch.0.join("SmartSuite").join("ollama");
        assert!(matches!(
            prepare_engine_at(&engine, &[legacy], &|_| false),
            EngineBinary::NeedsDownload
        ));
    }

    #[test]
    fn shared_binary_suppresses_zip_and_leaves_legacy() {
        let scratch = Scratch::new("shared");
        let engine = scratch.0.join("SmartSuite").join("ollama");
        fs::create_dir_all(&engine).unwrap();
        let exe = engine.join(binary_name());
        fs::write(&exe, b"shared").unwrap();
        let legacy = scratch.0.join("SmartGastos").join("ollama");
        fs::create_dir_all(&legacy).unwrap();
        fs::write(legacy.join(binary_name()), b"legacy").unwrap();
        match prepare_engine_at(&engine, &[legacy.clone()], &|_| false) {
            EngineBinary::Ready(path) => assert_eq!(path, exe),
            EngineBinary::NeedsDownload => panic!("el binario compartido no debe pedir el zip"),
        }
        assert_eq!(fs::read(legacy.join(binary_name())).unwrap(), b"legacy");
    }

    #[test]
    fn import_moves_blobs_and_respects_existing_size() {
        let scratch = Scratch::new("import");
        let src = scratch.0.join("SmartGastos").join("ollama").join("models");
        let engine = scratch.0.join("SmartSuite").join("ollama");
        let dst = engine.join("models");
        fs::create_dir_all(src.join("blobs")).unwrap();
        fs::create_dir_all(src.join("manifests").join("library").join("llama")).unwrap();
        fs::write(src.join("blobs").join("sha-new"), b"abcdef").unwrap();
        fs::write(src.join("blobs").join("sha-same"), b"same").unwrap();
        fs::write(src.join("blobs").join("sha-diff"), b"short").unwrap();
        fs::write(
            src.join("manifests").join("library").join("llama").join("3b"),
            b"manifest",
        )
        .unwrap();
        fs::create_dir_all(dst.join("blobs")).unwrap();
        fs::write(dst.join("blobs").join("sha-same"), b"same").unwrap();
        fs::write(dst.join("blobs").join("sha-diff"), b"different-size").unwrap();

        let note = import_model_sources(&engine, &[src.clone()], Duration::from_secs(2));
        assert!(note.contains("importados=2"), "{note}");
        assert!(note.contains("duplicados=1"), "{note}");
        assert!(note.contains("distintos=1"), "{note}");
        assert!(!src.join("blobs").join("sha-new").exists());
        assert_eq!(fs::read(dst.join("blobs").join("sha-new")).unwrap(), b"abcdef");
        assert!(!src.join("blobs").join("sha-same").exists());
        assert_eq!(fs::read(dst.join("blobs").join("sha-same")).unwrap(), b"same");
        assert_eq!(fs::read(src.join("blobs").join("sha-diff")).unwrap(), b"short");
        assert_eq!(
            fs::read(dst.join("blobs").join("sha-diff")).unwrap(),
            b"different-size"
        );
        assert_eq!(
            fs::read(dst.join("manifests").join("library").join("llama").join("3b")).unwrap(),
            b"manifest"
        );
    }

    #[test]
    fn migrate_lock_is_exclusive_then_released() {
        let scratch = Scratch::new("lock");
        let engine = scratch.0.join("engine");
        let first = acquire_migrate_lock(&engine, Duration::from_secs(0)).expect("primer lock");
        assert!(engine.join("migrate.lock").is_file());
        assert!(acquire_migrate_lock(&engine, Duration::from_millis(300)).is_none());
        drop(first);
        assert!(acquire_migrate_lock(&engine, Duration::from_secs(2)).is_some());
    }

    #[test]
    fn tag_already_in_api_tags_suppresses_pull() {
        let body = r#"{"models":[{"name":"llama3.2:3b"},{"name": "moondream"}]}"#;
        assert!(pull_suppressed_by_tags(body, "llama3.2:3b"));
        assert!(pull_suppressed_by_tags(body, "moondream"));
        assert!(!pull_suppressed_by_tags(body, "llama3.1:8b"));
    }

    #[test]
    fn other_app_model_dir_is_also_a_source() {
        let base = PathBuf::from("roaming-base");
        let app = base.join("OtherApp");
        let dirs = legacy_model_dirs(&app);
        assert!(dirs.iter().any(|p| p.ends_with(
            Path::new("SmartGastos").join("ollama").join("models")
        )));
        assert!(dirs.iter().any(|p| p.ends_with(
            Path::new("SmartRedes").join("ollama").join("models")
        )));
        assert!(dirs.iter().any(|p| p.ends_with(
            Path::new("SmartCaja").join("ollama").join("models")
        )));
        assert!(dirs.iter().any(|p| p.ends_with(
            Path::new("OtherApp").join("ollama").join("models")
        )));
    }
}
