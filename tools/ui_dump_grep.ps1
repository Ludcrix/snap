param(
  [string]$AdbPath = "",
  [string]$Pattern = "il y a|minutes?|heures?|jours?|vues?|vue|j'aime|likes?|comment|partage|shares?|Sponsor|Sponsoris",
  [int]$MaxHits = 20,
  [switch]$WithContext
)

$ErrorActionPreference = "Stop"

function Resolve-AdbPath {
  param([string]$Provided)

  if ($Provided -and (Test-Path $Provided)) { return $Provided }

  $wingetAdb = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe"
  if (Test-Path $wingetAdb) { return $wingetAdb }

  $cmd = Get-Command adb -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }

  return ""
}

$adb = Resolve-AdbPath -Provided $AdbPath
if (-not $adb) {
  Write-Host "[UI] ERROR: adb.exe introuvable. Passe -AdbPath ou installe platform-tools." -ForegroundColor Red
  exit 2
}

Write-Host "[UI] adb=$adb"
Write-Host "[UI] pattern=$Pattern"

Write-Host "[UI] Step 1: uiautomator dump -> /sdcard/window_dump.xml ..."
& $adb shell uiautomator dump --compressed /sdcard/window_dump.xml | Out-Null
Write-Host "[UI] Step 1 done."

Write-Host "[UI] Step 2: sanity check file (ls + head)"
& $adb shell "ls -l /sdcard/window_dump.xml; head -c 120 /sdcard/window_dump.xml"

Write-Host "[UI] Step 3: grep markers in XML ..."
$xml = & $adb shell cat /sdcard/window_dump.xml

if (-not $xml) {
  Write-Host "[UI] ERROR: XML empty (dump failed?)" -ForegroundColor Red
  exit 3
}

if ($WithContext) {
  $hits = $xml | Select-String -Pattern $Pattern -CaseSensitive:$false -Context 1,1
} else {
  $hits = $xml | Select-String -Pattern $Pattern -CaseSensitive:$false
}

if ($hits) {
  Write-Host ("[UI] HIT count=" + $hits.Count) -ForegroundColor Green
  $hits | Select-Object -First $MaxHits
  exit 0
}

Write-Host "[UI] No hits found in UIAutomator XML -> probablement overlay non exposé (OCR needed)." -ForegroundColor Yellow
exit 0
