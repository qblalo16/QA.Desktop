#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="python3"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

APP_NAME="AI QA Desktop Runner"
DMG_NAME="AI-QA-Desktop-Runner-arm64.dmg"
DIST_DIR="$ROOT_DIR/dist"
STAGING_DIR="$DIST_DIR/dmg-staging"

echo "[1/5] Instalando dependencias de empaquetado..."
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt pyinstaller

echo "[2/5] Instalando Chromium para Playwright (embebido en build)..."
export PLAYWRIGHT_BROWSERS_PATH=0
"$PYTHON_BIN" -m playwright install chromium

echo "[3/5] Limpiando artefactos anteriores..."
rm -rf "$ROOT_DIR/build" "$DIST_DIR"

echo "[4/5] Generando .app con PyInstaller..."
"$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --windowed \
  --name "$APP_NAME" \
  --paths "$ROOT_DIR/src" \
  --collect-all PySide6 \
  --collect-all playwright \
  "$ROOT_DIR/src/main.py"

APP_PATH="$DIST_DIR/$APP_NAME.app"
if [[ ! -d "$APP_PATH" ]]; then
  echo "No se encontró la app generada en: $APP_PATH" >&2
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
