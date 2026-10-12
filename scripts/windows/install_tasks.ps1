<#
.SYNOPSIS
  Registers the F&O Scanner scheduled tasks for the current Windows user.

.DESCRIPTION
  Creates three tasks in the Task Scheduler folder "\FnO Scanner\":

    run-day       Mon-Fri 08:10   pythonw -m scanner --retries 3 --retry-wait 300 run-day
    renew-token   daily 07:55     pythonw -m scanner renew-token --if-needed
    dashboard     at logon        pythonw -m scanner serve

  All run as YOU, only while you are logged on (no password is stored), with
  no console window (pythonw.exe). Missed starts run as soon as possible;
  a second copy is never started. Holidays/weekends: run-day exits by itself.

  Run in a normal (non-administrator) PowerShell from anywhere:
      powershell -ExecutionPolicy Bypass -File scripts\windows\install_tasks.ps1
  Preview without registering anything:
      powershell -ExecutionPolicy Bypass -File scripts\windows\install_tasks.ps1 -DryRun
  Remove the tasks again:
      powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_tasks.ps1
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoDashboard
)

$ErrorActionPreference = "Stop"
$TaskPath = "\FnO Scanner\"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$EnvFile = Join-Path $Root ".env"

Write-Host "Project:  $Root"
if (-not (Test-Path $Pythonw)) {
    throw "Not found: $Pythonw. Create the virtual environment first (python -m venv .venv; pip install -r requirements.txt)."
}

# Check .env has the needed keys set — values are never printed.
function Test-EnvKey([string]$Name) {
    if (-not (Test-Path $EnvFile)) { return $false }
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match "^\s*$Name\s*=\s*(.+)$" -and $Matches[1].Trim().Trim('"').Trim("'")) { return $true }
    }
    return $false
}
$hasClient = Test-EnvKey "DHAN_CLIENT_ID"
$hasDash = Test-EnvKey "DASHBOARD_TOKEN"
Write-Host ("DHAN_CLIENT_ID set:  " + $hasClient)
Write-Host ("DASHBOARD_TOKEN set: " + $hasDash)
if (-not $hasClient) { Write-Warning "DHAN_CLIENT_ID is empty in .env; Dhan steps will fail until it is set." }
if (-not $NoDashboard -and -not $hasDash) {
    Write-Warning "DASHBOARD_TOKEN is empty in .env; the dashboard task would refuse to start on your Wi-Fi. Set it, or use -NoDashboard."
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

function New-Settings([TimeSpan]$Limit) {
    New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -WakeToRun `
        -ExecutionTimeLimit $Limit
}

function New-Action([string]$Arguments) {
    New-ScheduledTaskAction -Execute $Pythonw -Argument $Arguments -WorkingDirectory $Root
}

$tasks = @(
    @{
        Name        = "run-day"
        Description = "F&O Scanner: universe 08:30, previous close 08:45, pre-open 09:10, live 09:15-15:30, archive 15:35, publish 15:40."
        Action      = New-Action "-m scanner --retries 3 --retry-wait 300 run-day"
        Trigger     = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "08:10"
        Settings    = New-Settings (New-TimeSpan -Hours 9)
    },
    @{
        Name        = "renew-token"
        Description = "F&O Scanner: keep the Dhan access token valid every day, weekends included (renews when < 12h left)."
        Action      = New-Action "-m scanner renew-token --if-needed"
        Trigger     = New-ScheduledTaskTrigger -Daily -At "07:55"
        Settings    = New-Settings (New-TimeSpan -Minutes 10)
    }
)
if (-not $NoDashboard) {
    $tasks += @{
        Name        = "dashboard"
        Description = "F&O Scanner: dashboard and read-only API on port 8000 (requires DASHBOARD_TOKEN)."
        Action      = New-Action "-m scanner --retries 3 --retry-wait 60 serve"
        Trigger     = New-ScheduledTaskTrigger -AtLogOn -User $user
        Settings    = New-Settings ([TimeSpan]::Zero)      # no time limit
    }
}

foreach ($t in $tasks) {
    $trigger = $t.Trigger
    Write-Host ""
    Write-Host ("Task:     " + $TaskPath + $t.Name)
    Write-Host ("Runs:     " + $Pythonw + " " + $t.Action.Arguments)
    $when = ""
    if ($trigger.StartBoundary) { $when = ([DateTime]::Parse($trigger.StartBoundary)).ToString("HH:mm") + " (local time)" }
    Write-Host ("Trigger:  " + ($trigger.CimClass.CimClassName -replace "MSFT_Task", "") + " " + $when)
    if ($DryRun) {
        Write-Host "DryRun:   not registered"
        continue
    }
    Register-ScheduledTask -TaskName $t.Name -TaskPath $TaskPath -Description $t.Description `
        -Action $t.Action -Trigger $trigger -Settings $t.Settings -Principal $principal -Force | Out-Null
    Write-Host "Registered."
}

Write-Host ""
if ($DryRun) {
    Write-Host "Dry run only: nothing was registered."
} else {
    Write-Host "Done. Open Task Scheduler > Task Scheduler Library > FnO Scanner to review."
    Write-Host "Logs: $Root\logs"
}
