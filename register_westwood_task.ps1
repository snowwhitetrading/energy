# Registers (or re-registers) the weekly Westwood jackup rig scrape.
# Runs every Friday at 18:00 local time under the current user account.
#
#   powershell -ExecutionPolicy Bypass -File .\register_westwood_task.ps1
#
# Remove it again with:
#   Unregister-ScheduledTask -TaskName 'Westwood Jackup Rig Weekly Scrape' -Confirm:$false

$TaskName = 'Westwood Jackup Rig Weekly Scrape'
$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner   = Join-Path $Root 'run_westwood_scraper.bat'

if (-not (Test-Path $Runner)) {
    throw "Runner not found: $Runner"
}

$Action = New-ScheduledTaskAction -Execute $Runner -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At '18:00'

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $Action `
    -Trigger     $Trigger `
    -Settings    $Settings `
    -Description 'Scrapes Westwood Energy for the weekly global jackup rig count and the monthly jackup marketed committed / total utilisation rates and committed jackup rig count, then upserts them into MongoDB (IRIS).' `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State, @{n = 'NextRun'; e = { (Get-ScheduledTaskInfo $_).NextRunTime } } |
    Format-List
