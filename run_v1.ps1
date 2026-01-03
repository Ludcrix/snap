Set-Location "C:\snap-bot"

# Legacy V1 bot is disabled by default; enable explicitly.
$env:RUN_LEGACY_BOT = "1"

# Prefer the workspace venv if present
$venvPy = "C:\snap-bot\.venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPy) {
  & $venvPy -m bot.telegram_control
} else {
  python -m bot.telegram_control
}
