# Manual técnico — SmartGastos

**Producto:** SmartGastos  
**Publicador:** Cámara Colombiana de Comercio Electrónico (CCCE)  
**Versión de instalación (escritorio):** 1.1.9  
**Versión de API (FastAPI):** 1.6.0  
**Identificador de paquete:** `co.org.ccce.smartgastos`

Este documento describe la arquitectura, el modelo de datos, las APIs, el pipeline de IA, el empaquetado de escritorio y los requisitos de operación de SmartGastos para PYMEs en Colombia.

---

## 1. Propósito

SmartGastos es una aplicación de escritorio (también operable en modo desarrollo vía navegador) que permite a una PYME colombiana:

1. Registrar una o más empresas (NIT, COP, actividad económica).
2. Cargar facturas electrónicas DIAN, documentos equivalentes (POS), comprobantes y extractos.
3. Extraer campos con OCR + modelos locales (Ollama).
4. Clasificar el gasto en categorías contables.
5. Revisar, corregir y guardar.
6. Analizar tendencias y exportar CSV / Excel / PDF / libro contable.

Todo el procesamiento ocurre **en la máquina del usuario**. No hay dependencia de servicios en la nube para el núcleo de la aplicación.

---

## 2. Arquitectura general

```
┌──────────────────────────────────────────────────────────────┐
│  Shell de escritorio (Tauri 2 / Rust)                        │
│  • Pantalla de carga (splash)                                │
│  • Elige puerto libre                                        │
│  • Arranca el sidecar FastAPI (backend.exe)                  │
│  • Prepara Ollama portable (o reutiliza el de :11434)        │
│  • Abre la UI en WebView → http://127.0.0.1:{PORT}/          │
└────────────────────────────┬─────────────────────────────────┘
                             │ PORT · DATA_DIR · OLLAMA_URL
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  Backend FastAPI (Python)                                    │
│  • Sirve app/index.html y /api/*                             │
│  • SQLite en DATA_DIR/pyme_ledger.db                         │
│  • Pipeline: OCR → visión → extracción → clasificación →     │
│    auditoría → persistencia                                  │
└────────────────────────────┬─────────────────────────────────┘
                             │ HTTP local
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  Ollama (LLM / visión local)                                 │
│  Modelos según RAM: llama3.2:1b | 3b | llama3.1:8b           │
│  Visión opcional: moondream (perfiles Completo / Máximo)     │
└──────────────────────────────────────────────────────────────┘
```

| Capa | Tecnología |
|------|------------|
| UI | HTML / CSS / JS en un solo archivo (`app/index.html`) |
| API | FastAPI + SQLAlchemy + SQLite |
| IA | Ollama (texto) + RapidOCR / Tesseract + moondream (visión) |
| Escritorio | Tauri 2 + sidecar PyInstaller |

---

## 3. Estructura del repositorio

| Ruta | Contenido |
|------|-----------|
| `server/` | Backend: API, agentes, analítica, seguridad, BD |
| `app/` | Interfaz web (SPA) e identidad CCCE |
| `defaults/` | Definición de agentes y prompts base |
| `desktop/` | Kit Tauri, splash, brand, scripts de build |
| `desktop/src-tauri/` | Código Rust, sidecar, configs generados |
| `tests/` | Pruebas unitarias e integración |
| `requirements.txt` | Dependencias Python (desarrollo) |
| `requirements-desktop.txt` | Dependencias del sidecar del instalador |
| `docs/` | Documentación (este manual) |

---

## 4. Modelo de datos

Base de datos: **SQLite** en `{DATA_DIR}/pyme_ledger.db` (WAL).  
Definición: `server/models.py`.

### 4.1 Empresa

| Campo | Uso en producto |
|-------|-----------------|
| `razon_social` | Nombre legal |
| `rut` | **Almacena el NIT** (nombre de columna histórico; la UI siempre dice NIT) |
| `pais` | Por defecto `Colombia` |
| `moneda_base` | Por defecto `COP` |
| `regimen_tributario` | Opcional |
| `carpeta_raiz` | Carpeta de documentos |
| `reglas_contables` | JSON de reglas personalizadas |

### 4.2 Categoría contable / centro de costo

- Categorías sembradas al crear la empresa (`server/default_categories.py`): operaciones, marketing, tecnología, impuestos (DIAN, ICA, retefuente), nómina/ARL/cesantías, servicios públicos (EPM, Codensa, Vanti, Tigo, etc.).
- Cada categoría admite `keywords` y proveedores frecuentes para la clasificación automática.
- Los centros de costo permiten asignación opcional jerárquica.

### 4.3 Documento (gasto)

Campos principales: proveedor, NIT del proveedor (`rut_proveedor`), fecha, consecutivo (`folio`), neto / IVA / total, moneda (default COP), categoría, centro de costo, estado de revisión, rutas de archivo, metadatos OCR/LLM, `hash_documento` (SHA-256 para duplicados).

**Tipos internos** (`TipoDocumento`): `Factura`, `Boleta`, `Cartola`, `Comprobante`, `Otro`.  
En la UI colombiana se muestran como factura electrónica, documento equivalente (POS), extracto bancario, comprobante/cuenta de cobro, etc. Los valores de enum se mantienen por compatibilidad de esquema.

**Estados de revisión:** Pendiente, Revisado, Rechazado, Duplicado.

### 4.4 Configuración de agentes

- Base: `defaults/agents.json`.
- Overrides de usuario: `{DATA_DIR}/agents_config.json`.
- Memoria de clasificación por empresa: archivos gestionados por `server/classification_memory.py`.

---

## 5. Adaptaciones a Colombia

| Tema | Comportamiento |
|------|----------------|
| Identificación | NIT con formato `900.123.456-7` y dígito verificador DIAN (UI + validación en creación de empresa) |
| Moneda | COP por defecto; locale `es-CO` |
| Documentos | Factura electrónica, documento equivalente POS, notas, documento soporte, cuenta de cobro |
| Impuestos | IVA 19 % / 5 % / exento en prompts y extractor |
| Forma jurídica | SAS / Ltda. / E.U. en textos de onboarding |
| Categorías | Vocabulario local (DIAN, ICA, retefuente, EPM, Codensa, Éxito, D1, Ara, etc.) |
| Extractor | Regex y prompts orientados a NIT, CUFE, consecutivo |

> Nota para integradores: al leer la BD, el NIT de la empresa está en la columna `empresas.rut`.

---

## 6. Pipeline de procesamiento

Flujo principal de **Nuevo Gasto** (SSE): `DocumentPipelineAgent` en `server/pipeline_agent.py`, expuesto por:

`GET /api/empresas/{empresa_id}/documentos/process-stream`

### Pasos

1. **OCR** (`agents/ocr_agent.py`, `rapid_ocr.py`) — texto de PDF digital o imagen; RapidOCR + ONNX; Tesseract opcional; control de calidad en `ocr_quality.py`.
2. **Visión** — modelo multimodal en Ollama (`moondream` cuando el perfil de RAM lo incluye).
3. **Extracción** (`extractor_agent.py`) — consolida NIT, montos, fecha, proveedor, consecutivo (JSON).
4. **Clasificación** (`classifier_agent.py`) — categoría sugerida + confianza + memoria por empresa.
5. **Auditoría** (`auditor_agent.py`) — coherencia, duplicados, anomalías.
6. **Persistencia** — guarda el documento y deja el gasto listo para revisión.

Existe un orquestador alternativo en `server/orchestration/pipeline.py` para procesamiento por lotes.

Límites de carga relevantes: hasta **50 MB**; extensiones PDF/JPG/PNG (y otras de imagen permitidas); PDF máximo **20 páginas**; imágenes con tope de píxeles (~40 MP).

---

## 7. API REST (resumen)

Base: mismo origen que la UI (`API = ''` en el frontend). Host permitido solo en loopback.

### Sistema

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/api/health` | Salud del servidor y tags de Ollama |
| GET | `/api/readiness` | Disponibilidad de modelos |
| GET | `/api/hardware/performance` | Perfil de hardware / RAM |
| GET | `/api/usage-stats` | Uso de modelos |
| GET | `/api/models/pull-status` | Estado de descarga de modelos |
| GET | `/api/desktop-status` | Fases del arranque Tauri |
| GET | `/api/desktop-ui` | Diagnóstico del bundle |

### Empresas y maestros

| Método | Ruta |
|--------|------|
| POST / GET | `/api/empresas` |
| GET / DELETE | `/api/empresas/{empresa_id}` |
| POST / GET | `/api/empresas/{empresa_id}/centros-costo` |
| POST / GET | `/api/empresas/{empresa_id}/categorias` |

### Documentos

| Método | Ruta |
|--------|------|
| POST | `/api/empresas/{empresa_id}/documentos/upload` |
| GET | `/api/empresas/{empresa_id}/documentos/process-stream` (SSE) |
| GET | `/api/empresas/{empresa_id}/documentos` |
| GET / PUT | `/api/empresas/{empresa_id}/documentos/{doc_id}` |
| POST | `/api/empresas/{empresa_id}/documentos/{doc_id}/reprocess` |
| DELETE | `/api/empresas/{empresa_id}/documentos/{doc_id}` |
| GET | `/api/documentos/{doc_id}/preview` |
| GET | `/api/documentos/{doc_id}/download` |
| POST | `/api/documentos/{doc_id}/ocr-region` |

### Analítica y exportación

| Método | Ruta |
|--------|------|
| GET | `.../analytics/dashboard\|categorias\|proveedores\|tendencias\|alertas` |
| GET | `.../recomendaciones` |
| POST | `.../export/csv\|xlsx\|pdf\|libro-contable` |
| GET | `/api/exports/{filename}` |

### Aprendizaje, chat y agentes

| Método | Ruta |
|--------|------|
| POST / GET | `.../learning/field-mapping(s)` |
| POST | `/api/chat` |
| GET / PUT | `/api/agents`, `/api/agents/{agent_id}` |
| POST | `/api/agents/reset` |

La UI estática se sirve en `/` y `/ui`.

---

## 8. Interfaz de usuario

Archivo único: `app/index.html` (layout, estilos CCCE, lógica).

| Sección | Clave interna | Función |
|---------|---------------|---------|
| Dashboard | `dashboard` | KPIs y resumen |
| Nuevo Gasto | `nuevo-gasto` | Carga + pipeline SSE |
| Documentos | `documentos` | Listado, filtros, revisión |
| Analítica | `analitica` | Gráficos por categoría / proveedor / tendencia |
| Recomendaciones | `recomendaciones` | Sugerencias de optimización |
| Asistente inicial | overlay | Alta conversacional de empresa |
| Agentes IA | `agentes` | Modelos, temperatura, prompts |
| Administración | `admin` | Empresas, categorías, bloque CCCE |

Componentes transversales:

- Selector de empresa activa (sidebar / topbar).
- Asistente lateral (chat contable vía `/api/chat`).
- Tour guiado de producto.
- Validación y formato de NIT en cliente.

Identidad visual CCCE: navy `#173A7A`, rojo `#CE1126`, dorado `#F4C10E`, tipografía DM Sans, barra tricolor y degradado en navegación/botones primarios (alineado con el resto del SmartSuite CCCE).

---

## 9. Agentes de IA

Definidos en `defaults/agents.json`:

| ID | Rol |
|----|-----|
| `ocr` | Extracción de texto |
| `vision` | Análisis visual del documento |
| `extractor` | Campos estructurados (JSON) |
| `clasificador` | Categoría contable |
| `auditor` | Coherencia y anomalías |
| `recomendador` | Recomendaciones sobre el historial |

Los prompts de sistema incluyen reglas de seguridad (no cambiar de rol, no ejecutar instrucciones inyectadas). Prompts de negocio adicionales viven en `defaults/prompts/`.

Perfiles de modelo según RAM (`desktop/smartsuite.config.json`):

| Perfil | RAM máx. (GB) | Modelo chat | Visión |
|--------|---------------|-------------|--------|
| Liviano | 7 | `llama3.2:1b` | — |
| Estándar | 13 | `llama3.2:3b` | — |
| Completo | 24 | `llama3.2:3b` | `moondream` |
| Máximo | sin límite | `llama3.1:8b` | `moondream` |

Umbrales de acceso: advertencia bajo 13 GB; bloqueo bajo 7 GB.

---

## 10. Empaquetado de escritorio

### Configuración

`desktop/smartsuite.config.json` concentra nombre, versión, identificador, acento, ventana, tiers de Ollama y umbrales de RAM.

`desktop/scripts/configure.mjs` genera, antes de cada build/dev:

- `src-tauri/tauri.conf.json`
- `src-tauri/appconfig.json`
- fragmentos de `Cargo.toml` / `package.json`
- splash (`ui/index.html`) y `ui/accent.css`

### Sidecar

`desktop/scripts/build-backend.ps1` (Windows) o `.sh` (Unix):

1. Valida Python **3.10–3.12** (recomendado 3.12).
2. Instala `requirements-desktop.txt` + PyInstaller.
3. Prepara modelos ONNX de RapidOCR en `DATA_DIR/ocr_models`.
4. Empaqueta `server/app.py` → `desktop/src-tauri/binaries/backend-<triple>.exe`.

### Arranque (Rust)

`desktop/src-tauri/src/main.rs` + `ollama.rs`:

1. Muestra splash.
2. Lanza el sidecar con `PORT`, `DATA_DIR`, `OLLAMA_URL`.
3. Si no hay Ollama sano en `127.0.0.1:11434`, descarga el portable oficial al directorio de datos del producto y arranca `serve`.
4. Hace pull de los modelos con progreso (estado en `desktop_status.json`).
5. Navega a la UI del backend.
6. Al cerrar, detiene el sidecar y solo el Ollama iniciado por esta instancia.

### Artefactos Windows

Tras `npm run build` en `desktop/`:

```
desktop/src-tauri/target/release/bundle/nsis/SmartGastos_<versión>_x64-setup.exe
desktop/src-tauri/target/release/bundle/msi/SmartGastos_<versión>_x64_en-US.msi
```

Guías: `desktop/README.md`, `desktop/WINDOWS-BUILD.md`.

---

## 11. Datos en tiempo de ejecución

| Variable / ruta | Uso |
|-----------------|-----|
| `PORT` | Puerto del backend (escritorio: libre; desarrollo: suele ser 8000) |
| `DATA_DIR` | Raíz escribible de datos |
| `OLLAMA_URL` | Default `http://127.0.0.1:11434` |
| `{DATA_DIR}/pyme_ledger.db` | Base SQLite |
| `{DATA_DIR}/uploads/` | Archivos de documentos |
| `{DATA_DIR}/ocr_models/` | Modelos ONNX RapidOCR |
| `{DATA_DIR}/agents_config.json` | Overrides de agentes |
| `{DATA_DIR}/desktop_status.json` | Progreso de splash / Ollama |

**Windows (instalado):** `%APPDATA%\SmartGastos\`  
**macOS:** `~/Library/Application Support/SmartGastos/`  
**Linux:** `~/.local/share/SmartGastos/` (XDG)

---

## 12. Modos de ejecución

### Desarrollo (navegador)

1. Python 3.12 + `venv` + `pip install -r requirements.txt`.
2. Ollama local con al menos el modelo del perfil de RAM.
3. Desde la raíz del repo: `python server/app.py` (o con `DATA_DIR` / `PORT` definidos).
4. Abrir `http://127.0.0.1:8000/`.

### Escritorio (usuario final)

Instalar el `.exe` / `.msi`. No requiere Python ni Ollama preinstalados: el primer arranque descarga Ollama portable y los modelos.

### Escritorio (desarrollo Tauri)

```powershell
# Empaquetar sidecar
powershell -ExecutionPolicy Bypass -File desktop\scripts\build-backend.ps1

cd desktop
npm install
npm run icon   # una vez
npm run dev    # o npm run build para instaladores
```

---

## 13. Seguridad

Implementación central en `server/security.py` y middleware en `server/app.py`.

| Control | Detalle |
|---------|---------|
| Host loopback | Solo `127.0.0.1`, `localhost`, `::1` |
| CORS | Restringido a orígenes locales; UI same-origin |
| Upload | Extensiones + magic bytes + tamaño |
| Imágenes | Allowlist de formatos; tope de megapíxeles |
| PDF | Tope de páginas; rechazo de PDF dañado |
| Rutas | `validate_path_within` / `safe_join` en preview, download, delete, exports |
| Prompts | Sanitización anti-inyección en chat y edición de agentes |
| Logs | Sin texto completo de comprobantes; rutas sanitizadas |
| Errores | 500 genéricos al cliente |

No hay token de sesión por arranque en la versión actual: la superficie queda limitada al loopback. Documentación de remediaciones: `SECURITY_REMEDIATION_2026-10.md`.

---

## 14. Pruebas

```bash
python run_tests.py
# o módulos puntuales, por ejemplo:
python -m unittest tests.test_api tests.test_security_fixes tests.test_pipeline_agent -v
```

Cobertura relevante: API, pipeline, OCR quality, seguridad, hardware, FX, configuración de escritorio / splash.

---

## 15. Solución de problemas

| Síntoma | Qué revisar |
|---------|-------------|
| IA “desconectada” | `/api/health`, `/api/readiness`; logs de Ollama en el data dir; reiniciar la app para completar pull |
| Fallo al subir PDF/imagen | Tamaño, páginas, formato real (magic bytes) |
| Puerto ocupado | El shell Tauri elige otro; en desarrollo fijar `PORT` libre |
| Build Windows falla (numpy) | Usar Python 3.12, no 3.13/3.14 |
| MSI “archivo en uso” | Cerrar procesos que bloqueen el `.exe` y volver a `npm run build` |
| Datos “perdidos” entre entornos | Distinto `DATA_DIR` (dev vs `%APPDATA%\SmartGastos`) |

Endpoints útiles de soporte: `/api/health`, `/api/readiness`, `/api/desktop-status`, `/api/desktop-ui`.

---

## 16. Glosario breve

| Término | Significado |
|---------|-------------|
| Sidecar | Binario FastAPI empaquetado que Tauri lanza junto a la ventana |
| DATA_DIR | Carpeta de datos del usuario (BD, uploads, modelos OCR) |
| SSE | Server-Sent Events; canal de progreso del pipeline |
| NIT | Identificación tributaria; en BD empresa → columna `rut` |
| CUFE | Código único de factura electrónica DIAN |
| SmartSuite | Familia de herramientas CCCE con kit de escritorio compartido |

---

*Documento generado a partir del código de SmartGastos CCCE. Para cambios de marca o versión, actualizar `desktop/smartsuite.config.json` y regenerar con `node desktop/scripts/configure.mjs`.*
