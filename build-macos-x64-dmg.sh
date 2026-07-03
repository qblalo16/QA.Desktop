#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="python3"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

run_python_x64() {
  if ! command -v arch >/dev/null 2>&1; then
    "$PYTHON_BIN" "$@"
    return
  fi

  # Fuerza ejecución x86_64 para generar artefacto Intel.
  if arch -x86_64 "$PYTHON_BIN" -c "import platform; print(platform.machine())" >/dev/null 2>&1; then
    arch -x86_64 "$PYTHON_BIN" "$@"
    return
  fi

  echo "No se pudo ejecutar Python en x86_64 en este host." >&2
  echo "Para generar DMG x64 necesitas un host Intel o Apple Silicon con Rosetta habilitado." >&2
  exit 1
}

APP_NAME="AI QA Desktop Runner"
DMG_NAME="AI-QA-Desktop-Runner-x64.dmg"
DIST_DIR="$ROOT_DIR/dist"
STAGING_DIR="$DIST_DIR/dmg-staging"
ICON_PATH="$ROOT_DIR/assets/app-icon.icns"

echo "[1/5] Instalando dependencias de empaquetado..."
run_python_x64 -m pip install --upgrade pip
run_python_x64 -m pip install -r requirements.txt pyinstaller

echo "[2/5] Instalando Chromium para Playwright (cache de usuario, no embebido en el bundle)..."
unset PLAYWRIGHT_BROWSERS_PATH
run_python_x64 -m playwright install chromium

# Limpia navegadores previamente embebidos en site-packages por builds antiguos (PLAYWRIGHT_BROWSERS_PATH=0).
PLAYWRIGHT_PACKAGE_DIR="$(run_python_x64 -c "import pathlib, playwright; print(pathlib.Path(playwright.__file__).resolve().parent)")"
rm -rf "$PLAYWRIGHT_PACKAGE_DIR/driver/package/.local-browsers"

echo "[3/5] Limpiando artefactos anteriores..."
rm -rf "$ROOT_DIR/build" "$DIST_DIR"
rm -rf "$HOME/Library/Application Support/pyinstaller"

echo "[4/5] Generando .app con PyInstaller..."
PYI_ARGS=(
  --noconfirm
  --clean
  --windowed
  --target-architecture x86_64
  --name "$APP_NAME"
  --paths "$ROOT_DIR/src"
  --collect-all PySide6
  --collect-submodules playwright
)

if [[ -f "$ICON_PATH" ]]; then
  PYI_ARGS+=(--icon "$ICON_PATH")
fi

run_python_x64 -m PyInstaller \
  "${PYI_ARGS[@]}" \
  "$ROOT_DIR/src/main.py"

APP_PATH="$DIST_DIR/$APP_NAME.app"
if [[ ! -d "$APP_PATH" ]]; then
  echo "No se encontro la app generada en: $APP_PATH" >&2
  exit 1
fi

echo "[5/5] Generando DMG..."
mkdir -p "$STAGING_DIR"
cp -R "$APP_PATH" "$STAGING_DIR/"
ln -sf /Applications "$STAGING_DIR/Applications"

hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$STAGING_DIR" \
  -ov \
  -format UDZO \
  "$DIST_DIR/$DMG_NAME"

echo "DMG creado en: $DIST_DIR/$DMG_NAME"
