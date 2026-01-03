Set-Location "C:\snap-bot"

# Legacy format-specific controller
$env:RUN_LEGACY_BOT = "1"

# Prefer the workspace venv if present
$venvPy = "C:\snap-bot\.venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPy) {
  & $venvPy -m bot.formats.anomalie_objet.telegram_control
} else {
  python -m bot.formats.anomalie_objet.telegram_control
}
