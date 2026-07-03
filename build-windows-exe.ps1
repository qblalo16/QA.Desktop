$ErrorActionPreference = 'Stop'

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RootDir

$PythonBin = Join-Path $RootDir '.venv\Scripts\python.exe'
if (-not (Test-Path $PythonBin)) {
    $PythonBin = 'python'
}

$AssetsDir = Join-Path $RootDir 'assets'
$PngIcon = Join-Path $AssetsDir 'app-icon.png'
$IcoIcon = Join-Path $AssetsDir 'app-icon.ico'

& $PythonBin -m pip install --upgrade pip
& $PythonBin -m pip install -r requirements.txt pyinstaller pillow
& $PythonBin -m playwright install chromium

if (Test-Path $PngIcon) {
    $env:AIQA_ICON_PNG = $PngIcon
    $env:AIQA_ICON_ICO = $IcoIcon
    & $PythonBin -c @'
from pathlib import Path
import os
from PIL import Image

source = Path(os.environ['AIQA_ICON_PNG'])
target = Path(os.environ['AIQA_ICON_ICO'])
image = Image.open(source).convert('RGBA')
image.save(target, format='ICO', sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
'@
}

if (Test-Path 'build') { Remove-Item -Recurse -Force 'build' }
if (Test-Path 'dist') { Remove-Item -Recurse -Force 'dist' }

$PyInstallerArgs = @(
    '--noconfirm',
    '--clean',
    '--onefile',
    '--windowed',
    '--name', 'AI-QA-Desktop-Runner',
    '--paths', 'src',
    '--add-data', "assets/app-icon.ico;assets",
    '--add-data', "assets/app-icon.png;assets",
    '--collect-all', 'PySide6',
    '--collect-submodules', 'playwright',
    '--icon', $IcoIcon,
    'src/main.py'
)

& $PythonBin -m PyInstaller @PyInstallerArgs

Write-Host 'Executable generado en dist\AI-QA-Desktop-Runner.exe'