# Set up the energy jobs on the Windows server (same box as C:\utility) and schedule them.
# Run by deploy_server.ps1 (-Initial installs, later deploys use -TasksOnly). By hand, as Administrator:
#     powershell -ExecutionPolicy Bypass -File C:\energy\setup_server.ps1
# Safe to re-run. Python 3.12 comes from the utility setup (C:\Program Files\Python312).
param([switch]$TasksOnly)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "C:\Program Files\Python312\python.exe"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Mo PowerShell bang 'Run as Administrator' roi chay lai."
}
if (-not (Test-Path $python)) { throw "Khong thay $python - chay setup_server.ps1 cua C:\utility truoc." }

if (-not $TasksOnly) {
    Write-Host ">> 1/3 Thu vien Python"
    $ErrorActionPreference = "Continue"  # pip writes warnings to stderr
    & $python -m pip install -r (Join-Path $root "requirements.txt") --quiet
    # PDF reading + OCR for the disclosure parsers (pure pip, no Tesseract needed)
    & $python -m pip install pdfplumber pymupdf rapidocr-onnxruntime requests --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip install loi" }
    $ErrorActionPreference = "Stop"
    if (-not (Test-Path (Join-Path $root "mongo_uri.txt"))) { throw "Thieu C:\energy\mongo_uri.txt" }
}

# 2/3 Schedules under SYSTEM -> run with nobody logged in (server clock is Vietnam time)
Write-Host ">> 2/3 Lich chay"
$run = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\run_energy.ps1`""
# oil & gas BCTC / investor bulletins: daily, after the utility run at 07:00
schtasks /Create /F /TN "Energy disclosures hang ngay" /RU SYSTEM /RL HIGHEST /SC DAILY /ST 08:30 `
    /TR "$run -Job disclosures -Log disclosures.log" | Out-Null
# Westwood jack-up rig counts / utilisation + SEA day rates: Friday evening, after Westwood's weekly update
schtasks /Create /F /TN "Energy Westwood hang tuan" /RU SYSTEM /RL HIGHEST /SC WEEKLY /D FRI /ST 18:00 `
    /TR "$run -Job westwood -Log westwood.log" | Out-Null
foreach ($t in "Energy disclosures hang ngay", "Energy Westwood hang tuan") {
    schtasks /Query /TN $t /FO LIST | Select-String "TaskName|Next Run"
}
if ($TasksOnly) { return }

# 3/3 Test run: one ticker, a published quarter, write nothing - checks sites, OCR and MongoDB
Write-Host ">> 3/3 Chay thu (PVD 2Q2026, dry run) ..."
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$root\run_energy.ps1" -Job disclosures `
    -Extra "--tickers PVD --quarter 2 --year 2026 --dry-run" -Log test_run.log
Get-Content (Join-Path $root "test_run.log") -Encoding UTF8 | Select-Object -Last 15
Write-Host ""
Write-Host ">> Xong. May chu tu chay: BCTC dau khi 08:30 hang ngay, Westwood 18:00 thu Sau. Log: $root\disclosures.log, westwood.log" -ForegroundColor Green
