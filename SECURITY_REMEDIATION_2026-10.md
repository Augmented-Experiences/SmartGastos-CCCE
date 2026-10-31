# Remediación de seguridad — 27 oct 2026

Escaneo de origen: Checkmarx ASA Premium, 14 oct 2026, scan `d4cb7fca-b73b-4206-82f7-90bc3feb853c`, zip de `SmartGastos-CCS-master`. 68 hallazgos, todos New / To Verify, 0 críticos: 34 SAST y 34 SCA. El PDF no trae archivo, línea ni data flow por hallazgo.

Este trabajo cierra la deuda que se puede verificar en el código de `kit`. No marca hallazgos de Checkmarx como resueltos: hace falta un re-scan.

El entorno de build quedó actualizado. **No se generó un instalador nuevo en esta pasada.** El instalador 1.1.9 que ya está generado sigue con Starlette 0.52.1 hasta que se vuelva a compilar.

## 1. Versiones

PyPI consultado el 27 oct 2026. Se instalaron en `venv` con `pip install -r requirements-desktop.txt`. `PyPDF2` se desinstaló de ese venv para que no quede importable al lado de `pypdf`.

| Librería | Antes | Después | Por qué esta versión |
|---|---|---|---|
| Pillow | 10.4.0 | **13.0.0** | Última estable. Supera el mínimo 12.1.1 (CVE-2026-25990) y también 12.2.0, que corrige el bypass por overflow del mismo parser PSD. `Image.LANCZOS` y `Image.open(..., formats=)` siguen existiendo. |
| python-multipart | 0.0.9 | **0.0.32** | Última estable. |
| starlette | 0.38.6 | **1.10.0** | Cierra CVE-2026-48710, CVE-2026-48817, CVE-2026-48818 y CVE-2026-54282 del reporte del 29 oct. FastAPI 0.162.0 lo aceptó y los tests de API pasaron. |
| fastapi | 0.115.0 | **0.162.0** | Última estable. Trae Starlette en el mismo install. Pydantic se quedó en 2.9.2 porque 0.162.0 acepta `pydantic>=2.9`. |
| requests | 2.32.3 | **2.34.2** | Última estable. |
| python-dotenv | 1.0.1 | **1.2.4** | Última estable. `load_dotenv` no se usa en el servidor; el pin queda alineado igual. |
| PyPDF2 | 3.0.1 | **eliminado** | Proyecto discontinuado. |
| pypdf | (no estaba; el código ya intentaba `import pypdf` en un punto) | **6.21.3** | Sucesor. `PdfReader.pages` y `extract_text()` cubren los dos call sites. Migración hecha, no solo propuesta. |

FastAPI 0.162.0 arrastra `opentelemetry-api==1.45.1`, `annotated-doc` y `typing-inspection`. No se pidieron; entran como dependencia directa del paquete.

## 2. Archivos

| Archivo | Para qué |
|---|---|
| `requirements.txt` | Mismos pines que el instalador. EasyOCR y el launcher Pinokio no están en el repo. |
| `requirements-desktop.txt` | Pines del sidecar que arma el instalador. |
| `server/security.py` | Rutas, magic bytes, `Image.open` con allowlist, tope de píxeles, límite de páginas PDF, sanitizado de logs y chequeo de Host. |
| `server/app.py` | Upload rechaza contenido que no calza. Host solo loopback. CORS sin `*`. Borrado y descarga pasan por el helper de rutas. |
| `server/pipeline_agent.py` | Imágenes y PDF digital usan los helpers. Logs de nombre de archivo y proveedor sin CR/LF. |
| `server/agents/ocr_agent.py` | Deja PyPDF2. Abre imágenes solo con la allowlist. No imprime el texto del documento en el modo CLI. |
| `server/agents/rapid_ocr.py` | La foto pasa por `open_receipt_image` antes de RapidOCR. |
| `server/classification_memory.py` | El log de proveedor y categoría pasa por `sanitize_log`. |
| `desktop/backend/backend.spec` | `verify_best_practices` queda en `excludes`. El entry point del sidecar sigue siendo `server/app.py`. |
| `tests/test_security_fixes.py` | Pruebas nuevas de ruta, Host, upload, PDF, bomba de imagen y alcance del spec. |
| `tests/test_cross_platform.py` | Sigue prohibiendo escuchar en el hostname `localhost`. Permite esa palabra solo en el regex de CORS loopback. |

`server/analytics/` no se tocó: no hay `logger` ni `print` de input de usuario.

## 3. Qué quedó cerrado en código

**Imágenes.** El único `Image.open` está en `open_receipt_image`. `formats` permite JPEG, PNG, WEBP, BMP y TIFF. BMP y TIFF están porque `ALLOWED_EXTENSIONS` ya los aceptaba. GIF no entra: el upload no lo acepta. `Image.MAX_IMAGE_PIXELS` queda en 40_000_000 (~40 megapíxeles: foto de celular y un A4 a 600 dpi). `DecompressionBombError` se traduce a «La imagen es demasiado grande para un comprobante (máximo 40 megapíxeles).»

**Upload.** Se leen los magic bytes. Un PSD (`8BPS`) renombrado a `.jpg` se rechaza antes del parser. Ya no se acepta un archivo solo porque `mimetypes` adivina por la extensión. Si `python-magic` está instalado y contradice el contenido, también se rechaza. El venv de build no tiene `python-magic`; la decisión la toman los magic bytes.

**PDF.** Máximo 20 páginas, chequeado antes de extraer texto. Un PDF dañado responde 400 con «El PDF está dañado o no se puede leer.» No se propaga el traceback al cliente.

**Rutas.** `validate_path_within` resuelve la ruta (`..`, absoluta, `%2e%2e` hasta dos veces y symlink, porque `Path.resolve` sigue el enlace). `safe_join` se usa en el stream de proceso y en la descarga de exports. Preview, descarga, reproceso, borrado de documento y borrado de empresa también lo comprueban antes de abrir o borrar.

**Host y CORS.** El header `Host` tiene que ser `localhost`, `127.0.0.1` o `::1` (con puerto). Un `Host: evil.example` recibe 403. `testserver` se acepta solo si el proceso no está congelado por PyInstaller, porque el TestClient de Starlette lo usa y un navegador no puede fijar ese Host. En el `.exe` no se acepta. Se quitó `Access-Control-Allow-Origin: *` del stream y del preview. El regex de CORS ahora incluye `localhost` y `[::1]` además de `127.0.0.1`. La UI usa `API = ''` (mismo origen), así que no depende de CORS.

**Token de sesión.** No hay uno. No se agregó: EventSource y el resto de `fetch` del frontend tendrían que enviarlo, y dejarlo a medias no cierra el agujero. Propuesta: un token aleatorio por arranque, en memoria, exigido en cada `/api` salvo `/api/health`, inyectado en el HTML que sirve el propio sidecar.

**Logs.** `sanitize_log` quita CR, LF y otros controles. `log_safe_path` deja la ruta completa solo si cae dentro de `DATA_DIR`; si no, solo el nombre. No se escribe el texto completo del comprobante en logs. La vista previa que la UI ya muestra en el pipeline no se tocó.

**`verify_best_practices.py`.** No iba en el instalador. Se eliminó del árbol para que el zip del repo no lo vuelva a escanear. `backend.spec` lo sigue listando en `excludes`.

## 4. Tests ejecutados

Entorno: `venv\Scripts\python.exe`, Python 3.12, directorio del repo. `DATA_DIR` de los tests es un temporal. No se usó el SQLite de `%APPDATA%\SmartGastos`.

```
venv\Scripts\python.exe -m unittest discover -s tests -q
```

Resultado: **316 tests, 35.1 s. 2 fallos, 1 skip.** Los dos fallos no vienen de este cambio:

- `test_expense_form.TestExpenseFormUI.test_moneda_options` busca `value="EUR"` en `app/index.html`. El HTML tiene `EUR|Euro` y no se modificó en esta pasada.
- `test_hardware.TestDetectHardware.test_ram_is_positive` recibe `ram_gb = 0`. `detect_hardware()` imprime `Error detectando RAM: [WinError 2] The system cannot find the file specified`. `server/hardware.py` no se tocó.

Skip: `test_symlink_pointing_outside_is_rejected`. Windows devolvió `WinError 1314` (el proceso no tiene privilegio para crear un symlink). El helper sí resuelve con `Path.resolve`, que sigue symlinks cuando el SO deja crearlos. Aquí no se pudo ejecutar ese caso.

Los casos nuevos que sí corrieron, en `tests.test_security_fixes`:

- `..` y ruta absoluta fuera de la base: rechazados.
- `..%252f..%252fwindows%252fwin.ini`: el nombre queda dentro de la base (`win.ini`), no escapa.
- Host `evil.example` contra `/api/health`: 403. `127.0.0.1`, `localhost` y `[::1]` aceptados por el helper.
- PNG de 32×24 generado y PDF de una página: upload 200, Pillow ve PNG, `read_pdf_text` devuelve texto.
- PSD renombrado a `.jpg`: 400.
- PDF corrupto (`%PDF-1.4` + basura) y PDF de 21 páginas: 400.
- Bomba de descompresión (tope forzado a 0 px sobre un PNG de 8×8): mensaje con «megapíxeles».
- `sanitize_log` quita CR/LF.
- `backend.spec` excluye `verify_best_practices`.

Subconjunto previo, también en verde salvo el skip del symlink: `tests.test_security_fixes`, `tests.test_security`, `tests.test_pinokio_config`, `tests.test_api`, `tests.test_analytics`, `tests.test_fx`, `tests.test_cross_platform.TestServerApp`. 177 tests, 1 skip.

Carga aparte del suite, con `DATA_DIR` en `C:\Users\Oscar\AppData\Local\Temp\sg-remediation-m3nj3mnu` (no es la base del usuario):

- `app/logo-ccs.png` (2 987 bytes) por el endpoint de upload: **200**.
- PDF de una página generado con pypdf: **200**.
- Cabecera PSD como `boleta.jpg`: **400**.

No se corrió el pipeline de OCR con Ollama. En la misma sesión `127.0.0.1:11434` rechazó la conexión (`WinError 10061`). La compuerta de upload y el parseo de PNG/PDF sí se ejecutaron.

## 5. pip freeze del venv de build

Este es el entorno donde `desktop\scripts\build-backend.ps1` instala y corre PyInstaller, después de `pip install -r requirements-desktop.txt` y de desinstalar PyPDF2. Incluye PyInstaller y lo que ya estaba en el venv. No es un venv vacío creado solo con el requirements.

```
altgraph==0.17.5
annotated-doc==0.0.5
annotated-types==0.8.0
antlr4-python3-runtime==4.9.3
anyio==4.15.1
certifi==2026.7.22
chardet==7.6.0
charset-normalizer==3.5.1
click==8.5.0
colorama==0.4.6
coloredlogs==15.0.1
colorlog==6.12.0
et_xmlfile==2.0.0
fastapi==0.162.0
flatbuffers==25.12.19
greenlet==3.5.6
h11==0.16.0
httpcore==1.0.9
httpx==0.27.2
humanfriendly==10.0
idna==3.19
mpmath==1.3.0
numpy==1.26.4
omegaconf==2.3.1
onnxruntime==1.19.2
opencv-python==4.11.0.86
openpyxl==3.1.5
opentelemetry-api==1.45.1
packaging==26.3
pandas==2.2.3
pdf2image==1.17.0
pefile==2024.8.26
pillow==13.0.0
protobuf==7.36.1
pyclipper==1.4.0
pydantic==2.9.2
pydantic-settings==2.5.2
pydantic_core==2.23.4
pyinstaller==6.22.3
pyinstaller-hooks-contrib==2026.7
pypdf==6.21.3
pyreadline3==3.5.6
pytesseract==0.3.13
python-dateutil==2.9.0
python-dotenv==1.2.4
python-multipart==0.0.32
pytz==2026.3.post1
pywin32-ctypes==0.2.3
PyYAML==6.0.3
rapidocr==3.3.1
reportlab==4.2.5
requests==2.34.2
setuptools==84.0.0
shapely==2.1.2
six==1.17.0
sniffio==1.3.1
SQLAlchemy==2.0.35
starlette==1.10.0
sympy==1.14.0
tqdm==4.70.1
typing-inspection==0.4.4
typing_extensions==4.16.0
tzdata==2026.4
urllib3==2.8.0
uvicorn==0.30.6
```

## 6. Pendiente de export detallado de Checkmarx

Sin archivo, línea y data flow no se cierra hallazgo SAST genérico. Quedan en este estado hasta el CSV o el acceso al scan:

- Los 34 SAST que el PDF solo resume por conteo (Path Traversal 6, y el resto repartido en `app.py` 18, `classification_memory.py` 4, `pipeline_agent.py` 4, `verify_best_practices.py` 2, `ocr_agent.py` 2, `extractor_agent.py` 2, `analyzer.py` 1, `recommender.py` 1). El código de `kit` ya tenía controles de path traversal; esta pasada los unificó y les agregó tests. Igual hace falta la traza de Checkmarx para decir «falso positivo» o «sigue abierto» en cada uno.
- Log Forging aparece como el hallazgo más antiguo del resumen, sin ubicación. El sanitizado de logs está puesto en los módulos que el PDF nombra y que sí escriben input. Sin la traza no se puede afirmar que Checkmarx apunte exactamente a esas líneas.
- Los 34 SCA deberían bajar en un re-scan sobre `kit` con estos pines. No se contrastó cada CVE del PDF, uno por uno, contra el changelog: el PDF lista Pillow por CVE y agrupa el resto por paquete.
- `PINOKIO_ORIGIN` ya no suma un origen CORS. El instalador no usa ese launcher.
- El test de symlink no corrió en esta máquina (WinError 1314).
- No hay instalador  nuevo con estas librerías. Hay que recompilar el sidecar para que el `.exe` / `.dmg` las lleven.
- Token de sesión local: propuesto arriba, no implementado.
