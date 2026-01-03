Set-Location "C:\snap-bot"

# Legacy V1 bot + V2 format integrations
$env:RUN_LEGACY_BOT = "1"

# Prefer the workspace venv if present
$venvPy = "C:\snap-bot\.venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPy) {
  & $venvPy -m bot.telegram_control_extended
} else {
  python -m bot.telegram_control_extended
}
