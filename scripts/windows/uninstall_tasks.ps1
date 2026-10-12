<#
.SYNOPSIS
  Removes the F&O Scanner scheduled tasks created by install_tasks.ps1.

  powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_tasks.ps1 [-DryRun]
#>
[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = "Stop"
$TaskPath = "\FnO Scanner\"
$found = @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)
if (-not $found) {
    Write-Host "No tasks found in $TaskPath"
    return
}
foreach ($t in $found) {
    if ($DryRun) {
        Write-Host ("Would remove " + $TaskPath + $t.TaskName)
    } else {
        Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $TaskPath -Confirm:$false
        Write-Host ("Removed " + $TaskPath + $t.TaskName)
    }
}
