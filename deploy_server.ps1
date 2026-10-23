# Push the energy jobs to the Windows server (27.71.27.223, same box as C:\utility) over PowerShell
# Remoting (WinRM HTTPS, port 5986) - straight from the VS Code terminal. Asks for the Administrator
# password every run.
#   First time : powershell -ExecutionPolicy Bypass -File q:\Coding\energy\deploy_server.ps1 -Initial
#                (code + company CSVs + mongo_uri.txt, pip install, schedules, test run)
#   Later      : powershell -ExecutionPolicy Bypass -File q:\Coding\energy\deploy_server.ps1
#                (code + ThiTruongKhoan.csv only - the company CSVs on the server are the live copies,
#                 updated by the daily job, and are NOT overwritten)
#   Run now    : ... deploy_server.ps1 -RunNow            (start the daily disclosures job now)
#   Logs       : ... deploy_server.ps1 -Log
#   CSVs back  : ... deploy_server.ps1 -PullCsv           (server company_*.csv -> this folder, to commit)
#   Shell      : ... deploy_server.ps1 -Shell
# Company network (LAN cable) blocks 5986 -> unplug, use 4G.
param(
    [switch]$Initial,
    [switch]$Shell,
    [switch]$RunNow,
    [switch]$Log,
    [switch]$PullCsv,
    [string]$Server = "27.71.27.223",
    [string]$User = "Administrator",
    [string]$RemoteDir = "C:\energy"
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (-not (Test-NetConnection $Server -Port 5986 -InformationLevel Quiet -WarningAction SilentlyContinue)) {
    throw "Khong toi duoc ${Server}:5986 - rut cap mang cong ty, dung 4G roi chay lai."
}
$cred = Get-Credential -UserName $User -Message "Mat khau may chu $Server"
# the server's WinRM certificate is self-signed -> skip CA / name checks
$opt = New-PSSessionOption -SkipCACheck -SkipCNCheck -SkipRevocationCheck
$conn = @{ ComputerName = $Server; Port = 5986; UseSSL = $true; Credential = $cred; SessionOption = $opt }

if ($Shell) {
    Enter-PSSession @conn
    return
}
if ($RunNow -or $Log) {
    Invoke-Command @conn -ScriptBlock {
        param($run, $dir)
        if ($run) { schtasks /Run /TN "Energy disclosures hang ngay" }
        else {
            foreach ($f in "disclosures.log", "westwood.log") {
                $p = Join-Path $dir $f
                if (Test-Path $p) { "===== $f"; Get-Content $p -Tail 40 -Encoding UTF8 }
            }
        }
    } -ArgumentList ([bool]$RunNow), $RemoteDir
    return
}

$s = New-PSSession @conn
try {
    if ($PullCsv) {
        foreach ($f in Invoke-Command $s { param($d) Get-ChildItem "$d\company_*_quarterly.csv" | ForEach-Object Name } -ArgumentList $RemoteDir) {
            Copy-Item "$RemoteDir\$f" -Destination (Join-Path $here $f) -FromSession $s -Force
            Write-Host ">> $f"
        }
        Write-Host ">> Xong - xem 'git diff' roi commit neu muon luu lich su." -ForegroundColor Green
        return
    }

    Invoke-Command $s { param($d) New-Item -ItemType Directory -Force "$d\disclosures" | Out-Null } -ArgumentList $RemoteDir
    $files = @("CongTyDauKhi.py", "ThiTruongKhoan.py", "ThiTruongKhoan.csv", "update_disclosure.py", "auto_disclosure.py",
               "requirements.txt", "run_energy.ps1", "setup_server.ps1", "mongo_uri.txt") +
             (Get-ChildItem "disclosures\*.py" | ForEach-Object { "disclosures\" + $_.Name })
    if ($Initial) {
        $files += (Get-ChildItem "company_*_quarterly.csv" | ForEach-Object Name)
    }
    foreach ($f in $files) { if (-not (Test-Path $f)) { throw "Khong thay $f trong $here" } }
    # one zip (much faster than file-by-file over WinRM); keeps the disclosures\ folder
    $zip = Join-Path $env:TEMP "energy_push.zip"
    if (Test-Path $zip) { Remove-Item $zip }
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::Open($zip, "Create")
    try {
        foreach ($f in $files) {
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, (Join-Path $here $f), $f.Replace("\", "/")) | Out-Null
        }
    } finally { $archive.Dispose() }
    Write-Host ">> Chep $($files.Count) file len ${Server}:$RemoteDir"
    Copy-Item $zip -Destination "$RemoteDir\energy_push.zip" -ToSession $s -Force
    Invoke-Command $s {
        param($d)
        Expand-Archive "$d\energy_push.zip" -DestinationPath $d -Force
        Remove-Item "$d\energy_push.zip"
    } -ArgumentList $RemoteDir
    Remove-Item $zip

    if ($Initial) {
        Write-Host ">> Cai dat tren may chu (thu vien, lich chay, chay thu) - vai phut"
        Invoke-Command $s { param($d) $ErrorActionPreference = "Continue"; & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$d\setup_server.ps1" } `
            -ArgumentList $RemoteDir -ErrorAction Continue
        $ok = Invoke-Command $s { param($d) (Test-Path "$d\test_run.log") -and
            (Select-String -Path "$d\test_run.log" -Pattern "exit 0" -Quiet) } -ArgumentList $RemoteDir
        if (-not $ok) { throw "Cai dat / chay thu tren may chu loi - xem thong bao phia tren." }
    } else {
        Invoke-Command $s { param($d) & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$d\setup_server.ps1" -TasksOnly } `
            -ArgumentList $RemoteDir -ErrorAction Continue
        Invoke-Command $s { param($d) $ErrorActionPreference = "Continue"; & "C:\Program Files\Python312\python.exe" -m pip install -r "$d\requirements.txt" --quiet } `
            -ArgumentList $RemoteDir -ErrorAction Continue
    }
    Write-Host ">> Xong." -ForegroundColor Green
} finally {
    Remove-PSSession $s
}
