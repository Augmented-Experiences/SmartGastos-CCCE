"""
test_pinokio_config.py — Configuración del instalador de escritorio y de la UI.
El launcher Pinokio no forma parte del release.
"""

import unittest
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()


class TestDefaultsAgentsJson(unittest.TestCase):
    """Tests para defaults/agents.json."""

    def setUp(self):
        self.path = REPO_ROOT / "defaults" / "agents.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def test_file_exists(self):
        self.assertTrue(self.path.exists())

    def test_has_agents_key(self):
        """Debe tener clave 'agents'."""
        self.assertIn("agents", self.data)

    def test_has_required_agents(self):
        """Debe tener los 6 agentes del pipeline."""
        agent_ids = [a["id"] for a in self.data["agents"]]
        required = ["ocr", "vision", "extractor", "clasificador", "auditor", "recomendador"]
        for req in required:
            self.assertIn(req, agent_ids, f"Agente faltante: {req}")

    def test_agents_have_required_fields(self):
        """Cada agente debe tener campos requeridos."""
        for agent in self.data["agents"]:
            self.assertIn("id", agent)
            self.assertIn("nombre", agent)
            self.assertIn("model", agent)
            self.assertIn("tipo", agent)


class TestDefaultPrompts(unittest.TestCase):
    """Tests para prompts por defecto."""

    def test_general_prompt_has_security(self):
        """Prompt general debe tener cláusula de seguridad."""
        path = REPO_ROOT / "defaults" / "prompts" / "general.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
            self.assertIn("INQUEBRANTABLES", content)

    def test_analyst_prompt_has_security(self):
        """Prompt de analista debe tener cláusula de seguridad."""
        path = REPO_ROOT / "defaults" / "prompts" / "analyst.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
            self.assertIn("INQUEBRANTABLES", content)


class TestNoOldJsFiles(unittest.TestCase):
    """Verifica que no existan archivos .js obsoletos."""

    def test_no_install_js(self):
        self.assertFalse((REPO_ROOT / "install.js").exists())

    def test_no_start_js(self):
        self.assertFalse((REPO_ROOT / "start.js").exists())

    def test_no_stop_js(self):
        self.assertFalse((REPO_ROOT / "stop.js").exists())


class TestRequirementsTxt(unittest.TestCase):
    """Tests para requirements.txt."""

    def setUp(self):
        self.path = REPO_ROOT / "requirements.txt"
        self.content = self.path.read_text(encoding="utf-8")

    def test_file_exists(self):
        self.assertTrue(self.path.exists())

    def test_has_fastapi(self):
        self.assertIn("fastapi", self.content.lower())

    def test_has_uvicorn(self):
        self.assertIn("uvicorn", self.content.lower())

    def test_has_sqlalchemy(self):
        self.assertIn("sqlalchemy", self.content.lower())

    def test_has_requests(self):
        self.assertIn("requests", self.content.lower())

    def test_has_pillow(self):
        """Pillow es necesario para procesamiento de imágenes."""
        self.assertIn("pillow", self.content.lower())


class TestUIFiles(unittest.TestCase):
    """Tests para archivos de UI."""

    def test_index_html_exists(self):
        self.assertTrue((REPO_ROOT / "app" / "index.html").exists())

    def test_icon_exists(self):
        self.assertTrue((REPO_ROOT / "icon.png").exists())

    def test_favicon_exists(self):
        self.assertTrue((REPO_ROOT / "app" / "favicon.ico").exists())
        self.assertTrue((REPO_ROOT / "desktop" / "src-tauri" / "icons" / "icon.ico").exists())

    def test_logo_exists(self):
        """Debe existir el isotipo CCCE en color y el mark para iconos."""
        app_dir = REPO_ROOT / "app"
        self.assertTrue((app_dir / "logo-ccce.png").exists(), "Falta app/logo-ccce.png (isotipo color)")
        self.assertTrue(
            (REPO_ROOT / "desktop" / "brand" / "ccce-mark.png").exists(),
            "Falta desktop/brand/ccce-mark.png (mark CCCE para iconos)",
        )
        has_legacy = (
            (app_dir / "logo-ccce-full.png").exists()
            or (app_dir / "logo.svg").exists()
            or (app_dir / "logo.png").exists()
        )
        self.assertTrue(has_legacy, "Debe existir logo-ccce-full.png, logo.svg o logo.png en app/")

    def test_desktop_smartsuite_config(self):
        cfg_path = REPO_ROOT / "desktop" / "smartsuite.config.json"
        self.assertTrue(cfg_path.exists(), "Falta desktop/smartsuite.config.json")
        import json
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("publisher"), "Cámara Colombiana de Comercio Electrónico (CCCE)")
        self.assertEqual(cfg.get("productName"), "SmartGastos")
        self.assertEqual(cfg.get("identifier"), "co.org.ccce.smartgastos")
        self.assertEqual(cfg.get("accent"), "#F4C10E")
        self.assertEqual(cfg.get("splashLogo"), "../app/logo-ccce.png")
        extra = (cfg.get("ollama") or {}).get("extraModels") or []
        tiers = (cfg.get("ollama") or {}).get("tiers") or []
        extras_in_tiers = [m for t in tiers for m in (t.get("extraModels") or [])]
        self.assertTrue(
            "moondream" in extra or "moondream" in extras_in_tiers,
            "moondream debe estar en extraModels globales o del perfil Completo/Máximo",
        )
        access = cfg.get("access") or {}
        self.assertEqual(access.get("blockBelowGb"), 7)
        self.assertEqual(access.get("warnBelowGb"), 13)

    def test_feedback_v2_tour_nav_and_new_company_flow(self):
        index = (REPO_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        i_asist = index.find('data-s="asistente-inicial"')
        i_agentes = index.find('data-s="agentes"')
        i_admin = index.find('data-s="admin"')
        self.assertGreater(i_asist, 0)
        self.assertLess(i_asist, i_agentes)
        self.assertLess(i_agentes, i_admin)
        self.assertIn("section: 'dashboard'", index)
        self.assertIn("showSection(step.section || 'dashboard')", index)
        self.assertIn("padding-right: 40px", index)
        self.assertIn("onclick=\"showOnboarding(true)\">+ Nueva</button>", index)
        self.assertNotIn("onclick=\"openModal('empresaModal')\">+ Nueva</button>", index)

    def test_feedback_v3_company_dropdown_and_cancel_upload(self):
        index = (REPO_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="topbarEmpresa"', index)
        self.assertIn("function onTopbarEmpresaChange", index)
        self.assertIn("function cancelUpload", index)
        self.assertIn("function stopPipelineStream", index)
        self.assertIn('onclick="cancelUpload()"', index)
        self.assertIn("Cancelar subida", index)
        self.assertIn("Quitar archivo", index)
        self.assertIn("emp-chevron", index)
        app_py = (REPO_ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn("request.is_disconnected()", app_py)

    def test_ui_uses_ccce_png_wordmark_and_favicon(self):
        index = (REPO_ROOT / "app" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="favicon.ico"', index)
        self.assertIn("logo-ccce.png", index)
        self.assertIn("ccce-about", index)
        canonical = (REPO_ROOT / "desktop" / "brand" / "splash.template.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("splash-mark.png", canonical)
        self.assertNotIn(".splash-mark", canonical)
        self.assertNotIn('class="splash-mark"', canonical)
        self.assertIn("splash-logo", canonical)
        self.assertIn("{{LOGO_HTML}}", canonical)
        self.assertIn('href="favicon.ico"', canonical)

    def test_ccce_palette_is_applied_to_splash_and_installer_theme(self):
        expected_navy = "radial-gradient(circle at 50% 30%, #1e4a94 0%, #0f2a5a 100%)"
        for relative_path in (
            "desktop/ui/splash.template.html",
            "desktop/ui/index.html",
        ):
            content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(expected_navy, content)
            self.assertIn("ccce-theme.css", content)
        for relative_path in ("desktop/brand/ccce-theme.css", "desktop/ui/ccce-theme.css"):
            content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("#173A7A", content)
            self.assertIn("#CE1126", content)
            self.assertIn("#F4C10E", content)

    def test_requirements_desktop_no_easyocr(self):
        lines = [
            ln.strip().lower()
            for ln in (REPO_ROOT / "requirements-desktop.txt").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        joined = "\n".join(lines)
        self.assertNotIn("easyocr", joined)
        self.assertNotIn("torch", joined)
        self.assertIn("rapidocr", joined)
        self.assertIn("onnxruntime", joined)

class TestPortableOllama(unittest.TestCase):
    """La app de escritorio descarga Ollama portable; no usa MSI ni torch/OCR extra."""

    def setUp(self):
        src = REPO_ROOT / "desktop" / "src-tauri" / "src"
        self.main = (src / "main.rs").read_text(encoding="utf-8")
        self.ollama = (src / "ollama.rs").read_text(encoding="utf-8")
        self.joined = self.main + "\n" + self.ollama
        self.splash = (REPO_ROOT / "desktop" / "ui" / "splash.template.html").read_text(
            encoding="utf-8"
        )

    def test_downloads_official_windows_zip_and_linux_tgz(self):
        self.assertIn("ollama-windows-amd64.zip", self.ollama)
        self.assertIn("ollama-linux-amd64.tgz", self.ollama)
        self.assertIn("releases/latest/download", self.ollama)

    def test_never_runs_windows_system_installer(self):
        lowered = self.joined.lower()
        for banned in ("ollamasetup.exe", "winget", ".msi", "verysilent", "install.sh"):
            self.assertNotIn(banned, lowered, f"no debe invocar instalador de sistema ({banned})")

    def test_sidecar_kills_backend_process_tree(self):
        self.assertIn("kill_pid_tree", self.joined)
        self.assertIn("taskkill", self.joined.lower())
        self.assertIn("kill_pid_tree(pid)", self.main)
        self.assertIn("kill_listeners_on_port", self.joined)
        self.assertIn("decide_listen_port", self.joined)
        self.assertIn("api/tags", self.ollama)
        self.assertIn("owned.pid", self.ollama)
        self.assertIn("record_owned_pid", self.joined)
        self.assertIn("kill_owned_child", self.joined)
        self.assertNotIn("Command::new(\"pkill\")", self.joined)
        self.assertNotIn("pkill -f", self.joined)
        self.assertNotIn("cierre retenido", self.main)
        self.assertNotIn("api.prevent_exit()", self.main)
        self.assertIn("se cierra la ventana de todos modos", self.main)

    def test_sidecar_gets_ollama_url_and_continues_without_restart(self):
        self.assertIn('env("OLLAMA_URL"', self.main)
        self.assertIn("ensure_portable_binary", self.main)
        self.assertIn("spawn_serve", self.main)
        self.assertIn("finish_ollama_bootstrap", self.main)

    def test_shared_engine_home_and_per_app_active_model(self):
        self.assertIn('SUITE_FOLDER: &str = "SmartSuite"', self.ollama)
        self.assertIn("import_legacy_models", self.main)
        self.assertIn("migrate.lock", self.ollama)
        self.assertIn("SmartRedes", self.ollama)
        self.assertIn("SmartCaja", self.ollama)
        self.assertIn('home.join("active_model.txt")', self.main)
        self.assertIn('home.join("active_profile.json")', self.main)
        self.assertIn("OLLAMA_MODELS=", self.main)
        self.assertIn("permanece en marcha", self.main)

    def test_sidecar_passes_ollama_model_env(self):
        self.assertIn('.env("OLLAMA_MODEL"', self.main)
        self.assertIn("active_model.txt", self.main)
        self.assertIn("profile_for_ram", self.main)
        self.assertIn("let ram_model = ram_profile.model.clone()", self.main)
        self.assertIn("ram_access_level", self.main)
        self.assertIn("blocked_ram_message", self.main)

    def test_product_models_are_llama31_and_moondream(self):
        import json
        cfg = json.loads(
            (REPO_ROOT / "desktop" / "smartsuite.config.json").read_text(encoding="utf-8")
        )
        extra = (cfg.get("ollama") or {}).get("extraModels") or []
        tiers = (cfg.get("ollama") or {}).get("tiers") or []
        extras_in_tiers = [m for t in tiers for m in (t.get("extraModels") or [])]
        self.assertTrue("moondream" in extra or "moondream" in extras_in_tiers)
        self.assertTrue(any(t.get("model") == "llama3.1:8b" for t in tiers))
        self.assertTrue(any(t.get("id") == "estandar" and t.get("model") == "llama3.2:3b" for t in tiers))
        self.assertIn("extra_models", self.main)
        self.assertIn("model_for_ram", self.main)
        self.assertIn("profile_for_ram", self.main)
        self.assertIn("active_profile.json", self.main)

    def test_linux_docker_build_script_exists(self):
        script = (REPO_ROOT / "desktop" / "scripts" / "linux-docker-build.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("AppImage", script)
        self.assertIn(".deb", script)
        self.assertIn(".rpm", script)
        self.assertIn("build-backend.sh", script)
        self.assertIn("ubuntu", script.lower())

    def test_splash_shows_ollama_progress(self):
        self.assertIn("/api/desktop-status", self.splash)
        self.assertIn("s.message", self.splash)
        self.assertIn("s.percent", self.splash)
        self.assertIn("ollamaDone", self.splash)
        self.assertNotIn("Entrar ahora", self.splash)
        self.assertNotIn("id=\"enter\"", self.splash)
        self.assertIn("Cargando la aplicación...", self.splash)
        self.assertIn("phase === 'blocked'", self.splash)
        self.assertNotIn("Use Entrar en el splash", self.main)
        self.assertIn("Cargando la aplicación...", self.main)

    def test_desktop_requirements_still_exclude_easyocr_torch(self):
        lines = [
            ln.strip().lower()
            for ln in (REPO_ROOT / "requirements-desktop.txt").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        joined = "\n".join(lines)
        self.assertNotIn("easyocr", joined)
        self.assertNotIn("torch", joined)
        self.assertIn("rapidocr", joined)
        cfg = json.loads(
            (REPO_ROOT / "desktop" / "smartsuite.config.json").read_text(encoding="utf-8")
        )
        excludes = (cfg.get("pyinstaller") or {}).get("excludes") or []
        self.assertNotIn("cv2", excludes)
        self.assertIn("torch", excludes)


class TestSplashSingleWhiteLogo(unittest.TestCase):

    def test_splash_single_white_ccce_logo(self):
        """Un solo logo CCCE; configure nunca emite splash-mark.png."""
        self.assertTrue((REPO_ROOT / "desktop" / "brand" / "logo-ccce-white.png").exists())
        canonical = (REPO_ROOT / "desktop" / "brand" / "splash.template.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("splash-mark.png", canonical)
        self.assertNotIn(".splash-mark", canonical)
        self.assertNotIn('class="splash-mark"', canonical)
        self.assertIn("splash-logo", canonical)
        self.assertIn("{{LOGO_HTML}}", canonical)
        cfg_js = (REPO_ROOT / "desktop" / "scripts" / "configure.mjs").read_text(encoding="utf-8")
        self.assertIn("logo-ccce-white.png", cfg_js)
        self.assertIn("rmSync", cfg_js)
        self.assertIn("brand/splash.template.html", cfg_js)
        self.assertIn('features = ["native-tls"]', cfg_js)
        self.assertIn('zip = "0.6"', cfg_js)
        cargo = (REPO_ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
        self.assertIn('features = ["native-tls"]', cargo)
        self.assertIn('zip = "0.6"', cargo)
        rust = (REPO_ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
        self.assertIn('.env("OLLAMA_MODEL"', rust)
        self.assertIn("active_model.txt", rust)
        extra = json.loads(
            (REPO_ROOT / "desktop" / "smartsuite.config.json").read_text(encoding="utf-8")
        )
        ollama = extra.get("ollama") or {}
        extras_in_tiers = [m for t in (ollama.get("tiers") or []) for m in (t.get("extraModels") or [])]
        self.assertTrue(
            "moondream" in (ollama.get("extraModels") or []) or "moondream" in extras_in_tiers
        )


class TestMacosDeveloperIdInstaller(unittest.TestCase):
    """El DMG de Mac se firma y notariza con el Developer ID de Augmented Experiences."""

    def setUp(self):
        self.workflow = (REPO_ROOT / ".github" / "workflows" / "desktop-build.yml").read_text(
            encoding="utf-8"
        )
        named = REPO_ROOT / ".github" / "workflows" / "macos-installer.yml"
        self.assertTrue(named.is_file(), "falta macos-installer.yml (mismo contrato que OsWaldo)")
        self.assertEqual(
            self.workflow.replace("\r\n", "\n"),
            named.read_text(encoding="utf-8").replace("\r\n", "\n"),
        )
        self.backend = (REPO_ROOT / "desktop" / "scripts" / "build-backend.sh").read_text(
            encoding="utf-8"
        )

    def test_workflow_uses_org_apple_secrets_and_oswaldo_runners(self):
        for name in (
            "APPLE_CERTIFICATE",
            "APPLE_CERTIFICATE_PASSWORD",
            "APPLE_SIGNING_IDENTITY",
            "APPLE_ID",
            "APPLE_PASSWORD",
            "APPLE_TEAM_ID",
        ):
            self.assertIn(f"secrets.{name}", self.workflow)
        self.assertNotIn("APPLE_DEVELOPER_ID", self.workflow)
        self.assertIn("macos-15", self.workflow)
        self.assertIn("macos-15-intel", self.workflow)
        self.assertIn("build-backend.sh --installer", self.workflow)

    def test_empty_certificate_unsets_apple_env(self):
        self.assertIn('if [ -z "${APPLE_CERTIFICATE}" ]', self.workflow)
        self.assertIn("unset APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID", self.workflow)

    def test_sidecar_gets_developer_id_not_adhoc_when_cert_present(self):
        self.assertIn("sign_bundled_sidecar", self.backend)
        self.assertIn("--options runtime", self.backend)
        self.assertIn("--timestamp", self.backend)
        self.assertIn('APPLE_SIGNING_IDENTITY', self.backend)
        self.assertIn(
            'if [[ "$(uname -s)" == "Darwin" && -z "${APPLE_CERTIFICATE:-}" ]]',
            self.backend,
        )


if __name__ == '__main__':
    unittest.main()
