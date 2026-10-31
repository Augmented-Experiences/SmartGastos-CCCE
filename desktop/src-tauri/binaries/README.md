# Sidecar Tauri (`externalBin`)

Aquí debe existir **`backend-<target-triple>.exe`** (Windows) o sin `.exe` en Linux/macOS.

Ejemplo Windows:

`backend-x86_64-pc-windows-msvc.exe`

No se versiona en git (se genera con PyInstaller). Desde la raíz del repo:

```powershell
powershell -ExecutionPolicy Bypass -File desktop\scripts\build-backend.ps1
```

`npm run build` ejecuta `configure.mjs` y **falla con un mensaje claro** si falta este archivo.
