Set-Location "C:\snap-bot"

# Enable visible device actions + Android debug logs
$env:V3_ENABLE_DEVICE_INPUT = "1"
$env:V3_ANDROID_DEBUG = "1"

# Prefer the workspace venv if present
$venvPy = "C:\snap-bot\.venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPy) {
  & $venvPy -m bot.v3.main
} else {
  python -m bot.v3.main
}
