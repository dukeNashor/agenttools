[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

$config = Get-SkillUpdaterConfig
$projectRoot = Get-SkillUpdaterRoot
$reportScript = Join-Path $PSScriptRoot 'New-SkillUpdateReport.ps1'
$taskName = [string]$config.task.name
$dayOfWeek = [string]$config.task.dayOfWeek
$at = [datetime]::ParseExact([string]$config.task.time, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$windowsPowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$actionArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $reportScript + '" -Open'
$action = New-ScheduledTaskAction -Execute $windowsPowerShell -Argument $actionArguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $dayOfWeek -At $at
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Generate and open a read-only HTML report for tracked agent skill updates.'

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
$registered = Get-ScheduledTask -TaskName $taskName
Write-Host "Scheduled task registered: $($registered.TaskName)"
Write-Host "Schedule: $dayOfWeek $($config.task.time), current interactive user, start when available."
