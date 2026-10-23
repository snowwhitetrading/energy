# Run one energy job and log it - used by Windows Task Scheduler on the server (setup_server.ps1).
#   Daily  : run_energy.ps1 -Job disclosures -Log disclosures.log   (oil & gas BCTC / bulletins -> CongTyDauKhi + CSV)
#   Weekly : run_energy.ps1 -Job westwood    -Log westwood.log      (Westwood jack-up scrape + SEA series -> ThiTruongKhoan)
# By hand : powershell -ExecutionPolicy Bypass -File C:\energy\run_energy.ps1 -Job disclosures [-Extra "--tickers PVS"]
param(
    [ValidateSet("disclosures", "westwood")] [string]$Job = "disclosures",
    [string]$Log = "energy_last_run.log",
    [string]$Extra = ""
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
# Python: the all-users install of the utility setup on the server, else the laptop venv, else PATH
$python = @("C:\Program Files\Python312\python.exe", (Join-Path $root ".venv\Scripts\python.exe")) |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
$logPath = Join-Path $root $Log
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore"

Set-Location $root
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($logPath, "=== bat dau $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $Job $Extra`r`n", $utf8)
# one energy job at a time (both write CSVs in this folder)
$lock = New-Object System.Threading.Mutex($false, "Global\EnergyJobs")
try { $got = $lock.WaitOne([TimeSpan]::FromHours(3)) } catch [System.Threading.AbandonedMutexException] { $got = $true }
if (-not $got) {
    [IO.File]::AppendAllText($logPath, "=== bo qua: lan chay truoc chua xong sau 3 gio`r`n", $utf8)
    exit 1
}
try {
    if ($Job -eq "disclosures") {
        # cmd passes Python's UTF-8 bytes through untouched (PowerShell 5.1 would re-encode with *>>)
        cmd /c "`"$python`" -u `"$root\auto_disclosure.py`" $Extra >> `"$logPath`" 2>&1"
        $code = $LASTEXITCODE
    } else {
        cmd /c "`"$python`" -u `"$root\ThiTruongKhoan.py`" --all $Extra >> `"$logPath`" 2>&1"
        $code = $LASTEXITCODE
        cmd /c "`"$python`" -u `"$root\ThiTruongKhoan.py`" --import-sea >> `"$logPath`" 2>&1"
        if ($LASTEXITCODE -ne 0) { $code = $LASTEXITCODE }
    }
} finally {
    $lock.ReleaseMutex()
}
[IO.File]::AppendAllText($logPath, "=== ket thuc $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (exit $code)`r`n", $utf8)
exit $code
