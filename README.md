# Desktop Runner

## Features
- Login JWT
- Lista de proyectos
- Lista de pruebas
- Ejecucion Playwright
- Screenshots y videos
- Logs en tiempo real
- Timeline de ejecucion
- Retry engine
- Auto-healing IA

## Ejecutar
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash run.sh
```

## Generar DMG para macOS (Apple Silicon / ARM64)
```bash
cd desktop-runner
chmod +x build-macos-arm64-dmg.sh
./build-macos-arm64-dmg.sh
```

## Generar DMG para macOS Intel (x64)
```bash
cd desktop-runner
chmod +x build-macos-x64-dmg.sh
./build-macos-x64-dmg.sh
```

## Cambiar icono de la aplicacion (macOS)
1. Guarda tu imagen (png/jpg) en `desktop-runner/assets/`.
2. Genera el `.icns`:

```bash
cd desktop-runner
chmod +x make-macos-icon.sh
./make-macos-icon.sh assets/app-icon.png
```

El archivo resultante debe quedar en `assets/app-icon.icns`. Los scripts de build lo detectan automaticamente.

Salida esperada:
- App: `dist/AI QA Desktop Runner.app`
- DMG: `dist/AI-QA-Desktop-Runner-arm64.dmg`
- DMG (Intel): `dist/AI-QA-Desktop-Runner-x64.dmg`

Notas:
- El script instala `pyinstaller` y prepara Chromium en cache de usuario de Playwright (no embebido en el bundle).
- Para distribuir fuera de tu máquina puede requerirse firmado/notarización de Apple.
