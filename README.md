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

Salida esperada:
- App: `dist/AI QA Desktop Runner.app`
- DMG: `dist/AI-QA-Desktop-Runner-arm64.dmg`

Notas:
- El script instala `pyinstaller` y embebe Chromium de Playwright para la app.
- Para distribuir fuera de tu máquina puede requerirse firmado/notarización de Apple.
