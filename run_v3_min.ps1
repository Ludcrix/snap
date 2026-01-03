Set-Location "C:\snap-bot"

Write-Host "[RUN] V3 minimal -> python -m bot.v3.main" -ForegroundColor Cyan

$env:V3_ENABLE_DEVICE_INPUT = "1"

# Prefer the workspace venv if present
$venvPy = "C:\snap-bot\.venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPy) {
	& $venvPy -m bot.v3.main
} else {
	python -m bot.v3.main
}
